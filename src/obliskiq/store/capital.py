"""The cash-flow ledger, withdrawal reservations, owner questions, unexplained cash events and
external holdings in the store (008_capital).

Every record is the owner's or the reconciliation's word and is never deleted: a flow is
cancelled, a reservation closed, a question answered or expired, a cash event classified, each
with its stamp; reservations and questions keep an append-only history of every change.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import sqlite3
from collections.abc import Callable, Iterable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ..domain.capital import (
    Adjustment,
    AdjustmentKind,
    CashEvent,
    CashEventState,
    CashFlow,
    Deposit,
    ExternalHolding,
    FlowKind,
    OwnerQuestion,
    QuestionKind,
    QuestionOption,
    QuestionState,
    ReservationEvent,
    ReservationState,
    Withdrawal,
    WithdrawalReservation,
)
from ..errors import StoreError
from .db import Store
from .mappers import (
    date_text,
    optional_stamp_text,
    read_date,
    read_optional_stamp,
    read_stamp,
    stamp_text,
)


def _decimal(text: str, table: str, column: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation:
        raise StoreError(f"{table}.{column} holds {text!r}, which is not a decimal") from None


def _build[T](table: str, make: Callable[[], T]) -> T:
    try:
        return make()
    except ValueError as error:  # a bad enum member or a model validation failure
        raise StoreError(f"a {table} row cannot be read: {error}") from None


def _new_id(cursor: sqlite3.Cursor, table: str) -> int:
    if cursor.lastrowid is None:  # pragma: no cover - an INSERT always sets it
        raise StoreError(f"the {table} row was not inserted")
    return cursor.lastrowid


def _next_seq(store: Store, table: str, key: str, value: int) -> int:
    row = store.execute(f"SELECT COALESCE(MAX(seq), 0) FROM {table} WHERE {key} = ?", (value,))
    return int(row.fetchone()[0]) + 1


# ── cash flows ──

_FLOW_COLUMNS = (
    "flow_id, kind, category, currency, amount, on_date, recorded_at, recorded_by, matched_at, "
    "cancelled_at, in_book, reservation_id, event_id, note"
)


def _flow_from_row(row: sqlite3.Row) -> CashFlow:
    common: dict[str, Any] = {
        "flow_id": row["flow_id"],
        "currency": row["currency"],
        "on": read_date(row["on_date"], "cash_flows", "on_date"),
        "recorded_at": read_stamp(row["recorded_at"], "cash_flows", "recorded_at"),
        "recorded_by": row["recorded_by"],
        "matched_at": read_optional_stamp(row["matched_at"], "cash_flows", "matched_at"),
        "cancelled_at": read_optional_stamp(row["cancelled_at"], "cash_flows", "cancelled_at"),
        "in_book": bool(row["in_book"]),
        "note": row["note"],
        "amount": _decimal(row["amount"], "cash_flows", "amount"),
    }

    def make() -> CashFlow:
        kind = FlowKind(row["kind"])
        if kind is FlowKind.DEPOSIT:
            return Deposit(**common)
        if kind is FlowKind.WITHDRAWAL:
            return Withdrawal(**common, reservation_id=row["reservation_id"])
        return Adjustment(
            **common, category=AdjustmentKind(row["category"]), event_id=row["event_id"]
        )

    return _build("cash_flows", make)


def record_flow(store: Store, flow: CashFlow) -> CashFlow:
    """Store a new flow and return it with its id."""
    if flow.flow_id is not None:
        raise StoreError("a new flow carries no id; the store gives it one")
    category = flow.category.value if isinstance(flow, Adjustment) else None
    reservation = flow.reservation_id if isinstance(flow, Withdrawal) else None
    event = flow.event_id if isinstance(flow, Adjustment) else None
    with store.transaction():
        cursor = store.execute(
            "INSERT INTO cash_flows (kind, category, currency, amount, on_date, recorded_at, "
            "recorded_by, matched_at, cancelled_at, in_book, reservation_id, event_id, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                flow.kind.value,
                category,
                flow.currency,
                str(flow.amount),
                date_text(flow.on),
                stamp_text(flow.recorded_at),
                flow.recorded_by,
                optional_stamp_text(flow.matched_at),
                optional_stamp_text(flow.cancelled_at),
                int(flow.in_book),
                reservation,
                event,
                flow.note,
            ),
        )
        return flow.model_copy(update={"flow_id": _new_id(cursor, "cash_flows")})


def get_flow(store: Store, flow_id: int) -> CashFlow | None:
    row = store.execute(
        f"SELECT {_FLOW_COLUMNS} FROM cash_flows WHERE flow_id = ?", (flow_id,)
    ).fetchone()
    return None if row is None else _flow_from_row(row)


def cash_flows(store: Store, *, as_of: date | None = None) -> list[CashFlow]:
    """Every flow, cancelled ones included, in the order they were recorded; with ``as_of``,
    only those effective on or before it."""
    if as_of is None:
        rows = store.execute(f"SELECT {_FLOW_COLUMNS} FROM cash_flows ORDER BY flow_id")
    else:
        rows = store.execute(
            f"SELECT {_FLOW_COLUMNS} FROM cash_flows WHERE on_date <= ? ORDER BY flow_id",
            (date_text(as_of),),
        )
    return [_flow_from_row(row) for row in rows.fetchall()]


def match_flow(store: Store, flow_id: int, *, at: datetime) -> None:
    """Stamp a flow as shown by the broker's cash; a flow is matched once."""
    with store.transaction():
        cursor = store.execute(
            "UPDATE cash_flows SET matched_at = ? WHERE flow_id = ? AND matched_at IS NULL "
            "AND cancelled_at IS NULL",
            (stamp_text(at), flow_id),
        )
        if cursor.rowcount != 1:
            raise StoreError(
                f"cash flow {flow_id} cannot be matched (unknown, matched or cancelled)"
            )


def cancel_flow(store: Store, flow_id: int, *, at: datetime) -> None:
    """Cancel a flow the broker has not shown; a matched or cancelled one is refused."""
    with store.transaction():
        cursor = store.execute(
            "UPDATE cash_flows SET cancelled_at = ? WHERE flow_id = ? AND cancelled_at IS NULL "
            "AND matched_at IS NULL",
            (stamp_text(at), flow_id),
        )
        if cursor.rowcount != 1:
            raise StoreError(
                f"cash flow {flow_id} cannot be cancelled (unknown, already cancelled, or shown "
                "by the broker's cash)"
            )


# ── reservations ──

_RESERVATION_COLUMNS = (
    "reservation_id, amount, currency, due_date, reason, state, created_at, created_by, updated_at"
)


def _reservation_from_row(row: sqlite3.Row) -> WithdrawalReservation:
    def make() -> WithdrawalReservation:
        return WithdrawalReservation(
            reservation_id=row["reservation_id"],
            amount=_decimal(row["amount"], "withdrawal_reservations", "amount"),
            currency=row["currency"],
            due_date=read_date(row["due_date"], "withdrawal_reservations", "due_date"),
            reason=row["reason"],
            state=ReservationState(row["state"]),
            created_at=read_stamp(row["created_at"], "withdrawal_reservations", "created_at"),
            created_by=row["created_by"],
            updated_at=read_stamp(row["updated_at"], "withdrawal_reservations", "updated_at"),
        )

    return _build("withdrawal_reservations", make)


def _reservation_event(
    store: Store, reservation_id: int, at: datetime, actor: str, detail: str
) -> None:
    store.execute(
        "INSERT INTO reservation_events (reservation_id, seq, at, actor, detail) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            reservation_id,
            _next_seq(store, "reservation_events", "reservation_id", reservation_id),
            stamp_text(at),
            actor,
            detail,
        ),
    )


def record_reservation(store: Store, reservation: WithdrawalReservation) -> WithdrawalReservation:
    """Store a new reservation and its first event."""
    if reservation.reservation_id is not None:
        raise StoreError("a new reservation carries no id; the store gives it one")
    if reservation.state is not ReservationState.OPEN:
        raise StoreError("a reservation starts open")
    with store.transaction():
        cursor = store.execute(
            "INSERT INTO withdrawal_reservations (amount, currency, due_date, reason, state, "
            "created_at, created_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(reservation.amount),
                reservation.currency,
                date_text(reservation.due_date),
                reservation.reason,
                reservation.state.value,
                stamp_text(reservation.created_at),
                reservation.created_by,
                stamp_text(reservation.updated_at),
            ),
        )
        made = reservation.model_copy(
            update={"reservation_id": _new_id(cursor, "withdrawal_reservations")}
        )
        assert made.reservation_id is not None
        _reservation_event(
            store,
            made.reservation_id,
            reservation.created_at,
            reservation.created_by,
            f"reserved {reservation.amount} {reservation.currency} by "
            f"{reservation.due_date.isoformat()}: {reservation.reason}",
        )
        return made


def get_reservation(store: Store, reservation_id: int) -> WithdrawalReservation | None:
    row = store.execute(
        f"SELECT {_RESERVATION_COLUMNS} FROM withdrawal_reservations WHERE reservation_id = ?",
        (reservation_id,),
    ).fetchone()
    return None if row is None else _reservation_from_row(row)


def reservations(
    store: Store, states: Iterable[ReservationState] | None = None
) -> list[WithdrawalReservation]:
    """Reservations in ``states`` (every one when ``None``), earliest due first."""
    rows = store.execute(
        f"SELECT {_RESERVATION_COLUMNS} FROM withdrawal_reservations "
        "ORDER BY due_date, reservation_id"
    ).fetchall()
    wanted = None if states is None else {state.value for state in states}
    return [_reservation_from_row(row) for row in rows if wanted is None or row["state"] in wanted]


def change_reservation(
    store: Store,
    reservation_id: int,
    *,
    at: datetime,
    actor: str,
    detail: str,
    amount: Decimal | None = None,
    due_date: date | None = None,
    state: ReservationState | None = None,
) -> WithdrawalReservation:
    """Change an open reservation's amount, due date or state, recording why."""
    with store.transaction():
        current = get_reservation(store, reservation_id)
        if current is None:
            raise StoreError(f"no reservation {reservation_id}")
        if current.state is not ReservationState.OPEN:
            raise StoreError(f"reservation {reservation_id} is {current.state.value}")
        changed = WithdrawalReservation.model_validate(
            current.model_dump()
            | {
                "amount": current.amount if amount is None else amount,
                "due_date": current.due_date if due_date is None else due_date,
                "state": current.state if state is None else state,
                "updated_at": at,
            }
        )
        store.execute(
            "UPDATE withdrawal_reservations SET amount = ?, due_date = ?, state = ?, "
            "updated_at = ? WHERE reservation_id = ?",
            (
                str(changed.amount),
                date_text(changed.due_date),
                changed.state.value,
                stamp_text(at),
                reservation_id,
            ),
        )
        _reservation_event(store, reservation_id, at, actor, detail)
        return changed


def _reservation_event_from_row(row: sqlite3.Row) -> ReservationEvent:
    def make() -> ReservationEvent:
        return ReservationEvent(
            reservation_id=row["reservation_id"],
            seq=row["seq"],
            at=read_stamp(row["at"], "reservation_events", "at"),
            actor=row["actor"],
            detail=row["detail"],
        )

    return _build("reservation_events", make)


def reservation_history(store: Store, reservation_id: int) -> list[ReservationEvent]:
    rows = store.execute(
        "SELECT reservation_id, seq, at, actor, detail FROM reservation_events "
        "WHERE reservation_id = ? ORDER BY seq",
        (reservation_id,),
    ).fetchall()
    return [_reservation_event_from_row(row) for row in rows]


# ── owner questions ──

_QUESTION_COLUMNS = (
    "question_id, kind, reservation_id, context, options, default_option, deadline, state, "
    "answer, answered_at, answered_by, created_at"
)


def _question_from_row(row: sqlite3.Row) -> OwnerQuestion:
    def make() -> OwnerQuestion:
        return OwnerQuestion(
            question_id=row["question_id"],
            kind=QuestionKind(row["kind"]),
            reservation_id=row["reservation_id"],
            context=row["context"],
            options=tuple(QuestionOption.model_validate(o) for o in json.loads(row["options"])),
            default=row["default_option"],
            deadline=read_stamp(row["deadline"], "owner_questions", "deadline"),
            state=QuestionState(row["state"]),
            answer=row["answer"],
            answered_at=read_optional_stamp(row["answered_at"], "owner_questions", "answered_at"),
            answered_by=row["answered_by"],
            created_at=read_stamp(row["created_at"], "owner_questions", "created_at"),
        )

    return _build("owner_questions", make)


def _question_event(store: Store, question_id: int, at: datetime, actor: str, detail: str) -> None:
    store.execute(
        "INSERT INTO question_events (question_id, seq, at, actor, detail) VALUES (?, ?, ?, ?, ?)",
        (
            question_id,
            _next_seq(store, "question_events", "question_id", question_id),
            stamp_text(at),
            actor,
            detail,
        ),
    )


def record_question(store: Store, question: OwnerQuestion, *, actor: str) -> OwnerQuestion:
    """Store a new open question; a second open one of the same kind and reservation is refused."""
    if question.question_id is not None or question.state is not QuestionState.OPEN:
        raise StoreError("a new question carries no id and is open")
    with store.transaction():
        cursor = store.execute(
            "INSERT INTO owner_questions (kind, reservation_id, context, options, default_option, "
            "deadline, state, answer, answered_at, answered_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)",
            (
                question.kind.value,
                question.reservation_id,
                question.context,
                json.dumps([option.model_dump(mode="json") for option in question.options]),
                question.default,
                stamp_text(question.deadline),
                question.state.value,
                stamp_text(question.created_at),
            ),
        )
        made = question.model_copy(update={"question_id": _new_id(cursor, "owner_questions")})
        assert made.question_id is not None
        _question_event(store, made.question_id, question.created_at, actor, "asked")
        return made


def get_question(store: Store, question_id: int) -> OwnerQuestion | None:
    row = store.execute(
        f"SELECT {_QUESTION_COLUMNS} FROM owner_questions WHERE question_id = ?", (question_id,)
    ).fetchone()
    return None if row is None else _question_from_row(row)


def questions(
    store: Store,
    *,
    states: Iterable[QuestionState] | None = None,
    reservation_id: int | None = None,
) -> list[OwnerQuestion]:
    """Questions in ``states`` (every one when ``None``), oldest first."""
    rows = store.execute(
        f"SELECT {_QUESTION_COLUMNS} FROM owner_questions ORDER BY question_id"
    ).fetchall()
    wanted = None if states is None else {state.value for state in states}
    return [
        _question_from_row(row)
        for row in rows
        if (wanted is None or row["state"] in wanted)
        and (reservation_id is None or row["reservation_id"] == reservation_id)
    ]


def close_question(
    store: Store,
    question_id: int,
    *,
    state: QuestionState,
    answer: str,
    at: datetime,
    actor: str,
) -> OwnerQuestion:
    """Answer an open question (``answered``) or apply its default at the deadline
    (``expired``)."""
    if state is QuestionState.OPEN:
        raise StoreError("closing a question answers it or lets it expire")
    with store.transaction():
        current = get_question(store, question_id)
        if current is None:
            raise StoreError(f"no question {question_id}")
        if current.state is not QuestionState.OPEN:
            raise StoreError(f"question {question_id} is already {current.state.value}")
        if current.option(answer) is None:
            raise StoreError(f"question {question_id} has no option {answer!r}")
        store.execute(
            "UPDATE owner_questions SET state = ?, answer = ?, answered_at = ?, answered_by = ? "
            "WHERE question_id = ?",
            (state.value, answer, stamp_text(at), actor, question_id),
        )
        _question_event(store, question_id, at, actor, f"{state.value}: {answer}")
        closed = get_question(store, question_id)
        assert closed is not None
        return closed


# ── unexplained cash ──

_EVENT_COLUMNS = (
    "event_id, currency, amount, detected_at, updated_at, state, classified_at, classified_by"
)


def _event_from_row(row: sqlite3.Row) -> CashEvent:
    def make() -> CashEvent:
        return CashEvent(
            event_id=row["event_id"],
            currency=row["currency"],
            amount=_decimal(row["amount"], "cash_events", "amount"),
            detected_at=read_stamp(row["detected_at"], "cash_events", "detected_at"),
            updated_at=read_stamp(row["updated_at"], "cash_events", "updated_at"),
            state=CashEventState(row["state"]),
            classified_at=read_optional_stamp(row["classified_at"], "cash_events", "classified_at"),
            classified_by=row["classified_by"],
        )

    return _build("cash_events", make)


def note_unexplained(store: Store, currency: str, amount: Decimal, *, at: datetime) -> CashEvent:
    """Record, or refresh, the pending event of ``currency`` with the unexplained ``amount``."""
    with store.transaction():
        row = store.execute(
            f"SELECT {_EVENT_COLUMNS} FROM cash_events WHERE currency = ? AND state = 'pending'",
            (currency,),
        ).fetchone()
        if row is None:
            cursor = store.execute(
                "INSERT INTO cash_events (currency, amount, detected_at, updated_at, state) "
                "VALUES (?, ?, ?, ?, 'pending')",
                (currency, str(amount), stamp_text(at), stamp_text(at)),
            )
            event_id = _new_id(cursor, "cash_events")
        else:
            event_id = row["event_id"]
            store.execute(
                "UPDATE cash_events SET amount = ?, updated_at = ? WHERE event_id = ?",
                (str(amount), stamp_text(at), event_id),
            )
        fresh = store.execute(
            f"SELECT {_EVENT_COLUMNS} FROM cash_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return _event_from_row(fresh)


def cash_events(store: Store, *, pending_only: bool = False) -> list[CashEvent]:
    sql = f"SELECT {_EVENT_COLUMNS} FROM cash_events"
    if pending_only:
        sql += " WHERE state = 'pending'"
    return [_event_from_row(row) for row in store.execute(sql + " ORDER BY event_id").fetchall()]


def get_cash_event(store: Store, event_id: int) -> CashEvent | None:
    row = store.execute(
        f"SELECT {_EVENT_COLUMNS} FROM cash_events WHERE event_id = ?", (event_id,)
    ).fetchone()
    return None if row is None else _event_from_row(row)


def classify_event(
    store: Store, event_id: int, flow: CashFlow, *, at: datetime, actor: str
) -> CashFlow:
    """Classify a pending event: record ``flow`` (which explains it) and close the event, in one
    transaction."""
    with store.transaction():
        event = get_cash_event(store, event_id)
        if event is None:
            raise StoreError(f"no cash event {event_id}")
        if event.state is not CashEventState.PENDING:
            raise StoreError(f"cash event {event_id} is already classified")
        made = record_flow(store, flow)
        store.execute(
            "UPDATE cash_events SET state = 'classified', classified_at = ?, classified_by = ? "
            "WHERE event_id = ?",
            (stamp_text(at), actor, event_id),
        )
        return made


# ── external holdings ──

_EXTERNAL_COLUMNS = (
    "holding_id, broker, symbol, isin, quantity, currency, market_value_base, last_updated, "
    "notes, sector, country"
)


def _external_from_row(row: sqlite3.Row) -> ExternalHolding:
    def make() -> ExternalHolding:
        return ExternalHolding(
            holding_id=row["holding_id"],
            broker=row["broker"],
            symbol=row["symbol"],
            isin=row["isin"],
            quantity=_decimal(row["quantity"], "external_positions", "quantity"),
            currency=row["currency"],
            market_value_base=_decimal(
                row["market_value_base"], "external_positions", "market_value_base"
            ),
            last_updated=read_date(row["last_updated"], "external_positions", "last_updated"),
            notes=row["notes"],
            sector=row["sector"],
            country=row["country"],
        )

    return _build("external_positions", make)


def upsert_external(store: Store, holding: ExternalHolding, *, at: datetime) -> ExternalHolding:
    """Update in place, or insert, keyed on (broker, symbol, isin), a missing ISIN matching a
    missing ISIN (lite's ``_upsert_external``: an update must never append a second row that
    would then be counted twice)."""
    values = (
        str(holding.quantity),
        holding.currency,
        str(holding.market_value_base),
        date_text(holding.last_updated),
        holding.notes,
        holding.sector,
        holding.country,
        stamp_text(at),
    )
    with store.transaction():
        cursor = store.execute(
            "UPDATE external_positions SET quantity = ?, currency = ?, market_value_base = ?, "
            "last_updated = ?, notes = ?, sector = ?, country = ?, recorded_at = ? "
            "WHERE broker = ? AND symbol = ? AND isin IS ?",
            (*values, holding.broker, holding.symbol, holding.isin),
        )
        if cursor.rowcount == 0:
            store.execute(
                "INSERT INTO external_positions (quantity, currency, market_value_base, "
                "last_updated, notes, sector, country, recorded_at, broker, symbol, isin) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*values, holding.broker, holding.symbol, holding.isin),
            )
        row = store.execute(
            f"SELECT {_EXTERNAL_COLUMNS} FROM external_positions "
            "WHERE broker = ? AND symbol = ? AND isin IS ?",
            (holding.broker, holding.symbol, holding.isin),
        ).fetchone()
        return _external_from_row(row)


def external_holdings(store: Store) -> list[ExternalHolding]:
    rows = store.execute(
        f"SELECT {_EXTERNAL_COLUMNS} FROM external_positions ORDER BY broker, symbol, holding_id"
    ).fetchall()
    return [_external_from_row(row) for row in rows]


def remove_external(store: Store, holding_id: int) -> bool:
    """Delete one external holding (the owner no longer holds it); ``False`` when unknown."""
    with store.transaction():
        cursor = store.execute("DELETE FROM external_positions WHERE holding_id = ?", (holding_id,))
        return cursor.rowcount == 1
