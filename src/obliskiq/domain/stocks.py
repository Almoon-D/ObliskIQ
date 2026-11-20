"""What the stock collector keeps for the thesis book's stock screen (MASTER_PLAN §5.5; DECISIONS
D180): the SEC's listings, snapshot by snapshot, the names that left them, each company's SIC,
the XBRL frame values the screen reads, and Yahoo's daily bars.

Every record carries ``known_at``, the instant it was collected: a read as of a date sees only
what had been collected by its end. Nothing here is a price history before the first run.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date

from pydantic import Field, NonNegativeFloat, PositiveInt

from .base import DomainModel, NonEmptyText, UtcDatetime
from .calendar import Mic


class StockListing(DomainModel):
    """One ticker the SEC lists for a company on a kept exchange."""

    cik: PositiveInt
    ticker: NonEmptyText
    name: NonEmptyText
    #: The SEC's own word for the exchange (``NYSE``, ``Nasdaq``).
    exchange: NonEmptyText
    #: The venue that exchange trades on (``XNYS``).
    venue: Mic


class ListingSnapshot(DomainModel):
    """One read of the SEC's listings: when, how many names it kept, and what was downloaded."""

    snapshot_id: PositiveInt
    known_at: UtcDatetime
    listed: PositiveInt
    content_sha256: NonEmptyText


class LeftListing(DomainModel):
    """A ticker in one snapshot and absent from the next: it left the SEC's list. Never a
    delisting return, never a price."""

    cik: PositiveInt
    ticker: NonEmptyText
    #: The last snapshot that listed it, and the first that did not.
    last_seen_at: UtcDatetime
    left_at: UtcDatetime


class StockFact(DomainModel):
    """One value of an XBRL frame for one company, as read on ``known_at``. A later read that
    differs is a new record; nothing is overwritten."""

    cik: PositiveInt
    #: The screen's input (``earnings_per_share``, ``gross_profit``, ``assets``).
    input: NonEmptyText
    #: The frame's calendar period (``CY2025``, ``CY2026Q3I``).
    period: NonEmptyText
    #: The day the reported period ended.
    period_end: date
    value: float
    accession: NonEmptyText
    known_at: UtcDatetime


class StockBar(DomainModel):
    """One session of a listed name as Yahoo served it: the close (split-adjusted as of the
    read), the volume, and the session's total return from the session before it, computed from
    the adjusted closes of one answer (``None`` when the answer held no session before it)."""

    symbol: NonEmptyText
    session: date
    close: float = Field(gt=0)
    volume: NonNegativeFloat | None
    change: float | None = Field(default=None, gt=-1)
    known_at: UtcDatetime
