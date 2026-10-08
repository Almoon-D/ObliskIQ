"""Trading-day arithmetic, with holidays.

The mandatory close date is ``tenor.mandatory_close_trading_days`` trading days
before expiry, and it is written to the ledger at ENTRY. It therefore has to be
right when it is written, months before anyone looks at it again. Counting back
ten weekdays from a December expiry and landing on Christmas Day produces a date
the market cannot honour, and IBKR force-liquidates a non-compliant position at
market — so the close date is inherited rather than controlled, which is the one
outcome the rule exists to prevent.

Holiday lists are reference data and live in ``calendars.yaml``, addressed by
``calendars.path``. A venue with no declared list resolves to the configured
default, weekends only, and **every date computed that way says so**: the result
carries ``holidays_declared=False`` and the calendar id it used. An undeclared
calendar is visibly undeclared rather than silently assumed to trade through
Good Friday.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from ..config import Config
from ..errors import ConfigError
from ..logging_setup import get_logger
from ..util.timeutil import parse_date

log = get_logger("compute.calendar")

#: Saturday and Sunday as ``date.weekday()`` reports them. Overridable per file
#: via ``weekend_days``, because not every exchange rests on the same days.
DEFAULT_WEEKEND_DAYS = (5, 6)

#: A runaway guard on the day-by-day walk. A year of holidays cannot push ten
#: trading days more than this far back, so hitting it means the calendar is
#: malformed — every day a holiday — and the caller must hear about it rather
#: than loop.
MAX_CALENDAR_WALK_DAYS = 3650


@dataclass(frozen=True)
class TradingCalendar:
    """One venue's sessions."""

    calendar_id: str
    holidays: frozenset[date]
    weekend_days: frozenset[int]
    description: str | None = None

    @property
    def holidays_declared(self) -> bool:
        """False when this calendar knows only about weekends."""
        return bool(self.holidays)

    @property
    def declared_years(self) -> frozenset[int]:
        """The years the holiday list actually covers."""
        return frozenset(day.year for day in self.holidays)

    def covers(self, day: date) -> bool:
        """Does the declared list say anything about ``day``'s year?

        ``holidays_declared`` asks whether a list exists at all, which a date in
        an uncovered year passes trivially: the 2026-2027 lists in calendars.yaml
        made a 2028 expiry read as computed against a declared calendar while
        every 2028 holiday was silently absent. That is the Christmas-Day close
        date the file's own header says it exists to prevent, arriving two years
        later.
        """
        return day.year in self.declared_years

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() not in self.weekend_days and day not in self.holidays

    def previous_trading_day(self, day: date) -> date:
        """The latest session strictly before ``day``."""
        cursor = day - timedelta(days=1)
        for _ in range(MAX_CALENDAR_WALK_DAYS):
            if self.is_trading_day(cursor):
                return cursor
            cursor -= timedelta(days=1)
        raise ConfigError(
            f"calendar {self.calendar_id!r} has no trading day within "
            f"{MAX_CALENDAR_WALK_DAYS} days before {day.isoformat()}; the holiday list "
            "is malformed."
        )

    def subtract_trading_days(self, day: date, count: int) -> date:
        """Step back ``count`` sessions from ``day``.

        ``day`` itself is not counted, whether or not it is a session: "ten
        trading days before expiry" means ten sessions on which the position
        could still be closed, and expiry is not one of them.
        """
        if count < 0:
            raise ValueError(f"count must not be negative, got {count}")
        cursor = day
        for _ in range(count):
            cursor = self.previous_trading_day(cursor)
        return cursor

    def trading_days_between(self, start: date, end: date) -> int:
        """Sessions in ``(start, end]``. Negative when ``end`` precedes ``start``."""
        if end < start:
            return -self.trading_days_between(end, start)
        count = 0
        cursor = start
        while cursor < end:
            cursor += timedelta(days=1)
            if self.is_trading_day(cursor):
                count += 1
        return count


@dataclass(frozen=True)
class CloseDate:
    """A computed close date and the calendar that produced it."""

    close_date: date
    calendar_id: str
    trading_days_before: int
    #: False when the venue had no declared holiday list and only weekends were
    #: skipped. The caller must surface this: a date computed on weekends alone
    #: can land on a market holiday.
    holidays_declared: bool
    #: False when a list exists but says nothing about the years this date spans.
    #: Distinct from the flag above, and the distinction is the point: a list
    #: covering 2026-2027 is "declared" for a 2028 expiry while holding no 2028
    #: holidays, so the date would read as calendar-checked and be computed on
    #: weekends alone.
    years_covered: bool = True

    @property
    def provenance(self) -> str:
        if not self.holidays_declared:
            note = " (weekends only; no holiday list declared)"
        elif not self.years_covered:
            note = (
                " (WEEKENDS ONLY IN PRACTICE: the holiday list declares no days in the "
                "years this date spans, so no holiday was skipped)"
            )
        else:
            note = ""
        return (
            f"{self.trading_days_before} trading days before expiry on calendar "
            f"{self.calendar_id}" + note
        )


class TradingCalendars:
    """Every declared calendar, and the exchange mapping onto them."""

    def __init__(
        self,
        calendars: dict[str, TradingCalendar],
        mapping: dict[str, str],
        default_id: str,
    ) -> None:
        if default_id not in calendars:
            raise ConfigError(
                f"calendars.default is {default_id!r}, which is not declared in the "
                f"calendar file. Declared: {sorted(calendars)}."
            )
        self._calendars = calendars
        self._mapping = {k.upper(): v for k, v in mapping.items()}
        self._default_id = default_id

    @classmethod
    def from_config(cls, config: Config) -> TradingCalendars:
        path = _resolve_path(config.require_str("calendars.path"), config)
        default_id = config.require_str("calendars.default")
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"calendar file {path} is not valid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"calendar file {path} must be a mapping at the top level")

        weekend = frozenset(int(day) for day in (raw.get("weekend_days") or DEFAULT_WEEKEND_DAYS))
        declared = raw.get("calendars") or {}
        if not isinstance(declared, dict):
            raise ConfigError(f"{path}: calendars must be a mapping of id to spec")

        calendars: dict[str, TradingCalendar] = {}
        for name, spec in declared.items():
            if not isinstance(spec, dict):
                raise ConfigError(f"{path}: calendars.{name} is not a mapping")
            holidays: set[date] = set()
            for entry in spec.get("holidays") or []:
                parsed = entry if isinstance(entry, date) else parse_date(str(entry))
                if parsed is None:
                    raise ConfigError(f"{path}: calendars.{name} has unparseable date {entry!r}")
                holidays.add(parsed)
            calendars[str(name)] = TradingCalendar(
                calendar_id=str(name),
                holidays=frozenset(holidays),
                weekend_days=weekend,
                description=spec.get("description"),
            )

        mapping = {str(k): str(v) for k, v in (raw.get("mapping") or {}).items()}
        unknown = sorted({v for v in mapping.values() if v not in calendars})
        if unknown:
            raise ConfigError(
                f"{path}: the exchange mapping points at undeclared calendars {unknown!r}."
            )
        return cls(calendars, mapping, default_id)

    @property
    def default(self) -> TradingCalendar:
        return self._calendars[self._default_id]

    def by_id(self, calendar_id: str) -> TradingCalendar:
        try:
            return self._calendars[calendar_id]
        except KeyError as exc:
            raise ConfigError(
                f"no calendar {calendar_id!r} is declared; declared: {sorted(self._calendars)}"
            ) from exc

    def for_exchange(self, exchange: str | None) -> TradingCalendar:
        """The calendar for a venue, or the default when none is mapped."""
        if not exchange:
            return self.default
        mapped = self._mapping.get(str(exchange).upper())
        if mapped is None:
            log.debug(
                "no calendar declared for this exchange; falling back to weekends only",
                extra={"exchange": exchange, "default": self._default_id},
            )
            return self.default
        return self._calendars[mapped]

    def close_date(self, expiry: date, *, trading_days: int, exchange: str | None) -> CloseDate:
        """The mandatory close date for a contract expiring on ``expiry``."""
        calendar = self.for_exchange(exchange)
        close = calendar.subtract_trading_days(expiry, trading_days)
        # Both ends of the walk, because a close date in December can be counted
        # back from an expiry in January of the year the list does cover.
        covered = calendar.covers(expiry) and calendar.covers(close)
        if calendar.holidays_declared and not covered:
            log.warning(
                "the holiday list declares no days in the years this close date spans, so "
                "it was computed on weekends alone",
                extra={
                    "calendar": calendar.calendar_id,
                    "expiry": expiry.isoformat(),
                    "close_date": close.isoformat(),
                    "declared_years": sorted(calendar.declared_years),
                },
            )
        return CloseDate(
            close_date=close,
            calendar_id=calendar.calendar_id,
            trading_days_before=trading_days,
            holidays_declared=calendar.holidays_declared,
            years_covered=covered,
        )


def _resolve_path(declared: str, config: Config) -> Path:
    """Resolve the calendar file relative to the working directory, then to config.

    The server and the collectors run from different working directories on a
    deployed VM, so a bare relative path resolves against ``config.yaml``'s own
    location as a fallback rather than failing on cwd alone.
    """
    candidate = Path(declared).expanduser()
    if candidate.is_file():
        return candidate
    source: Any = config.source
    if source is not None:
        beside = Path(source).parent / declared
        if beside.is_file():
            return beside
    raise ConfigError(
        f"calendar file not found: {declared}. The mandatory close date needs a "
        "trading calendar; see calendars.path in config.yaml."
    )
