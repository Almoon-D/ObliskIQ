"""What the calendar layer answers: statuses, windows and the reason a venue is blocked.

A status is ``ok`` or ``blocked(reason)``. Every reason is a stable member of
:class:`BlockReason`, so a pre-trade check can test one reason code per way a venue can be
blocked. Nothing here is ever "unknown, so open".

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from ..domain.calendar import Incident, TimeWindow


class BlockReason(StrEnum):
    UNKNOWN_VENUE = "unknown_venue"
    EXCHANGE_UNMAPPED = "exchange_unmapped"
    CALENDAR_NOT_COVERED = "calendar_not_covered"
    NOT_A_SESSION = "not_a_session"
    SCHEDULE_INCONSISTENT = "schedule_inconsistent"
    NO_TRADABLE_WINDOW = "no_tradable_window"
    INCIDENT_OVERLAP = "incident_overlap"
    INCIDENT_FILE_INVALID = "incident_file_invalid"
    SOURCE_DISAGREEMENT = "source_disagreement"
    OUTSIDE_WINDOW = "outside_window"


class DisagreementKind(StrEnum):
    IS_SESSION = "is_session"
    OPEN = "open"
    CLOSE = "close"


@dataclass(frozen=True, slots=True)
class Disagreement:
    """A cross-check source and the calendar differ about a venue's regular session."""

    venue: str
    day: date
    kind: DisagreementKind
    source: str
    calendar: str
    observed: str


@dataclass(frozen=True, slots=True)
class VenueStatus:
    """Is a venue available for trading on a day?

    This is the planning view: any incident overlapping the session blocks the day, with
    ``partial`` telling whether some tradable time remains. Whether now is tradable is
    :class:`Tradability`.
    """

    venue: str
    day: date
    reason: BlockReason | None
    detail: str = ""
    partial: bool = False
    incidents: tuple[Incident, ...] = ()
    disagreements: tuple[Disagreement, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.reason is None


@dataclass(frozen=True, slots=True)
class Tradability:
    """Can an order go to a venue at an instant? The act-now view."""

    venue: str
    at: datetime
    reason: BlockReason | None
    detail: str = ""
    window: TimeWindow | None = None

    @property
    def ok(self) -> bool:
        return self.reason is None


@dataclass(frozen=True, slots=True)
class SessionBounds:
    """A session's open and close, in the venue's local time and in UTC."""

    venue: str
    day: date
    open_local: datetime
    close_local: datetime
    open_utc: datetime
    close_utc: datetime
    break_start_utc: datetime | None
    break_end_utc: datetime | None
    early_close: bool


@dataclass(frozen=True, slots=True)
class SkippedSession:
    venue: str
    day: date
    reason: BlockReason


@dataclass(frozen=True, slots=True)
class LastTradableSession:
    """The last session on or before an expiry on which both venues can trade.

    ``skipped`` lists every day between it and the expiry, with the venue and the reason that
    ruled each out, so an exit scheduled against it can say why it is earlier than the expiry.
    """

    session: date
    expiry: date
    skipped: tuple[SkippedSession, ...]
