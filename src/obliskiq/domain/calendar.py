"""Venues, trading phases, schedule observations and incidents.

A venue is identified by its ISO 10383 market identifier code. Times are UTC instants; the
local wall clock of a venue comes from its time zone at the edges, never from a constant.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import re
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, model_validator

from .base import Conid, DomainModel, NonEmptyText, UtcDatetime

_MIC_RE = re.compile(r"[A-Z0-9]{4}")


def _mic(value: str) -> str:
    if not _MIC_RE.fullmatch(value):
        raise ValueError("must be a four-character market code in capitals, such as XNYS")
    return value


def _time_zone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError, ValueError:
        raise ValueError(f"unknown IANA time zone {value!r}") from None
    return value


type Mic = Annotated[str, AfterValidator(_mic)]
type TimeZoneName = Annotated[str, AfterValidator(_time_zone)]


class Region(StrEnum):
    AMERICA = "America"
    EUROPE = "Europe"
    ASIA = "Asia"
    OCEANIA = "Oceania"


class Venue(DomainModel):
    """A trading venue's identity."""

    mic: Mic
    name: NonEmptyText
    region: Region
    timezone: TimeZoneName

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class PhaseKind(StrEnum):
    """What a venue is doing during an interval of a day."""

    PRE_MARKET = "pre_market"
    OPENING_AUCTION = "opening_auction"
    REGULAR = "regular"
    LUNCH_BREAK = "lunch_break"
    CLOSING_AUCTION = "closing_auction"
    POST_MARKET = "post_market"
    CLOSED = "closed"


class TimeWindow(DomainModel):
    """A half-open interval ``[start, end)`` of UTC instants, never empty."""

    start: UtcDatetime
    end: UtcDatetime

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start >= self.end:
            raise ValueError("a window must start before it ends")
        return self

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def contains(self, instant: datetime) -> bool:
        return self.start <= instant < self.end


class Phase(DomainModel):
    """One phase of a venue's day over a half-open interval of UTC instants."""

    kind: PhaseKind
    start: UtcDatetime
    end: UtcDatetime

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start >= self.end:
            raise ValueError("a phase must start before it ends")
        return self

    @property
    def window(self) -> TimeWindow:
        return TimeWindow(start=self.start, end=self.end)

    def contains(self, instant: datetime) -> bool:
        return self.start <= instant < self.end


class SessionObservation(DomainModel):
    """What one source says about a venue's regular session on a day.

    Only the regular session is compared across sources: product hours differ (index options
    run past the cash close) and would read as a permanent disagreement. A source with no
    information returns no observation at all; that is not the same as a closed day.
    """

    venue: Mic
    day: date
    source: NonEmptyText
    is_session: bool
    open: UtcDatetime | None
    close: UtcDatetime | None

    @model_validator(mode="after")
    def _times_belong_to_a_session(self) -> Self:
        if not self.is_session:
            if self.open is not None or self.close is not None:
                raise ValueError("a closed day has no open or close")
        elif (self.open is None) != (self.close is None):
            raise ValueError("open and close are given together or not at all")
        elif self.open is not None and self.close is not None and self.open >= self.close:
            raise ValueError("a session must open before it closes")
        return self


class IncidentSource(StrEnum):
    MANUAL = "manual"
    FEED = "feed"


class IncidentScope(StrEnum):
    VENUE = "venue"
    INSTRUMENT = "instrument"


class Incident(DomainModel):
    """A halt or exceptional closure. An incident can block trading, never open it.

    ``expected_end`` ``None`` means open-ended: the incident blocks until it is removed.
    ``origin`` names where the entry came from (a file, a feed) so that source can replace its
    own entries wholesale. An instrument-scoped incident still names the instrument's venue.
    """

    incident_id: NonEmptyText
    origin: NonEmptyText
    source: IncidentSource
    scope: IncidentScope
    venue: Mic
    conid: Conid | None
    start: UtcDatetime
    expected_end: UtcDatetime | None
    reason: NonEmptyText

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.scope is IncidentScope.INSTRUMENT and self.conid is None:
            raise ValueError("an instrument incident names the instrument's conid")
        if self.scope is IncidentScope.VENUE and self.conid is not None:
            raise ValueError("a venue incident names no instrument")
        if self.expected_end is not None and self.expected_end <= self.start:
            raise ValueError("expected_end must be after start")
        return self
