"""IBKR's trading schedule as a cross-check on the calendars (a ``ScheduleSource``).

``GET /contract/trading-schedule?conid=&exchange=`` returns, for a few days around today, each
day's ``liquid_hours`` (the regular session, epoch seconds) and ``extended_hours``; IBKR omits
non-trading days (docs/knowledge/ibkr.md). Only the regular session is compared, as the calendars
require.

The calendars ask synchronously, inside latency-sensitive code, so
:meth:`IbkrTradingSchedule.observe` only reads a cache; :meth:`IbkrTradingSchedule.refresh` fills
it asynchronously from reference contracts, one per venue, and
:meth:`IbkrTradingSchedule.update` replaces a venue's entries in one assignment.
``None`` means "no information" — a venue never refreshed, a day outside the returned
span, a skipped venue — and never "closed". A weekday missing inside the returned span reads as
closed when ``missing_weekday_is_closed`` says IBKR omits holidays (VERIFY-LIVE until a holiday is
seen). Venues whose IBKR hours are a product's rather than the venue's (Eurex options against the
XEUR calendar) are skipped, so they do not disagree every day.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping
from datetime import date, datetime, timedelta

from ..config.schema_ibkr import IbkrConfig
from ..domain.calendar import SessionObservation
from ..errors import ObliskError
from ..logging_setup import get_logger
from .client import Contracts
from .models import TradingSchedule

log = get_logger("ibkr.schedule")

SOURCE_NAME = "ibkr_trading_schedule"

#: Saturday and Sunday in ``date.weekday()`` numbering: never read as a missing session.
_WEEKEND = frozenset({5, 6})


def observations(
    venue: str, schedule: TradingSchedule, *, missing_is_closed: bool
) -> dict[date, SessionObservation]:
    """One observation per day the schedule covers: the day's liquid hours, or closed."""
    spans: dict[date, tuple[datetime, datetime]] = {}
    for session in schedule.sessions:
        opened, closed = spans.get(session.day, (session.open, session.close))
        spans[session.day] = (min(opened, session.open), max(closed, session.close))
    out = {
        day: SessionObservation(
            venue=venue, day=day, source=SOURCE_NAME, is_session=True, open=opened, close=closed
        )
        for day, (opened, closed) in spans.items()
    }
    if missing_is_closed and schedule.first_day is not None and schedule.last_day is not None:
        day = schedule.first_day
        while day <= schedule.last_day:
            if day not in out and day.weekday() not in _WEEKEND:
                out[day] = SessionObservation(
                    venue=venue,
                    day=day,
                    source=SOURCE_NAME,
                    is_session=False,
                    open=None,
                    close=None,
                )
            day += timedelta(days=1)
    return out


class IbkrTradingSchedule:
    """A cache of IBKR's regular sessions per venue (and per registered contract)."""

    def __init__(self, ibkr: IbkrConfig) -> None:
        self._ibkr = ibkr
        self._skip = frozenset(ibkr.trading_schedule.skip_venues)
        self._by_venue: dict[str, dict[date, SessionObservation]] = {}
        self._by_conid: dict[int, dict[date, SessionObservation]] = {}

    @property
    def name(self) -> str:
        return SOURCE_NAME

    def observe(self, venue: str, day: date, conid: int | None) -> SessionObservation | None:
        """Read only: the cached observation, or ``None`` for no information."""
        if venue in self._skip:
            return None
        if conid is not None and conid in self._by_conid:
            return self._by_conid[conid].get(day)
        return self._by_venue.get(venue, {}).get(day)

    def update(self, venue: str, schedule: TradingSchedule, *, for_contract: bool = False) -> None:
        """Replace what is known for ``venue`` (or, ``for_contract``, for the schedule's conid)."""
        found = observations(
            venue, schedule, missing_is_closed=self._ibkr.trading_schedule.missing_weekday_is_closed
        )
        if for_contract:
            self._by_conid = {**self._by_conid, schedule.conid: found}
        else:
            self._by_venue = {**self._by_venue, venue: found}

    async def refresh(
        self, contracts: Contracts, references: Mapping[str, tuple[int, str | None]]
    ) -> list[str]:
        """Refresh each venue from its reference contract ``(conid, exchange)``.

        A venue whose refresh fails keeps what it had; the failures are returned by venue.
        """
        failed: list[str] = []
        for venue, (conid, exchange) in sorted(references.items()):
            if venue in self._skip:
                continue
            try:
                schedule = await contracts.trading_schedule(conid, exchange=exchange)
            except ObliskError as exc:
                log.warning(
                    "trading schedule refresh failed", extra={"venue": venue, "error": str(exc)}
                )
                failed.append(venue)
                continue
            self.update(venue, schedule)
        return failed
