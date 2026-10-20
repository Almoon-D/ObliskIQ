"""Alerts: what the owner must hear, said once while its condition holds.

Every source of trouble becomes an :class:`Alert`: the order path's ``OrderAlert`` (a declined
warning, an unknown outcome, a failed conversion, a missing stop, drift, …), the broker session's
``SessionAlert`` (a competing session, a lost session, a failed or successful re-login), a calendar
disagreement, a kill-switch sweep, and the service's own findings (a failed cycle, a stuck intent,
a blocked venue, a downgrade).

A **condition** alert is sent once and then held in the :class:`AlertBook` under its key while the
condition holds: the heartbeat that alerts a stop it cannot place on every pass, or a conversion
that fails on every funding attempt, reaches the owner once. A condition clears when a round of
the job that raised it (a heartbeat, a cycle) ends without raising it again, or when an event
resolves it (a successful re-login clears the session's alerts); raised again after that, it is
sent again. An **event** (a re-login that succeeded, a downgrade) is sent every time.

The book is kept in a JSON file in the data directory (``paths.alert_state``), written atomically,
so a restart does not repeat what the owner already heard; a file that cannot be read is set aside
and the book starts empty (market-pulse-bot's state file, legacy/pulse notification_backend.py).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..calendars.status import Disagreement
from ..execution.boundary import OrderAlert, OrderAlertKind
from ..execution.killswitch import SweepReport
from ..ibkr.session import SessionAlert, SessionAlertKind
from ..logging_setup import get_logger
from ..util.timeutil import utc_stamp

log = get_logger("reporting.alerts")

_DIGITS = re.compile(r"\d+(\.\d+)?")


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


#: How each severity opens a message (the legacy notifier's emoji per level, the information
#: badge as market-pulse-bot's blue).
BADGE = {Severity.INFO: "🔵", Severity.WARNING: "⚠️", Severity.CRITICAL: "🚨"}


@dataclass(frozen=True, slots=True)
class Alert:
    """Something the owner must hear. ``key`` names the condition: the same key is the same
    condition. ``condition``: held until it clears; otherwise an event, sent every time.
    ``clears``: key prefixes of conditions this alert resolves."""

    kind: str
    key: str
    text: str
    severity: Severity
    at: datetime
    condition: bool = True
    clears: tuple[str, ...] = ()

    def message(self) -> str:
        return f"{BADGE[self.severity]} {self.kind}: {self.text}"


def condition_key(kind: str, subject: str) -> str:
    """A key for ``kind`` about ``subject`` whose numbers (amounts, seconds) do not split it."""
    return f"{kind}:{_DIGITS.sub('#', subject)}"


#: Order alerts that leave the account at risk until someone looks.
_CRITICAL_ORDERS = frozenset(
    {
        OrderAlertKind.RESTORE_FAILED,
        OrderAlertKind.STOP_MISSING,
        OrderAlertKind.STOP_GAPPED,
        OrderAlertKind.OUTCOME_UNKNOWN,
        OrderAlertKind.RESUME_UNRESOLVED,
    }
)


def from_order(alert: OrderAlert) -> Alert:
    """An order alert's condition is its intent's, or, about the account, its words."""
    kind = f"order.{alert.kind.value}"
    key = (
        f"{kind}:{alert.intent_id}"
        if alert.intent_id is not None
        else condition_key(kind, alert.detail)
    )
    severity = Severity.CRITICAL if alert.kind in _CRITICAL_ORDERS else Severity.WARNING
    return Alert(kind, key, alert.detail, severity, alert.at)


def from_session(alert: SessionAlert) -> Alert:
    kind = f"session.{alert.kind.value}"
    if alert.kind is SessionAlertKind.REAUTHENTICATED:
        return Alert(
            kind,
            kind,
            f"logged in again ({alert.detail})",
            Severity.INFO,
            alert.at,
            condition=False,
            clears=("session.",),
        )
    severity = (
        Severity.CRITICAL
        if alert.kind is SessionAlertKind.REAUTHENTICATION_FAILED
        else Severity.WARNING
    )
    return Alert(kind, kind, alert.detail, severity, alert.at)


def from_disagreement(found: Disagreement, at: datetime) -> Alert:
    kind = "calendar.disagreement"
    text = (
        f"{found.venue} {found.day.isoformat()} {found.kind.value}: the calendar says "
        f"{found.calendar}, {found.source} says {found.observed}"
    )
    return Alert(kind, kind, text, Severity.WARNING, at, condition=False)


def from_sweep(report: SweepReport, at: datetime) -> Alert | None:
    """A sweep that cancelled, failed or could not read is news; a quiet one is not."""
    if report.error is None and not report.cancelled and not report.failed:
        return None
    parts = [f"cancelled {len(report.cancelled)}, kept {len(report.kept)}"]
    if report.failed:
        parts.append(f"failed {', '.join(report.failed)}")
    if report.error is not None:
        parts.append(report.error)
    severity = Severity.CRITICAL if report.failed or report.error else Severity.WARNING
    return Alert("kill_switch.sweep", "kill_switch.sweep", "; ".join(parts), severity, at, False)


@dataclass
class _Held:
    kind: str
    sources: set[str] = field(default_factory=set)
    sent: bool = False
    since: str = ""


class AlertBook:
    """Which conditions the owner has heard of, by key, and which jobs keep raising them."""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._held: dict[str, _Held] = self._load()

    @property
    def active(self) -> dict[str, str]:
        """Each held condition's kind, by key."""
        return {key: held.kind for key, held in sorted(self._held.items())}

    def _load(self) -> dict[str, _Held]:
        path = self._path
        if path is None or not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            held = {
                str(key): _Held(
                    kind=str(entry["kind"]),
                    sources={str(s) for s in entry["sources"]},
                    sent=bool(entry["sent"]),
                    since=str(entry["since"]),
                )
                for key, entry in data["held"].items()
            }
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError) as exc:
            quarantine = path.with_name(f"{path.name}.corrupt-{utc_stamp().replace(':', '')}")
            log.warning("the alert state cannot be read; starting empty", extra={"error": str(exc)})
            try:
                path.replace(quarantine)
            except OSError:
                log.exception("the unreadable alert state could not be set aside")
            return {}
        return held

    def _save(self) -> None:
        path = self._path
        if path is None:
            return
        data: dict[str, Any] = {
            "held": {
                key: {
                    "kind": held.kind,
                    "sources": sorted(held.sources),
                    "sent": held.sent,
                    "since": held.since,
                }
                for key, held in self._held.items()
            }
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(data, indent=2, sort_keys=True))
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        except OSError:
            log.exception("the alert state could not be saved; a restart may repeat alerts")

    def raised(self, alert: Alert, source: str) -> bool:
        """Note ``alert`` raised by ``source``; whether it should be sent now."""
        cleared = [
            key for key in self._held if any(key.startswith(prefix) for prefix in alert.clears)
        ]
        for key in cleared:
            del self._held[key]
        if not alert.condition:
            if cleared:
                self._save()
            return True
        held = self._held.get(alert.key)
        if held is None:
            held = self._held[alert.key] = _Held(kind=alert.kind, since=utc_stamp(alert.at))
        new_source = source not in held.sources
        held.sources.add(source)
        if cleared or new_source:
            self._save()
        return not held.sent

    def sent(self, key: str) -> None:
        held = self._held.get(key)
        if held is not None and not held.sent:
            held.sent = True
            self._save()

    def end_round(self, source: str, raised: Iterable[str]) -> list[str]:
        """A round of ``source`` ended having raised ``raised``: every condition it held and did not
        raise again is no longer its; one no job holds any more clears. Returns the cleared keys."""
        again = set(raised)
        cleared: list[str] = []
        changed = False
        for key in list(self._held):
            held = self._held[key]
            if source in held.sources and key not in again:
                held.sources.discard(source)
                changed = True
                if not held.sources:
                    del self._held[key]
                    cleared.append(key)
        if changed:
            self._save()
        return cleared
