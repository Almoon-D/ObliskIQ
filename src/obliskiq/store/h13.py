"""H13's records (018_h13_sessions): the thesis book's capital on each day a cycle read it, and
H13's looks, each written once (``research/preregistrations/thesis-book.md``).

The live decision path reads what the looks decided here (``capital/allocation.py``): the book's
share, and the sources and conviction levels held back. It never reads research (D64).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final, Literal

from ..domain.orders import ExecutionMode
from .db import Store
from .mappers import date_text, read_date, read_stamp, stamp_text

type Record = Literal["shadow", "paper", "live"]
#: The group of the whole book; the others are ``source:<s>``, ``analyst:<segment>`` and
#: ``conviction:<c>``.
BOOK: Final = "book"
SOURCE: Final = "source:"
SEGMENT: Final = "analyst:"
CONVICTION: Final = "conviction:"
#: The modes whose fills make each record.
RECORD_MODES: Final[dict[Record, frozenset[ExecutionMode]]] = {
    "shadow": frozenset({ExecutionMode.SHADOW}),
    "paper": frozenset({ExecutionMode.PAPER}),
    "live": frozenset({ExecutionMode.LIVE_SMALL, ExecutionMode.LIVE}),
}


def record_of(mode: ExecutionMode) -> Record:
    return next(name for name, modes in RECORD_MODES.items() if mode in modes)


# ── the book's capital ──


@dataclass(frozen=True, slots=True)
class BookCapital:
    mode: ExecutionMode
    on: date
    capital: Decimal
    recorded_at: datetime


def record_book_capital(store: Store, reading: BookCapital) -> bool:
    """Keep the day's first reading of the book's capital; ``False`` when the day has one."""
    with store.transaction():
        cursor = store.execute(
            "INSERT INTO book_capital (mode, on_day, capital, recorded_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (mode, on_day) DO NOTHING",
            (
                reading.mode.value,
                date_text(reading.on),
                str(reading.capital),
                stamp_text(reading.recorded_at),
            ),
        )
        return cursor.rowcount > 0


def capital_before(store: Store, record: Record, day: date) -> Decimal | None:
    """The book's capital in ``record``'s modes on the last day before ``day`` it was read."""
    modes = sorted(mode.value for mode in RECORD_MODES[record])
    row = store.execute(
        f"SELECT capital FROM book_capital WHERE mode IN ({', '.join('?' for _ in modes)}) "
        "AND on_day < ? ORDER BY on_day DESC LIMIT 1",
        (*modes, date_text(day)),
    ).fetchone()
    return None if row is None else Decimal(row[0])


def book_capital(store: Store) -> list[BookCapital]:
    rows = store.execute("SELECT * FROM book_capital ORDER BY on_day, mode").fetchall()
    return [
        BookCapital(
            mode=ExecutionMode(row["mode"]),
            on=read_date(row["on_day"], "book_capital", "on_day"),
            capital=Decimal(row["capital"]),
            recorded_at=read_stamp(row["recorded_at"], "book_capital", "recorded_at"),
        )
        for row in rows
    ]


# ── looks ──


@dataclass(frozen=True, slots=True)
class H13Look:
    """One look of one group of one record. ``series`` is the months' ``x`` as JSON."""

    record: Record
    group: str
    month: str
    look: int
    months: int
    closed: int
    mean: float | None
    sd: float | None
    t_up: float | None
    t_down: float | None
    c_up: float | None
    c_down: float | None
    without_best: float | None
    step: bool
    back: bool
    evaluable: bool
    series: str
    detail: str
    at: datetime


def record_h13_look(store: Store, look: H13Look) -> bool:
    """Record a look; ``False`` when that record's group already has a look that month."""
    with store.transaction():
        cursor = store.execute(
            "INSERT INTO h13_looks (record, grp, month, look, months, closed, mean, sd, t_up, "
            "t_down, c_up, c_down, without_best, step, back, evaluable, series, detail, at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (record, grp, month) DO NOTHING",
            (
                look.record,
                look.group,
                look.month,
                look.look,
                look.months,
                look.closed,
                look.mean,
                look.sd,
                look.t_up,
                look.t_down,
                look.c_up,
                look.c_down,
                look.without_best,
                int(look.step),
                int(look.back),
                int(look.evaluable),
                look.series,
                look.detail,
                stamp_text(look.at),
            ),
        )
        return cursor.rowcount > 0


def _look(row: sqlite3.Row) -> H13Look:
    return H13Look(
        record=row["record"],
        group=row["grp"],
        month=row["month"],
        look=row["look"],
        months=row["months"],
        closed=row["closed"],
        mean=row["mean"],
        sd=row["sd"],
        t_up=row["t_up"],
        t_down=row["t_down"],
        c_up=row["c_up"],
        c_down=row["c_down"],
        without_best=row["without_best"],
        step=bool(row["step"]),
        back=bool(row["back"]),
        evaluable=bool(row["evaluable"]),
        series=row["series"],
        detail=row["detail"],
        at=read_stamp(row["at"], "h13_looks", "at"),
    )


def h13_looks(store: Store, record: Record | None = None) -> list[H13Look]:
    """The looks recorded (of one record, or every one), oldest month first."""
    sql = "SELECT * FROM h13_looks"
    params: list[object] = []
    if record is not None:
        sql += " WHERE record = ?"
        params.append(record)
    rows = store.execute(sql + " ORDER BY month, record, grp", params).fetchall()
    return [_look(row) for row in rows]


# ── what the looks decided ──


@dataclass(frozen=True, slots=True)
class Holdbacks:
    """What H13's group looks hold back on the live account: sources (and analyst segments) no
    longer traded there, and conviction levels sized as ``normal``; each with the look's
    detail."""

    sources: dict[str, str]
    segments: dict[str, str]
    convictions: dict[str, str]


def holdbacks(store: Store) -> Holdbacks:
    """Every group a paper or live look sent back to shadow, for good (a new registration is
    the only way back)."""
    sources: dict[str, str] = {}
    segments: dict[str, str] = {}
    convictions: dict[str, str] = {}
    for look in h13_looks(store):
        if not look.back or look.record == "shadow" or look.group == BOOK:
            continue
        why = f"H13's {look.record} look of {look.month}: {look.detail}"
        if look.group.startswith(SOURCE):
            sources.setdefault(look.group.removeprefix(SOURCE), why)
        elif look.group.startswith(SEGMENT):
            segments.setdefault(look.group.removeprefix(SEGMENT), why)
        elif look.group.startswith(CONVICTION):
            convictions.setdefault(look.group.removeprefix(CONVICTION), why)
    return Holdbacks(sources, segments, convictions)
