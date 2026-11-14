"""``managed_nav`` and ``breaker_events`` (014_breakers): a managed book's value over time, the
equity reference read beside it, and the hard breaker's latch.

**One writer.** :func:`record_reading` is the only code that writes a reading. ``as_of`` is the
day the reading was TAKEN. A day holds one reading: one taken inside the day replaces the day's
earlier one, the close (the day's last decision cycle) replaces either, and nothing replaces a
close, so a value read inside the day can never stand where a peak is taken from (DECISIONS D88).

**The latch** is append-only: a book's latest event says whether its hard breaker is tripped.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from ..errors import StoreError
from .db import Store
from .mappers import date_text, read_date, read_stamp, stamp_text

type Book = Literal["shadow", "broker"]
type LatchEvent = Literal["tripped", "reset"]

BOOKS: tuple[Book, ...] = ("shadow", "broker")
TRIPPED: LatchEvent = "tripped"
RESET: LatchEvent = "reset"


@dataclass(frozen=True, slots=True)
class ManagedReading:
    """A managed book's value in the base currency, read on ``as_of``, and one unit of the equity
    reference read with it (``None``: it had no quote). ``is_close``: the day's last cycle's."""

    book: Book
    as_of: date
    managed: Decimal
    reference_conid: int | None
    reference_unit: Decimal | None
    is_close: bool
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class BreakerEvent:
    """A change of a book's latch: the hard breaker tripped, or the owner reset it."""

    at: datetime
    book: Book
    event: LatchEvent
    actor: str
    detail: str


def record_reading(store: Store, reading: ManagedReading) -> bool:
    """Record the day's reading; ``False`` when the day already holds a close this one may not
    replace (a reading taken inside the day)."""
    if not reading.managed.is_finite() or reading.managed <= 0:
        raise StoreError(f"a managed value of {reading.managed} is not a book's value")
    if (reading.reference_conid is None) != (reading.reference_unit is None):
        raise StoreError("a reference reading names its conid and its value, or neither")
    with store.transaction():
        cursor = store.execute(
            "INSERT INTO managed_nav (book, as_of, managed, reference_conid, reference_unit, "
            "is_close, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (book, as_of) DO UPDATE SET managed = excluded.managed, "
            "reference_conid = excluded.reference_conid, "
            "reference_unit = excluded.reference_unit, is_close = excluded.is_close, "
            "recorded_at = excluded.recorded_at "
            "WHERE managed_nav.is_close = 0 OR excluded.is_close = 1",
            (
                reading.book,
                date_text(reading.as_of),
                str(reading.managed),
                reading.reference_conid,
                None if reading.reference_unit is None else str(reading.reference_unit),
                int(reading.is_close),
                stamp_text(reading.recorded_at),
            ),
        )
        return cursor.rowcount > 0


def readings(
    store: Store, book: Book, *, as_of: date, closes_only: bool = False
) -> list[ManagedReading]:
    """The book's readings taken on or before ``as_of``, oldest first."""
    rows = store.execute(
        "SELECT book, as_of, managed, reference_conid, reference_unit, is_close, recorded_at "
        "FROM managed_nav WHERE book = ? AND as_of <= ? AND is_close >= ? ORDER BY as_of",
        (book, date_text(as_of), int(closes_only)),
    ).fetchall()
    return [
        ManagedReading(
            book=row["book"],
            as_of=read_date(row["as_of"], "managed_nav", "as_of"),
            managed=Decimal(row["managed"]),
            reference_conid=row["reference_conid"],
            reference_unit=(
                None if row["reference_unit"] is None else Decimal(row["reference_unit"])
            ),
            is_close=bool(row["is_close"]),
            recorded_at=read_stamp(row["recorded_at"], "managed_nav", "recorded_at"),
        )
        for row in rows
    ]


def record_event(store: Store, event: BreakerEvent) -> None:
    with store.transaction():
        store.execute(
            "INSERT INTO breaker_events (at, book, event, actor, detail) VALUES (?, ?, ?, ?, ?)",
            (stamp_text(event.at), event.book, event.event, event.actor, event.detail),
        )


def _event(store: Store, book: Book, kind: LatchEvent | None) -> BreakerEvent | None:
    row = store.execute(
        "SELECT at, book, event, actor, detail FROM breaker_events "
        "WHERE book = ? AND (? IS NULL OR event = ?) ORDER BY event_id DESC LIMIT 1",
        (book, kind, kind),
    ).fetchone()
    if row is None:
        return None
    return BreakerEvent(
        at=read_stamp(row["at"], "breaker_events", "at"),
        book=row["book"],
        event=row["event"],
        actor=row["actor"],
        detail=row["detail"],
    )


def latest_event(store: Store, book: Book) -> BreakerEvent | None:
    """The book's last latch change (by insertion); ``None`` when it never tripped."""
    return _event(store, book, None)


def latest_reset(store: Store, book: Book) -> BreakerEvent | None:
    """The owner's last reset of the book's latch: the hard peak is taken after it."""
    return _event(store, book, RESET)


def latches(store: Store) -> list[BreakerEvent]:
    """Every trip a latch still holds, over all books."""
    return [event for book in BOOKS if (event := tripped(store, book)) is not None]


def tripped(store: Store, book: Book) -> BreakerEvent | None:
    """The trip the book's latch still holds; ``None`` when it is not tripped."""
    latest = latest_event(store, book)
    return latest if latest is not None and latest.event == TRIPPED else None
