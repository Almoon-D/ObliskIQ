"""``obliskiq settings set|diff|history`` (``show`` lives in ``cli/app.py``) and ``obliskiq pause``.

``settings set <key> <value> --reason ...`` changes one setting in the owner's panel from the
machine, where any setting may change, loosening a risk limit included (MASTER_PLAN §5.17): it
prints what the change moves, validated with the whole configuration, and writes and audits it
only with ``--confirm``. The running service applies it at its next cycle boundary. ``settings
diff`` lists what the owner's panel changes from the expert defaults, and whether the service runs
on the panel as it is now; ``settings history`` the audit log. ``pause on|off|status`` skips the
decision cycles until resumed (``orchestration/pause.py``). None opens a brokerage session.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import argparse
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TextIO

from ..config.audit import SettingsAuditLog
from ..config.changes import (
    CLI,
    SettingsRefusal,
    apply,
    owner_overrides,
    propose,
    settings_fingerprint,
)
from ..errors import SettingsAuditError
from ..orchestration.health import read_heartbeat
from ..orchestration.pause import clear_pause, read_pause, set_pause

if TYPE_CHECKING:
    from .app import Loaded

EXIT_OK = 0
EXIT_INVALID = 1
OWNER_ACTOR = "owner (command line)"
#: The heartbeat's fact naming the fingerprint of the settings the service runs on.
SETTINGS_FACT = "settings"
#: Audit entries ``settings history`` prints by default.
HISTORY_ROWS = 20


def _settings_set(loaded: Loaded, out: TextIO) -> int:
    args = loaded.args
    try:
        change = propose(loaded.config, args.key, args.value)
    except SettingsRefusal as exc:
        print(f"refused: {exc}", file=out)
        return EXIT_INVALID
    print(f"{change.describe()} (in {loaded.config.owner_file})", file=out)
    if not args.confirm:
        print("nothing was changed: run the same command with --confirm to change it", file=out)
        return EXIT_INVALID
    audit = SettingsAuditLog.for_settings(loaded.config.settings, loaded.env)
    try:
        entries = apply(
            change,
            loaded.config,
            audit,
            actor=OWNER_ACTOR,
            channel=CLI,
            reason=args.reason,
            at=datetime.now(UTC),
        )
    except (SettingsRefusal, SettingsAuditError) as exc:
        print(f"refused: {exc}", file=out)
        return EXIT_INVALID
    print(
        f"changed and audited ({len(entries)} entr{'y' if len(entries) == 1 else 'ies'}); the "
        "service applies it at its next cycle boundary",
        file=out,
    )
    return EXIT_OK


def _settings_diff(loaded: Loaded, out: TextIO) -> int:
    rows = owner_overrides(loaded.config)
    print(f"{len(rows)} setting(s) set in {loaded.config.owner_file}:", file=out)
    for key, default, value in rows:
        shown = "no expert default" if default is None else f"default {default}"
        print(f"  {key}: {value} ({shown})", file=out)
    settings = loaded.config.settings
    data_dir = loaded.env.data_dir
    if data_dir is None:
        return EXIT_OK
    beat, facts = read_heartbeat(data_dir / settings.paths.heartbeat_file)
    running = facts.get(SETTINGS_FACT)
    if beat is None or running is None:
        print("the service: no heartbeat says which settings it runs on", file=out)
    elif running == settings_fingerprint(settings):
        print(f"the service runs on these settings (heartbeat {beat.isoformat()})", file=out)
    else:
        print(
            "the service runs on other settings: these apply at its next cycle boundary",
            file=out,
        )
    return EXIT_OK


def _settings_history(loaded: Loaded, out: TextIO) -> int:
    audit = SettingsAuditLog.for_settings(loaded.config.settings, loaded.env)
    try:
        entries = audit.entries()
    except SettingsAuditError as exc:
        print(f"refused: {exc}", file=out)
        return EXIT_INVALID
    shown = entries[-loaded.args.limit :] if loaded.args.limit > 0 else entries
    print(f"{len(entries)} change(s) audited; the latest {len(shown)}:", file=out)
    for entry in shown:
        why = f" ({entry.reason})" if entry.reason else ""
        print(
            f"  {entry.seq} {entry.at.isoformat()} {entry.key}: {entry.old} -> {entry.new} by "
            f"{entry.actor} via {entry.channel}{why}",
            file=out,
        )
    return EXIT_OK


def _pause(loaded: Loaded, out: TextIO) -> int:
    path = loaded.env.require_data_dir() / loaded.config.settings.paths.pause_flag
    action = loaded.args.action
    if action == "on":
        state = set_pause(path, actor=OWNER_ACTOR, reason=loaded.args.reason, at=datetime.now(UTC))
    elif action == "off":
        state = clear_pause(path)
    else:
        state = read_pause(path)
    print(state.describe(), file=out)
    return EXIT_OK


def add_settings_actions(actions: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """``settings set|diff|history`` beside ``settings show``."""
    change = actions.add_parser(
        "set", help="change one setting (validated, audited, applied at the next cycle)"
    )
    change.add_argument("key", help="the dotted setting, as `settings show` lists it")
    change.add_argument("value", help="its new value")
    change.add_argument("--reason", required=True, help="why (audited)")
    change.add_argument("--confirm", action="store_true", help="write it: without it, a preview")
    change.set_defaults(handler=_settings_set)
    actions.add_parser(
        "diff", help="what your settings change from the expert defaults, and what is pending"
    ).set_defaults(handler=_settings_diff)
    history = actions.add_parser("history", help="the audited changes, latest last")
    history.add_argument(
        "--limit", type=int, default=HISTORY_ROWS, help="how many (0: every change)"
    )
    history.set_defaults(handler=_settings_history)


def add_pause_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    pause = commands.add_parser(
        "pause", help="skip the decision cycles until resumed (lighter than the kill switch)"
    )
    actions = pause.add_subparsers(dest="action", required=True, metavar="ACTION")
    on = actions.add_parser("on", help="pause: no new decision, protective exits stay")
    on.add_argument("--reason", required=True, help="why (recorded with the pause)")
    on.set_defaults(handler=_pause)
    actions.add_parser("off", help="resume the decision cycles").set_defaults(handler=_pause)
    actions.add_parser("status", help="whether the bot is paused").set_defaults(handler=_pause)
