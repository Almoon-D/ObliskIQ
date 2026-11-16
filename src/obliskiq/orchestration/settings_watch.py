"""Settings apply at the next cycle boundary, and every change is audited (MASTER_PLAN §5.17).

**The boundary.** The service is built from the settings it starts with. :class:`SettingsWatch`
remembers the owner's panel as it was read; the service asks it before each heartbeat or job, which
is never inside a cycle. When the panel changed and the whole configuration validates with it, the
service stops its loop, closes everything it opened and starts again from the new settings
(``cli/service.py``): every part is rebuilt from one consistent configuration, and a cycle that
was due runs after the start through the catch-up. A panel that does not validate changes nothing:
the service keeps the settings in force and the owner is told why, once per edit.

**The audit.** ``obliskiq settings set``, Telegram ``/set`` and MCP ``update_setting`` audit what
they change. :func:`audit_unrecorded` catches the rest at each start: it compares the effective
settings with those the last start recorded (``paths.settings_applied``) and audits each difference
the log does not already hold as the setting's latest value, naming where it came from: the
owner's file (an edit by hand) or the expert defaults (a release that changed one).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config.audit import AuditEntry, SettingChange, SettingsAuditLog
from ..config.changes import file_hash
from ..config.loader import LoadedConfig, display_path, flatten
from ..errors import ConfigError

#: Who the audit names for a difference no command recorded.
FILE_ACTOR = "owner (file edit)"
DEFAULTS_ACTOR = "release (expert defaults)"
FILE_CHANNEL = "owner file"
DEFAULTS_CHANNEL = "defaults"
_ABSENT = object()


@dataclass(frozen=True, slots=True)
class SettingsCheck:
    """What the watch found: ``changed`` when the service should restart on the new panel;
    ``problem`` when the panel changed and does not validate."""

    changed: bool
    problem: str | None = None


class SettingsWatch:
    """The owner's panel the service runs with, and whether a valid change is waiting."""

    def __init__(self, owner_file: Path, *, reload: Callable[[], LoadedConfig]) -> None:
        self._owner_file = owner_file
        self._reload = reload
        self._running = file_hash(owner_file)
        self._refused: str | None = None

    @property
    def owner_file(self) -> Path:
        return self._owner_file

    def check(self) -> SettingsCheck:
        """Read the panel's hash; on a change, validate the whole configuration once per edit."""
        found = file_hash(self._owner_file)
        if found in (self._running, self._refused):
            return SettingsCheck(changed=False)
        try:
            self._reload()
        except ConfigError as exc:
            self._refused = found
            return SettingsCheck(changed=False, problem=str(exc))
        return SettingsCheck(changed=True)


def _snapshot(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path} cannot be read as the settings last applied: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path} does not hold the settings last applied")
    return data


def _write_snapshot(path: Path, settings: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(settings, sort_keys=True, indent=1), encoding="utf-8")
    temporary.replace(path)


def audit_unrecorded(
    loaded: LoadedConfig, data_dir: Path, audit: SettingsAuditLog, *, at: datetime
) -> tuple[AuditEntry, ...]:
    """Audit every effective setting that changed since the last start and that no command
    recorded, then record the settings now in force. The first start records them only."""
    settings = loaded.settings
    path = data_dir / settings.paths.settings_applied
    current = flatten(settings.model_dump(mode="json"))
    previous = _snapshot(path)
    written: list[AuditEntry] = []
    if previous is not None:
        latest: dict[str, Any] = {entry.key: entry.new for entry in audit.entries()}
        owner = display_path(loaded.owner_file, loaded.config_dir)
        by_file: list[SettingChange] = []
        by_defaults: list[SettingChange] = []
        for key in sorted(previous.keys() | current.keys()):
            old, new = previous.get(key), current.get(key)
            if old == new or latest.get(key, _ABSENT) == new:
                continue
            change = SettingChange(key=key, old=old, new=new)
            (by_file if loaded.sources.get(key) == owner else by_defaults).append(change)
        written += audit.record(by_file, actor=FILE_ACTOR, channel=FILE_CHANNEL, reason=None, at=at)
        written += audit.record(
            by_defaults, actor=DEFAULTS_ACTOR, channel=DEFAULTS_CHANNEL, reason=None, at=at
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_snapshot(path, current)
    return tuple(written)
