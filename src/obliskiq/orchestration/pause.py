"""The owner's pause: decision cycles are skipped while it holds.

A pause is lighter than the kill switch. Nothing new is decided or placed (each cycle that falls
due is recorded as skipped, with who paused and why), while the heartbeat, the kill switch's
sweep, the protective exits resting at the broker, the collections and the digest go on. The
kill switch also cancels working entry orders and blocks every order that adds risk; a pause only
stops the bot deciding. It is a file in the data directory (``paths.pause_flag``) so the service,
the command line and the cockpit share it without a broker session; a flag that cannot be read
counts as set.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..util.timeutil import parse_stamp, utc_stamp


@dataclass(frozen=True, slots=True)
class PauseState:
    """Whether the bot is paused, and by whom, when and why when it is."""

    on: bool
    actor: str | None = None
    reason: str | None = None
    at: datetime | None = None

    def describe(self) -> str:
        if not self.on:
            return "not paused"
        when = "" if self.at is None else f" since {self.at.isoformat()}"
        who = self.actor or "someone"
        return f"paused by {who}{when}: {self.reason or 'no reason recorded'}"


def read_pause(path: Path) -> PauseState:
    """The pause the flag at ``path`` holds; one that cannot be read counts as on."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return PauseState(on=False)
    except OSError:
        return PauseState(on=True, reason="the pause flag cannot be read")
    try:
        data = json.loads(text)
        return PauseState(
            on=True,
            actor=str(data["actor"]),
            reason=str(data["reason"]),
            at=parse_stamp(str(data["at"])),
        )
    except ValueError, KeyError, TypeError:
        return PauseState(on=True, reason="the pause flag is unreadable")


def set_pause(path: Path, *, actor: str, reason: str, at: datetime) -> PauseState:
    """Pause: write the flag atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.tmp")
    staging.write_text(
        json.dumps({"at": utc_stamp(at), "actor": actor, "reason": reason}) + "\n",
        encoding="utf-8",
    )
    staging.replace(path)
    return PauseState(on=True, actor=actor, reason=reason, at=at)


def clear_pause(path: Path) -> PauseState:
    """Resume: remove the flag."""
    path.unlink(missing_ok=True)
    return PauseState(on=False)
