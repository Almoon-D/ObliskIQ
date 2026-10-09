"""The shape of the calendar configuration: venues, their phase windows, synthetic calendars,
lunch breaks, seasonal closes, the IBKR exchange-code map and the order-window offsets.

A venue is a mapping keyed by its market code (MIC) so an owner override can change one venue.
``venues.<MIC>`` holds only what every venue has; what applies to a few venues lives in sparse
side sections keyed by the same MIC (``synthetic_calendars``, ``lunch_breaks``,
``seasonal_closes``), so no venue carries a null for a block it does not use. A venue is
synthetic exactly when its MIC appears in ``synthetic_calendars``; otherwise its MIC is the
``exchange_calendars`` name.

Times are written ``"HH:MM"`` and must be quoted: YAML reads an unquoted ``10:00`` as the number
600.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import re
from datetime import date
from itertools import pairwise
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BeforeValidator,
    NonNegativeInt,
    PositiveInt,
    StringConstraints,
    model_validator,
)

from ..domain.base import NonEmptyText
from ..domain.calendar import Mic, Region, TimeZoneName
from .base import FrozenModel

_CLOCK_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _quoted_clock_time(value: object) -> object:
    if isinstance(value, int) and not isinstance(value, bool):
        raise ValueError(
            f'is the number {value}: quote times, such as "10:00" '
            "(YAML reads an unquoted 10:00 as 600)"
        )
    return value


def _clock_time(value: str) -> str:
    if not _CLOCK_RE.fullmatch(value):
        raise ValueError("must be a 24-hour clock time written HH:MM, such as 09:30")
    return value


type ClockTime = Annotated[str, BeforeValidator(_quoted_clock_time), AfterValidator(_clock_time)]
type IbkrExchangeCode = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{2,12}$")]
type Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
type WindowPhase = Literal["pre_market", "opening_auction", "closing_auction", "post_market"]
type WindowAnchor = Literal["session_open", "session_close"]


class Provenance(FrozenModel):
    """Where a value came from, and whether it still awaits a live reading."""

    origin: NonEmptyText
    verify_live: bool


class PhaseWindowConfig(FrozenModel):
    """A phase window as minutes from the session open or close.

    Offsets follow the session: on a half day the close moves and so does a window anchored
    to it.
    """

    phase: WindowPhase
    anchor: WindowAnchor
    start_offset_minutes: int
    end_offset_minutes: int

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.end_offset_minutes <= self.start_offset_minutes:
            raise ValueError(f"{self.phase}: the end offset must be greater than the start offset")
        opening = self.phase in {"pre_market", "opening_auction"}
        if opening and self.anchor != "session_open":
            raise ValueError(f"{self.phase}: the anchor must be session_open")
        if not opening and self.anchor != "session_close":
            raise ValueError(f"{self.phase}: the anchor must be session_close")
        if opening and self.end_offset_minutes > 0:
            raise ValueError(f"{self.phase} must end no later than the session open")
        if self.phase == "closing_auction" and self.start_offset_minutes > 0:
            raise ValueError("closing_auction must start no later than the session close")
        if self.phase == "post_market" and self.start_offset_minutes < 0:
            raise ValueError("post_market must start at or after the session close")
        return self


class IncidentSourceConfig(FrozenModel):
    """What kind of incident feed a venue has, and the scope that feed may raise.

    A feed may only raise incidents within its declared scope: a single-stock halt feed never
    closes a venue. Manual entries are accepted at either scope.
    """

    type: Literal["manual", "structured_feed"]
    scope: Literal["market_wide", "single_stock"]


class VenueSources(FrozenModel):
    calendar: Provenance
    phases: Provenance


class VenueConfig(FrozenModel):
    name: NonEmptyText
    region: Region
    timezone: TimeZoneName
    phase_windows: list[PhaseWindowConfig]
    incident_source: IncidentSourceConfig
    sources: VenueSources

    @model_validator(mode="after")
    def _windows_do_not_repeat_or_overlap(self) -> Self:
        phases = [window.phase for window in self.phase_windows]
        if len(phases) != len(set(phases)):
            raise ValueError("phase_windows cannot repeat a phase")
        for anchor in ("session_open", "session_close"):
            spans = sorted(
                (w.start_offset_minutes, w.end_offset_minutes)
                for w in self.phase_windows
                if w.anchor == anchor
            )
            for (_, earlier_end), (later_start, _) in pairwise(spans):
                if later_start < earlier_end:
                    raise ValueError(f"phase windows anchored to {anchor} overlap")
        return self


class SyntheticCalendarConfig(FrozenModel):
    """A venue the library lacks: fixed hours, a weekday set and a declared holiday list.

    ``holidays_through`` is the last date the holiday list is known to cover. With ``null``
    nothing is declared, and every date is reported as not covered rather than open.
    """

    open: ClockTime
    close: ClockTime
    trading_days: list[Weekday]
    holidays: list[date]
    holidays_through: date | None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.open >= self.close:
            raise ValueError("open must be before close")
        if not self.trading_days or len(self.trading_days) != len(set(self.trading_days)):
            raise ValueError("trading_days must name each trading weekday once")
        if self.holidays_through is None and self.holidays:
            raise ValueError("a holiday list needs holidays_through, the date it is known through")
        if self.holidays_through is not None and any(
            day > self.holidays_through for day in self.holidays
        ):
            raise ValueError("a holiday falls after holidays_through")
        return self


class LunchBreakConfig(FrozenModel):
    """A lunch break the library does not carry. It applies only where the library has none."""

    start: ClockTime
    end: ClockTime
    source: Provenance

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start >= self.end:
            raise ValueError("start must be before end")
        return self


class SeasonalCloseConfig(FrozenModel):
    """A regular close that moves while another market observes daylight saving.

    B3, for example, closes earlier while New York is on daylight saving time, to keep
    overlapping with the US session. ``close`` replaces the library's close on those days.
    """

    reference_timezone: TimeZoneName
    close: ClockTime
    source: Provenance


class IbkrExchangeConfig(FrozenModel):
    """An IBKR exchange code resolved to the venue whose calendar governs it."""

    venue: Mic
    source: Provenance


class OrderOffsetsConfig(FrozenModel):
    """Orders go only in ``[open + after_open, close - before_close]`` of the regular phase."""

    after_open_minutes: NonNegativeInt
    before_close_minutes: NonNegativeInt


class CrossCheckConfig(FrozenModel):
    time_tolerance_minutes: NonNegativeInt


class IncidentsConfig(FrozenModel):
    grace_minutes: NonNegativeInt


class CalendarsConfig(FrozenModel):
    history_years: PositiveInt
    horizon_years: PositiveInt
    order_offsets: OrderOffsetsConfig
    venue_order_offsets: dict[Mic, OrderOffsetsConfig]
    cross_check: CrossCheckConfig
    incidents: IncidentsConfig
