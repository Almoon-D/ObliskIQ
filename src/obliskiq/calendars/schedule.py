"""Market schedules: when a venue's regular sessions are, on which days, from which source.

A schedule answers for the days it was built to cover and refuses the rest (``covers``): a day
outside the window is neither open nor closed but unknown, and a caller must not read that as
either. Two kinds exist, behind one interface:

* :class:`LibrarySchedule` reads an ``exchange_calendars`` calendar (sessions, holidays, early
  closes, native breaks). It may add a lunch break the library does not carry and replace a
  close that moves with another market's daylight saving time.
* :class:`SyntheticSchedule` serves a venue the library lacks from fixed hours, a weekday set
  and a declared holiday list, and covers nothing until a holiday list is declared.

Everything leaving this module is a standard-library type in UTC; the library and its pandas
objects stay here. Further sources (IBKR's trading schedule, Session 3) cross-check a schedule
through :class:`ScheduleSource`; they never replace it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import exchange_calendars as xc
from exchange_calendars import errors as xc_errors

from ..config.schema_calendars import (
    WEEKDAYS,
    LunchBreakConfig,
    SeasonalCloseConfig,
    SyntheticCalendarConfig,
)
from ..domain.calendar import SessionObservation
from ..errors import CalendarCoverageError, ConfigError, ScheduleInconsistentError

#: Where in a day to ask a zone whether daylight saving time applies: far from every changeover.
_MIDDAY = timedelta(hours=12)


@dataclass(frozen=True, slots=True)
class SessionTimes:
    """One regular session in UTC, with the break inside it if there is one."""

    day: date
    open: datetime
    close: datetime
    break_start: datetime | None
    break_end: datetime | None
    early_close: bool


class ScheduleSource(Protocol):
    """A cross-check on a schedule, such as IBKR's trading schedule for a contract.

    ``observe`` runs inside synchronous, latency-sensitive code and must not do I/O: a source
    refreshes its cache elsewhere and reads it here. ``None`` means "no information", which is
    not the same as an observation saying the day is closed.
    """

    @property
    def name(self) -> str: ...

    def observe(self, venue: str, day: date, conid: int | None) -> SessionObservation | None: ...


def _local_to_utc(day: date, clock: str, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time.fromisoformat(clock), tzinfo=zone).astimezone(UTC)


def _library_instant(value: Any) -> datetime:
    """A library timestamp as a standard-library UTC datetime."""
    instant: datetime = value.to_pydatetime()
    return instant.astimezone(UTC)


def _sanitise(
    mic: str,
    day: date,
    open_: datetime,
    close: datetime,
    break_start: datetime | None,
    break_end: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    """The break to keep, or an error when the session contradicts itself.

    A break that starts at or after the close is not part of that session (an early close
    before lunch) and is dropped. Anything else out of order is inconsistent: the session is
    not guessed at.
    """
    if open_ >= close:
        raise ScheduleInconsistentError(f"{mic} {day}: the session opens at or after it closes")
    if break_start is None or break_end is None:
        return None, None
    if break_start >= close:
        return None, None
    if not open_ < break_start < break_end < close:
        raise ScheduleInconsistentError(f"{mic} {day}: the break does not fit inside the session")
    return break_start, break_end


class MarketSchedule(ABC):
    """The regular sessions of one venue over the window it covers."""

    def __init__(self, mic: str, zone: ZoneInfo, coverage: tuple[date, date] | None) -> None:
        self.mic = mic
        self.zone = zone
        self.coverage = coverage

    def covers(self, day: date) -> bool:
        return self.coverage is not None and self.coverage[0] <= day <= self.coverage[1]

    def _require(self, day: date) -> None:
        if not self.covers(day):
            raise CalendarCoverageError(
                f"{self.mic}: {day} is outside the calendar built (covers {self.coverage})"
            )

    @abstractmethod
    def _session(self, day: date) -> SessionTimes | None: ...

    @abstractmethod
    def _is_session(self, day: date) -> bool: ...

    @abstractmethod
    def is_business_weekday(self, day: date) -> bool:
        """Is this a weekday the venue normally trades? Tells a holiday from a weekend."""

    def session(self, day: date) -> SessionTimes | None:
        """The session on ``day``; ``None`` when the venue is closed. Raises outside coverage."""
        self._require(day)
        return self._session(day)

    def is_session(self, day: date) -> bool:
        self._require(day)
        return self._is_session(day)

    def next_session(self, after: date) -> date | None:
        """The first session strictly after ``after``; ``None`` when none lies in coverage."""
        if self.coverage is None:
            return None
        day = max(after + timedelta(days=1), self.coverage[0])
        while day <= self.coverage[1]:
            if self._is_session(day):
                return day
            day += timedelta(days=1)
        return None

    def previous_session(self, before: date) -> date | None:
        """The last session strictly before ``before``; ``None`` when none lies in coverage."""
        if self.coverage is None:
            return None
        day = min(before - timedelta(days=1), self.coverage[1])
        while day >= self.coverage[0]:
            if self._is_session(day):
                return day
            day -= timedelta(days=1)
        return None

    def session_day(self, at: datetime) -> date:
        """The venue-local calendar date of an instant (a naive datetime is refused)."""
        if at.tzinfo is None:
            raise ValueError("an instant must carry a time zone; a naive datetime is ambiguous")
        return at.astimezone(self.zone).date()

    def last_completed_session(self, at: datetime) -> date | None:
        """The latest session that had closed by ``at``; ``None`` when none lies in coverage.

        A cycle that reads data "as of" a session must pass this, not today's date: a date
        equal to today would include what is collected after the close.
        """
        day = self.session_day(at)
        self._require(day)
        today = self._session(day)
        if today is not None and today.close <= at:
            return day
        return self.previous_session(day)

    def next_open(self, at: datetime) -> datetime | None:
        """The first session open strictly after ``at``; ``None`` past the covered window."""
        day = self.session_day(at)
        self._require(day)
        today = self._session(day)
        if today is not None and today.open > at:
            return today.open
        following = self.next_session(day)
        return None if following is None else self._session_times(following).open

    def next_close(self, at: datetime) -> datetime | None:
        """The first session close strictly after ``at``; ``None`` past the covered window."""
        day = self.session_day(at)
        self._require(day)
        today = self._session(day)
        if today is not None and today.close > at:
            return today.close
        following = self.next_session(day)
        return None if following is None else self._session_times(following).close

    def _session_times(self, day: date) -> SessionTimes:
        times = self._session(day)
        assert times is not None  # a day next_session returned is a session
        return times

    def sessions_between(self, start: date, end: date) -> list[date]:
        """Every session day from ``start`` to ``end`` inclusive; both must be covered."""
        self._require(start)
        self._require(end)
        days: list[date] = []
        day = start
        while day <= end:
            if self._is_session(day):
                days.append(day)
            day += timedelta(days=1)
        return days


class LibrarySchedule(MarketSchedule):
    """A schedule read from an ``exchange_calendars`` calendar."""

    def __init__(
        self,
        mic: str,
        zone: ZoneInfo,
        calendar: xc.ExchangeCalendar,
        lunch: LunchBreakConfig | None,
        seasonal: SeasonalCloseConfig | None,
    ) -> None:
        coverage = (calendar.first_session.date(), calendar.last_session.date())
        super().__init__(mic, zone, coverage)
        self._calendar = calendar
        self._lunch = lunch
        self._seasonal = seasonal
        self._early_closes = {stamp.date() for stamp in calendar.early_closes}
        if lunch is not None and bool(calendar.has_break):
            raise ConfigError(
                f"lunch_breaks.{mic}: the library now carries a break for {mic}; "
                "remove the override"
            )
        if seasonal is not None and not self._seasonal_days_differ():
            raise ConfigError(
                f"seasonal_closes.{mic}: the library already closes at {seasonal.close} while "
                f"{seasonal.reference_timezone} observes daylight saving time; remove the override"
            )

    @classmethod
    def build(
        cls,
        mic: str,
        timezone: str,
        start: date,
        end: date,
        lunch: LunchBreakConfig | None,
        seasonal: SeasonalCloseConfig | None,
    ) -> LibrarySchedule | None:
        """The schedule for ``start``..``end``, clamped to what the library can build.

        ``None`` when the library cannot build any of that window (a precomputed calendar whose
        holidays end before ``start``): the venue then covers nothing. An unknown calendar name
        or a time zone that disagrees with the library's is a configuration error.
        """
        calendar = _library_calendar(mic, start, end)
        if calendar is None:
            return None
        if str(calendar.tz) != timezone:
            raise ConfigError(
                f"venues.{mic}.timezone: {timezone} is not the library's time zone "
                f"{calendar.tz} for {mic}"
            )
        return cls(mic, ZoneInfo(timezone), calendar, lunch, seasonal)

    def _seasonal_days_differ(self) -> bool:
        """Does the override change the close on at least one covered day?"""
        assert self._seasonal is not None
        assert self.coverage is not None
        for day in self.sessions_between(*self.coverage):
            if day in self._early_closes or not self._reference_in_dst(day):
                continue
            library_close = _library_instant(self._calendar.session_close(day))
            if library_close != _local_to_utc(day, self._seasonal.close, self.zone):
                return True
        return False

    def _reference_in_dst(self, day: date) -> bool:
        assert self._seasonal is not None
        zone = ZoneInfo(self._seasonal.reference_timezone)
        midday = datetime.combine(day, time.min, tzinfo=zone) + _MIDDAY
        return bool(midday.dst())

    def is_business_weekday(self, day: date) -> bool:
        return bool(self._calendar.weekmask[day.weekday()] == "1")

    def _is_session(self, day: date) -> bool:
        return bool(self._calendar.is_session(day))

    def _session(self, day: date) -> SessionTimes | None:
        if not self._is_session(day):
            return None
        calendar = self._calendar
        open_ = _library_instant(calendar.session_open(day))
        close = _library_instant(calendar.session_close(day))
        early = day in self._early_closes
        if self._seasonal is not None and not early and self._reference_in_dst(day):
            # An override can only shorten the day: one later than the library's close would
            # open time the library does not.
            close = min(close, _local_to_utc(day, self._seasonal.close, self.zone))
        break_start: datetime | None = None
        break_end: datetime | None = None
        if calendar.session_has_break(day):
            break_start = _library_instant(calendar.session_break_start(day))
            break_end = _library_instant(calendar.session_break_end(day))
        elif self._lunch is not None:
            break_start = _local_to_utc(day, self._lunch.start, self.zone)
            break_end = _local_to_utc(day, self._lunch.end, self.zone)
        break_start, break_end = _sanitise(self.mic, day, open_, close, break_start, break_end)
        return SessionTimes(day, open_, close, break_start, break_end, early)


def _library_calendar(mic: str, start: date, end: date) -> xc.ExchangeCalendar | None:
    """The library's calendar for the window, or the part of it the library supports."""
    try:
        return xc.get_calendar(mic, start=start, end=end)
    except xc_errors.InvalidCalendarName:
        raise ConfigError(f"venues.{mic}: {mic} is not an exchange_calendars calendar") from None
    except ValueError:
        pass
    # The window leaves the library's bounds (a precomputed calendar ends with its last
    # recorded year): build what it does support.
    bounds = type(xc.get_calendar(mic))
    lower, upper = bounds.bound_min(), bounds.bound_max()
    first = start if lower is None else max(start, lower.date())
    last = end if upper is None else min(end, upper.date())
    if first >= last:
        return None
    return xc.get_calendar(mic, start=first, end=last)


class SyntheticSchedule(MarketSchedule):
    """A schedule for a venue the library lacks: fixed hours and a declared holiday list."""

    def __init__(
        self,
        mic: str,
        timezone: str,
        config: SyntheticCalendarConfig,
        lunch: LunchBreakConfig | None,
        start: date,
        end: date,
    ) -> None:
        through = config.holidays_through
        coverage = (
            None if through is None or start > min(end, through) else (start, min(end, through))
        )
        super().__init__(mic, ZoneInfo(timezone), coverage)
        self._config = config
        self._lunch = lunch
        self._trading_weekdays = frozenset(WEEKDAYS.index(name) for name in config.trading_days)
        self._holidays = frozenset(config.holidays)

    def is_business_weekday(self, day: date) -> bool:
        return day.weekday() in self._trading_weekdays

    def _is_session(self, day: date) -> bool:
        return self.is_business_weekday(day) and day not in self._holidays

    def _session(self, day: date) -> SessionTimes | None:
        if not self._is_session(day):
            return None
        open_ = _local_to_utc(day, self._config.open, self.zone)
        close = _local_to_utc(day, self._config.close, self.zone)
        break_start = break_end = None
        if self._lunch is not None:
            break_start = _local_to_utc(day, self._lunch.start, self.zone)
            break_end = _local_to_utc(day, self._lunch.end, self.zone)
        break_start, break_end = _sanitise(self.mic, day, open_, close, break_start, break_end)
        return SessionTimes(day, open_, close, break_start, break_end, False)


class UncoveredSchedule(MarketSchedule):
    """A venue whose calendar covers none of the window: every date is reported not covered."""

    def __init__(self, mic: str, zone: ZoneInfo) -> None:
        super().__init__(mic, zone, None)

    def _session(self, day: date) -> SessionTimes | None:
        raise CalendarCoverageError(f"{self.mic}: {day} is not covered")

    def _is_session(self, day: date) -> bool:
        raise CalendarCoverageError(f"{self.mic}: {day} is not covered")

    def is_business_weekday(self, day: date) -> bool:
        raise CalendarCoverageError(f"{self.mic}: {day} is not covered")
