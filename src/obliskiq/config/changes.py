"""Changing a setting: validated, permitted for its channel, written, audited (MASTER_PLAN §5.17).

``config/owner.yaml`` stays the source of truth. A change names one setting and its new value;
:func:`propose` edits the panel's text (``owner_file.py``), loads the whole configuration with it
and diffs the effective settings, so a change the schema refuses never reaches the file, and the
diff shows everything the edit moves. :func:`check_remote` is the rule for Telegram and the MCP
cockpit: only a setting ``remote_settings`` lists, a risk limit only in the direction that
tightens it, a budget only inside its bounds; the command line on the machine may change any
setting. :func:`apply` writes the panel only if nobody changed it since it was read, then records
one audit entry per changed setting. The service applies the file at its next cycle boundary
(``orchestration/settings_watch.py``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import hashlib
import tempfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml
from pydantic import JsonValue

from ..errors import ObliskError
from .audit import AuditEntry, SettingChange, SettingsAuditLog, diff_settings
from .loader import (
    DEFAULTS_DIR,
    SECTION_FILES,
    YAML_GLOB,
    ConfigInvalid,
    LoadedConfig,
    display_path,
    flatten,
    load_config,
    read_yaml_mapping,
)
from .owner_file import OwnerFileError, edit_text, write_atomically
from .schema import ObliskConfig
from .schema_server import BoundedRule, RemoteSettingsConfig

#: Channels: the command line on the machine, and the two remote ones.
CLI = "cli"
TELEGRAM = "telegram"
MCP = "mcp"
#: How the loader names a value the environment set (``Environment.config_overrides``).
ENVIRONMENT_SOURCE = "environment"


class SettingsRefusal(ObliskError):
    """A settings change that is not made, and why, in the owner's words."""


@dataclass(frozen=True)
class ProposedChange:
    """A change ready to apply: the panel's new text and what it changes."""

    key: str
    value: JsonValue
    changes: tuple[SettingChange, ...]
    text: str
    #: The SHA-256 of the panel as it was read; :func:`apply` refuses if it changed since.
    read_hash: str

    def describe(self) -> str:
        return "; ".join(f"{c.key}: {_shown(c.old)} -> {_shown(c.new)}" for c in self.changes)


def _shown(value: JsonValue) -> str:
    return "not set" if value is None else str(value)


def settings_fingerprint(settings: ObliskConfig) -> str:
    """The SHA-256 of the effective settings, canonically encoded: the heartbeat names the
    settings the service runs on with it."""
    return hashlib.sha256(settings.model_dump_json().encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    """The SHA-256 of ``path``'s bytes; of nothing when it does not exist."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return hashlib.sha256(b"").hexdigest()


def parse_value(raw: str, current: JsonValue) -> JsonValue:
    """The owner's text as a value of the setting's kind: a setting held as text (a quoted
    decimal, a name) takes the text itself; anything else is read as YAML (a number, a list)."""
    text = raw.strip()
    if not text:
        raise SettingsRefusal("a setting needs a value")
    if isinstance(current, str):
        return text.strip("\"'")
    try:
        value: JsonValue = yaml.safe_load(text)
    except yaml.YAMLError:
        raise SettingsRefusal(f"{raw!r} is not a value a setting can hold") from None
    return value


def propose(loaded: LoadedConfig, key: str, raw: str) -> ProposedChange:
    """The change setting ``key`` to ``raw`` makes, validated with the whole configuration."""
    leaves = flatten(loaded.settings.model_dump(mode="json"))
    if key not in leaves:
        raise SettingsRefusal(f"there is no setting {key!r} (obliskiq settings show lists them)")
    value = parse_value(raw, leaves[key])
    owner_file = loaded.owner_file
    try:
        current_text = owner_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        current_text = ""
    read_hash = hashlib.sha256(current_text.encode("utf-8")).hexdigest()
    try:
        text = edit_text(current_text, key.split("."), value)
    except OwnerFileError as exc:
        raise SettingsRefusal(str(exc)) from None
    after = _validated(loaded, text)
    changes = diff_settings(loaded.settings, after)
    if not changes:
        raise SettingsRefusal(f"{key} is already {_shown(leaves[key])}: nothing to change")
    return ProposedChange(key, value, changes, text, read_hash)


def environment_overrides(loaded: LoadedConfig) -> dict[str, tuple[Any, str]]:
    """The values the environment set over the files (``OBLISK_LOG_*``), as ``load_config``
    takes them, so a candidate panel is read the way ``loaded`` was."""
    flat = flatten(loaded.settings.model_dump(mode="json"))
    return {
        key: (flat[key], source)
        for key, source in loaded.sources.items()
        if source.startswith(ENVIRONMENT_SOURCE) and key in flat
    }


def _validated(loaded: LoadedConfig, text: str) -> ObliskConfig:
    """The configuration with ``text`` as the owner's panel, read with the same environment
    overrides as ``loaded``; refused with every problem."""
    with tempfile.TemporaryDirectory(prefix="obliskiq-settings-") as folder:
        candidate = Path(folder) / loaded.owner_file.name
        candidate.write_text(text, encoding="utf-8")
        try:
            return load_config(
                loaded.config_dir,
                owner_file=candidate,
                overrides=environment_overrides(loaded),
            ).settings
        except ConfigInvalid as exc:
            problems = "; ".join(problem.message for problem in exc.problems)
            raise SettingsRefusal(f"the configuration would be invalid: {problems}") from None


def _decimal(value: Any, key: str) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise SettingsRefusal(f"{key}: {value!r} is not a number") from None


def check_remote(change: ProposedChange, rules: RemoteSettingsConfig) -> None:
    """Refuse a change Telegram or the MCP cockpit may not make: a setting ``remote_settings``
    does not list, a risk limit moved the way that loosens it, a budget outside its bounds."""
    for moved in change.changes:
        rule = rules.rule(moved.key)
        if rule is None:
            raise SettingsRefusal(
                f"{moved.key} cannot be changed remotely; run `obliskiq settings set` on the "
                "machine"
            )
        new = _decimal(moved.new, moved.key)
        if isinstance(rule, BoundedRule):
            if not rule.min <= new <= rule.max:
                raise SettingsRefusal(
                    f"{moved.key} may be set remotely only between {rule.min} and {rule.max}"
                )
            continue
        old = _decimal(moved.old, moved.key)
        looser = new > old if rule.tighter == "lower" else new < old
        if looser:
            raise SettingsRefusal(
                f"{moved.key} {old} -> {new} loosens a risk limit; that needs `obliskiq settings "
                "set` on the machine"
            )


def apply(
    change: ProposedChange,
    loaded: LoadedConfig,
    audit: SettingsAuditLog,
    *,
    actor: str,
    channel: str,
    reason: str | None,
    at: datetime,
) -> tuple[AuditEntry, ...]:
    """Write the panel and audit every changed setting. Refused when the panel changed since the
    change was proposed (someone else edited it): propose again."""
    if file_hash(loaded.owner_file) != change.read_hash:
        raise SettingsRefusal("the owner's file changed meanwhile; read it again and retry")
    try:
        write_atomically(loaded.owner_file, change.text)
    except OSError as exc:
        raise SettingsRefusal(f"the owner's file could not be written: {exc}") from None
    return audit.record(change.changes, actor=actor, channel=channel, reason=reason, at=at)


def owner_overrides(loaded: LoadedConfig) -> list[tuple[str, Any, JsonValue]]:
    """``(key, expert default, value in force)`` for every setting the owner's panel sets; the
    default is ``None`` where no defaults file declares the key (the owner's alone)."""
    config_dir = loaded.config_dir
    owner = flatten(
        read_yaml_mapping(loaded.owner_file, display_path(loaded.owner_file, config_dir))
    )
    defaults: dict[str, Any] = {}
    paths = [
        *sorted((config_dir / DEFAULTS_DIR).glob(YAML_GLOB)),
        *(config_dir / name for name in sorted(SECTION_FILES)),
    ]
    for path in paths:
        defaults |= flatten(read_yaml_mapping(path, display_path(path, config_dir)))
    effective = flatten(loaded.settings.model_dump(mode="json"))
    return [(key, defaults.get(key), effective.get(key)) for key in sorted(owner)]
