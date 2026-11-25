"""The thesis book in the store (013_theses, 016_thesis_options): theses, their append-only log
and their grades.

A thesis moves only along its lifecycle (``domain/thesis.py``); every change writes its log entry
in the same transaction. The store's triggers keep an approved thesis's text and triggers, and
any ended thesis, from changing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ..domain.orders import ExecutionMode
from ..domain.thesis import (
    ENDED_STATES,
    Expression,
    LogKind,
    Thesis,
    ThesisGrade,
    ThesisInstrument,
    ThesisLogEntry,
    ThesisState,
)
from ..errors import StoreError
from .db import Store
from .mappers import (
    cutoff_text,
    date_text,
    read_date,
    read_optional_stamp,
    read_stamp,
    stamp_text,
)

#: Where a thesis may go from each state it can leave.
TRANSITIONS: dict[ThesisState, frozenset[ThesisState]] = {
    ThesisState.CANDIDATE: frozenset(
        {ThesisState.APPROVED, ThesisState.REFUSED, ThesisState.WITHDRAWN, ThesisState.EXPIRED}
    ),
    ThesisState.APPROVED: frozenset(
        {ThesisState.ACTIVE, ThesisState.WITHDRAWN, ThesisState.EXPIRED}
    ),
    ThesisState.ACTIVE: frozenset({ThesisState.CLOSED}),
}

_COLUMNS = (
    "thesis_id, source, conviction, view, expression, symbol, candidate_key, conid, "
    "listing_exchange, currency, asset_class, description, sector, country, defensive, isin, "
    "expectation, target, "
    "invalidation, horizon, entry_max, enter_by, state, mode, size_base, held_as, exit_reason, "
    "created_at, approved_at, opened_at, closed_at"
)


def _decimal(text: str, column: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation:
        raise StoreError(f"{column} holds {text!r}, which is not a decimal") from None


def _optional(text: str | None, column: str) -> Decimal | None:
    return None if text is None else _decimal(text, column)


def _build[T](table: str, make: Callable[[], T]) -> T:
    try:
        return make()
    except ValueError as error:  # a bad enum member or a model validation failure
        raise StoreError(f"a {table} row cannot be read: {error}") from None


def _thesis(row: sqlite3.Row) -> Thesis:
    table = "theses"
    return _build(
        table,
        lambda: Thesis(
            thesis_id=row["thesis_id"],
            source=row["source"],
            conviction=row["conviction"],
            view=row["view"],
            expression=row["expression"],
            instrument=ThesisInstrument(
                symbol=row["symbol"],
                candidate_key=row["candidate_key"],
                conid=row["conid"],
                listing_exchange=row["listing_exchange"],
                currency=row["currency"],
                asset_class=row["asset_class"],
                description=row["description"],
                sector=row["sector"],
                country=row["country"],
                defensive=bool(row["defensive"]),
                isin=row["isin"],
            ),
            expectation=row["expectation"],
            target=_decimal(row["target"], "target"),
            invalidation=_decimal(row["invalidation"], "invalidation"),
            horizon=read_date(row["horizon"], table, "horizon"),
            entry_max=_optional(row["entry_max"], "entry_max"),
            enter_by=read_date(row["enter_by"], table, "enter_by"),
            state=row["state"],
            mode=row["mode"],
            size_base=_optional(row["size_base"], "size_base"),
            held_as=row["held_as"],
            exit_reason=row["exit_reason"],
            created_at=read_stamp(row["created_at"], table, "created_at"),
            approved_at=read_optional_stamp(row["approved_at"], table, "approved_at"),
            opened_at=read_optional_stamp(row["opened_at"], table, "opened_at"),
            closed_at=read_optional_stamp(row["closed_at"], table, "closed_at"),
        ),
    )


def _text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def append_log(
    store: Store, thesis_id: int, *, at: datetime, actor: str, kind: LogKind, detail: str
) -> ThesisLogEntry:
    """Append one entry to a thesis's log."""
    with store.transaction():
        row = store.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM thesis_log WHERE thesis_id = ?", (thesis_id,)
        ).fetchone()
        entry = ThesisLogEntry(
            thesis_id=thesis_id, seq=int(row[0]) + 1, at=at, actor=actor, kind=kind, detail=detail
        )
        store.execute(
            "INSERT INTO thesis_log (thesis_id, seq, at, actor, kind, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (thesis_id, entry.seq, stamp_text(at), actor, kind.value, detail),
        )
    return entry


def insert_thesis(store: Store, thesis: Thesis, *, actor: str) -> Thesis:
    """Store a new candidate with its intake entry; the store assigns its ``thesis_id``."""
    if thesis.state is not ThesisState.CANDIDATE:
        raise StoreError("a thesis enters the store as a candidate")
    instrument = thesis.instrument
    with store.transaction():
        cursor = store.execute(
            "INSERT INTO theses (source, conviction, view, expression, symbol, candidate_key, "
            "conid, listing_exchange, currency, asset_class, description, sector, country, "
            "defensive, isin, expectation, target, invalidation, horizon, entry_max, enter_by, "
            "state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                thesis.source.value,
                thesis.conviction.value,
                thesis.view.value,
                thesis.expression.value,
                instrument.symbol,
                instrument.candidate_key,
                instrument.conid,
                instrument.listing_exchange,
                instrument.currency,
                instrument.asset_class.value,
                instrument.description,
                instrument.sector,
                instrument.country,
                int(instrument.defensive),
                instrument.isin,
                thesis.expectation,
                _text(thesis.target),
                _text(thesis.invalidation),
                date_text(thesis.horizon),
                _text(thesis.entry_max),
                date_text(thesis.enter_by),
                thesis.state.value,
                stamp_text(thesis.created_at),
            ),
        )
        stored = thesis.model_copy(update={"thesis_id": cursor.lastrowid})
        append_log(
            store,
            stored.thesis_id,
            at=thesis.created_at,
            actor=actor,
            kind=LogKind.INTAKE,
            detail=(
                f"handed in by {thesis.source.value}, conviction {thesis.conviction.value}, to "
                f"be held as {thesis.expression.value}"
            ),
        )
    return stored


def get_thesis(store: Store, thesis_id: int) -> Thesis | None:
    row = store.execute(
        f"SELECT {_COLUMNS} FROM theses WHERE thesis_id = ?", (thesis_id,)
    ).fetchone()
    return None if row is None else _thesis(row)


def _required(store: Store, thesis_id: int) -> Thesis:
    thesis = get_thesis(store, thesis_id)
    if thesis is None:
        raise StoreError(f"no thesis {thesis_id}")
    return thesis


def theses(
    store: Store, *, as_of: date, states: Iterable[ThesisState] | None = None
) -> list[Thesis]:
    """The theses handed in by ``as_of``, in order of arrival; ``states`` keeps only those."""
    rows = store.execute(
        f"SELECT {_COLUMNS} FROM theses WHERE created_at < ? ORDER BY thesis_id",
        (cutoff_text(as_of),),
    ).fetchall()
    wanted = None if states is None else frozenset(states)
    found = [_thesis(row) for row in rows]
    return [thesis for thesis in found if wanted is None or thesis.state in wanted]


def pin_instrument(
    store: Store,
    thesis_id: int,
    *,
    conid: int,
    symbol: str,
    listing_exchange: str,
    currency: str,
    at: datetime,
    actor: str,
) -> Thesis:
    """Pin a candidate thesis's instrument to the contract IBKR resolved."""
    with store.transaction():
        store.execute(
            "UPDATE theses SET conid = ?, symbol = ?, listing_exchange = ?, currency = ? "
            "WHERE thesis_id = ?",
            (conid, symbol, listing_exchange, currency, thesis_id),
        )
        append_log(
            store,
            thesis_id,
            at=at,
            actor=actor,
            kind=LogKind.RESOLVED,
            detail=f"pinned to conid {conid} ({symbol} on {listing_exchange}, {currency})",
        )
        return _required(store, thesis_id)


def give_isin(store: Store, thesis_id: int, isin: str, *, at: datetime, actor: str) -> Thesis:
    """Keep a candidate thesis's ISIN (a listed stock's, given after its intake)."""
    with store.transaction():
        store.execute("UPDATE theses SET isin = ? WHERE thesis_id = ?", (isin, thesis_id))
        append_log(
            store, thesis_id, at=at, actor=actor, kind=LogKind.RESOLVED, detail=f"ISIN {isin}"
        )
        return _required(store, thesis_id)


def approve(
    store: Store,
    thesis_id: int,
    *,
    mode: ExecutionMode,
    size_base: Decimal,
    at: datetime,
    actor: str,
    detail: str,
    held_as: Expression = Expression.SHARES,
) -> Thesis:
    """A candidate the gate passed: its mode, its size and what it is held as are fixed, and its
    text from here on."""
    with store.transaction():
        _check_move(_required(store, thesis_id), ThesisState.APPROVED)
        store.execute(
            "UPDATE theses SET state = ?, mode = ?, size_base = ?, held_as = ?, approved_at = ? "
            "WHERE thesis_id = ?",
            (
                ThesisState.APPROVED.value,
                mode.value,
                _text(size_base),
                held_as.value,
                stamp_text(at),
                thesis_id,
            ),
        )
        append_log(store, thesis_id, at=at, actor=actor, kind=LogKind.STATE, detail=detail)
        return _required(store, thesis_id)


def _check_move(thesis: Thesis, to: ThesisState) -> None:
    if to not in TRANSITIONS.get(thesis.state, frozenset()):
        raise StoreError(f"{thesis.ref} is {thesis.state.value}: it cannot become {to.value}")


def move(
    store: Store,
    thesis_id: int,
    to: ThesisState,
    *,
    at: datetime,
    actor: str,
    detail: str,
    exit_reason: str | None = None,
) -> Thesis:
    """Move a thesis to ``active`` or to an ended state, with why."""
    if to is ThesisState.APPROVED:
        raise StoreError("a thesis is approved with its mode and size (approve)")
    with store.transaction():
        _check_move(_required(store, thesis_id), to)
        stamp = "opened_at" if to is ThesisState.ACTIVE else "closed_at"
        store.execute(
            f"UPDATE theses SET state = ?, {stamp} = ?, exit_reason = ? WHERE thesis_id = ?",
            (to.value, stamp_text(at), exit_reason if to in ENDED_STATES else None, thesis_id),
        )
        append_log(store, thesis_id, at=at, actor=actor, kind=LogKind.STATE, detail=detail)
        return _required(store, thesis_id)


def _log_entry(row: sqlite3.Row) -> ThesisLogEntry:
    return _build(
        "thesis_log",
        lambda: ThesisLogEntry(
            thesis_id=row["thesis_id"],
            seq=row["seq"],
            at=read_stamp(row["at"], "thesis_log", "at"),
            actor=row["actor"],
            kind=row["kind"],
            detail=row["detail"],
        ),
    )


def log_entries(store: Store, thesis_id: int) -> list[ThesisLogEntry]:
    rows = store.execute(
        "SELECT thesis_id, seq, at, actor, kind, detail FROM thesis_log WHERE thesis_id = ? "
        "ORDER BY seq",
        (thesis_id,),
    ).fetchall()
    return [_log_entry(row) for row in rows]


def latest_log(store: Store, thesis_id: int) -> ThesisLogEntry | None:
    entries = log_entries(store, thesis_id)
    return entries[-1] if entries else None


def record_grade(store: Store, grade: ThesisGrade) -> None:
    with store.transaction():
        store.execute(
            "INSERT INTO thesis_grades (thesis_id, graded_at, mode, source, conviction, "
            "exit_reason, quantity, entry_price, exit_price, reached, payoff, stated_payoff, "
            "days_held, horizon_days, held_as) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                grade.thesis_id,
                stamp_text(grade.graded_at),
                grade.mode.value,
                grade.source.value,
                grade.conviction.value,
                grade.exit_reason,
                _text(grade.quantity),
                _text(grade.entry_price),
                _text(grade.exit_price),
                int(grade.reached),
                _text(grade.payoff),
                _text(grade.stated_payoff),
                grade.days_held,
                grade.horizon_days,
                grade.held_as.value,
            ),
        )


def _grade(row: sqlite3.Row) -> ThesisGrade:
    table = "thesis_grades"
    return _build(
        table,
        lambda: ThesisGrade(
            thesis_id=row["thesis_id"],
            graded_at=read_stamp(row["graded_at"], table, "graded_at"),
            mode=row["mode"],
            source=row["source"],
            conviction=row["conviction"],
            held_as=row["held_as"],
            exit_reason=row["exit_reason"],
            quantity=_decimal(row["quantity"], "quantity"),
            entry_price=_decimal(row["entry_price"], "entry_price"),
            exit_price=_decimal(row["exit_price"], "exit_price"),
            reached=bool(row["reached"]),
            payoff=_decimal(row["payoff"], "payoff"),
            stated_payoff=_decimal(row["stated_payoff"], "stated_payoff"),
            days_held=row["days_held"],
            horizon_days=row["horizon_days"],
        ),
    )


def grades(store: Store, *, as_of: date) -> list[ThesisGrade]:
    """Every grade written by ``as_of``, in the order the theses closed."""
    rows = store.execute(
        "SELECT * FROM thesis_grades WHERE graded_at < ? ORDER BY graded_at, thesis_id",
        (cutoff_text(as_of),),
    ).fetchall()
    return [_grade(row) for row in rows]


@dataclass(frozen=True, slots=True)
class PinnedShare:
    """A stock candidate a thesis pinned: a share outside the universe file, as IBKR resolved it
    and as its thesis described it."""

    conid: int
    symbol: str
    listing_exchange: str
    currency: str
    description: str
    #: ``None`` for a listed stock whose ISIN is not given yet: it trades in shadow alone.
    isin: str | None
    #: The issuer's country, where the thesis states it.
    country: str | None = None


def pinned_shares(store: Store) -> list[PinnedShare]:
    """The shares outside the universe file the order path must describe, one per conid."""
    rows = store.execute(
        "SELECT conid, symbol, listing_exchange, currency, description, isin, country FROM theses "
        "WHERE candidate_key IS NOT NULL AND conid IS NOT NULL ORDER BY thesis_id"
    ).fetchall()
    found: dict[int, PinnedShare] = {}
    for row in rows:
        found.setdefault(row["conid"], PinnedShare(*row))
    return list(found.values())
