"""The reporter: alerts through the alert book to the notifier, and the service's reports.

The order path and the broker session raise alerts from synchronous hooks inside the event loop,
so :meth:`Reporter.push` only queues; :meth:`Reporter.flush` sends what is due. The service
flushes after each job and as soon as the keepalive raises something, so a failed re-login
reaches the owner within one heartbeat (DECISIONS D68). Each queued alert carries the job it was
raised in (``source``), which is how a condition clears when that job's next round no longer
raises it (:class:`~obliskiq.reporting.alerts.AlertBook`).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass

from ..logging_setup import get_logger
from .alerts import Alert, AlertBook
from .notifier import Notifier

log = get_logger("reporting")


@dataclass(frozen=True, slots=True)
class _Queued:
    alert: Alert
    source: str


class Reporter:
    """Alerts, summaries and digests to the owner."""

    def __init__(self, notifier: Notifier, book: AlertBook) -> None:
        self._notifier = notifier
        self._book = book
        self._queue: list[_Queued] = []
        self._raised: dict[str, set[str]] = {}

    @property
    def book(self) -> AlertBook:
        return self._book

    def push(self, alert: Alert, source: str) -> None:
        """Queue an alert raised by ``source`` (synchronous, for the order path's hooks)."""
        self._queue.append(_Queued(alert, source))
        if alert.condition:
            self._raised.setdefault(source, set()).add(alert.key)

    async def flush(self) -> int:
        """Send every queued alert that is due. Returns how many were delivered."""
        queued, self._queue = self._queue, []
        delivered = 0
        for item in queued:
            alert = item.alert
            if not self._book.raised(alert, item.source):
                continue
            if await self._notifier.send(f"ObliskIQ alert: {alert.kind}", alert.message()):
                delivered += 1
                if alert.condition:
                    self._book.sent(alert.key)
        return delivered

    async def end_round(self, source: str) -> list[str]:
        """``source``'s round is over: send its alerts, and clear the conditions it held and no
        longer raised."""
        await self.flush()
        cleared = self._book.end_round(source, self._raised.pop(source, set()))
        for key in cleared:
            log.info("an alert's condition cleared", extra={"key": key, "source": source})
        return cleared

    async def report(self, subject: str, body: str) -> bool:
        """A run summary or a digest."""
        return await self._notifier.send(subject, body)
