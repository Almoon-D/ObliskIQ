"""What the events collector keeps from the SEC's filings (MASTER_PLAN §5.5; DECISIONS D189,
D200): each filing of a kept form as the daily index lists it, an 8-K's item numbers as its
header states them, an initial public offering's terms as its pricing prospectus states them,
and the sections of a company's reports read for a dossier.

Every record carries ``known_at``, the instant it was collected: a read as of a date sees only
what had been collected by its end. A term the text does not state is ``None``, never estimated.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Collection, Iterable
from datetime import date
from decimal import Decimal
from typing import Final, Literal, Self

from pydantic import Field, PositiveInt, model_validator

from .base import DomainModel, NonEmptyText, UtcDatetime

#: The fact sheet's section of the events, as a rendered line names it.
EVENTS_SECTION: Final = "events"
#: A rendered fact line: reference, day, status, section, text.
_FIELDS: Final = 5

#: What a pricing prospectus was read as: an initial public offering's, another offering's, or
#: one that could not be fetched or read (``detail`` says why).
type ProspectusStatus = Literal["ipo", "not_ipo", "failed"]
#: What one section of a report came to: its text, a heading the text does not hold, or a
#: document that could not be fetched or read.
type TextStatus = Literal["ok", "not_found", "failed"]


class Filing(DomainModel):
    """One filing of a kept form, as the day's index lists it; an 8-K's ``items`` and the
    acceptance instant come from its header (``None`` when the header was not read)."""

    accession: NonEmptyText
    cik: PositiveInt
    company: NonEmptyText
    form: NonEmptyText
    filed: date
    path: NonEmptyText
    items: tuple[NonEmptyText, ...] | None = None
    accepted_at: UtcDatetime | None = None
    known_at: UtcDatetime


class IpoTerms(DomainModel):
    """A pricing prospectus as read: for an initial public offering, the offer price, the shares
    and the shortest lock-up it states, with the day that lock-up ends counted from the
    prospectus's date. A lock-up the text does not state has no length and no end."""

    accession: NonEmptyText
    cik: PositiveInt
    status: ProspectusStatus
    price: Decimal | None = Field(default=None, gt=0)
    shares: PositiveInt | None = None
    lockup_days: PositiveInt | None = None
    prospectus_day: date | None = None
    lockup_ends: date | None = None
    detail: str | None = None
    known_at: UtcDatetime

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.lockup_days is None) != (self.lockup_ends is None):
            raise ValueError("a lock-up's end is stated with its length, or neither is")
        if self.status != "ipo" and any(
            value is not None for value in (self.price, self.shares, self.lockup_days)
        ):
            raise ValueError("only an initial public offering carries offering terms")
        if self.status == "failed" and not self.detail:
            raise ValueError("a prospectus that could not be read says why")
        return self


class FilingText(DomainModel):
    """One section of a report as read for a dossier (``text`` cut to the configured length)."""

    accession: NonEmptyText
    cik: PositiveInt
    form: NonEmptyText
    filed: date
    section: NonEmptyText
    status: TextStatus
    text: str | None = None
    detail: str | None = None
    known_at: UtcDatetime

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.status == "ok") != bool(self.text):
            raise ValueError("a section read is its text; one not read holds none")
        if self.status != "ok" and not self.detail:
            raise ValueError("a section not read says why")
        return self


def event_tags(rendered: Iterable[str], kinds: Collection[str]) -> tuple[str, ...]:
    """The kinds of the event lines among ``rendered`` fact lines (``F3 | day | status | events |
    results: ...``), once each in the order cited: the tags a call made from them carries."""
    found: list[str] = []
    for line in rendered:
        parts = line.split(" | ", _FIELDS - 1)
        if len(parts) != _FIELDS or parts[3] != EVENTS_SECTION:
            continue
        kind = parts[4].split(":", 1)[0]
        if kind in kinds and kind not in found:
            found.append(kind)
    return tuple(found)
