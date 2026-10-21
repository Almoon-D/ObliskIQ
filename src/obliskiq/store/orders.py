"""The order path's state: intents and their history, clearances, what the broker reported,
modes, the kill switch and the holdings registry.

Two kinds of read live here. Snapshots of the account (positions, cash) are time series and
take ``as_of`` like every measurement read. The rest is the system's own state — an intent's
current state, whether a clearance was spent, the current mode, the kill switch — and is read
whole, unbounded by time: a time bound can only hide a row, and a hidden kill switch, downgrade
or spent clearance would fail open (DECISIONS D46).

Every write opens a transaction (joining the caller's, when one is open), so its rows and their
history land together or not at all.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from ..domain.orders import (
    TERMINAL_STATES,
    UNMANAGED,
    ApprovalDecision,
    BrokerOrder,
    CashBalance,
    ClearanceAction,
    ExecutionMode,
    Fill,
    HoldingAssignment,
    IntentEvent,
    IntentRole,
    IntentState,
    KillSwitchRecord,
    ModeChange,
    OrderIntent,
    OrderTerms,
    Position,
    PretradeClearance,
    ProtectiveSpec,
    ReconcileFinding,
    ReconcileRun,
    ReconcileStatus,
    RiskEffect,
    Sleeve,
    StopResize,
    StopResizeState,
)
from ..errors import StoreError
from .db import Store
from .mappers import read_stamp, stamp_text

_INTENT_COLUMNS = (
    "intent_id, coid, sleeve, thesis_id, role, terms, reason, mode, approval_required, "
    "created_at, expires_at, parent_id, protective_id, protective_spec, state"
)


@dataclass(frozen=True, slots=True)
class StoredIntent:
    """An intent as the store holds it, with the customer order id IBKR sees."""

    intent: OrderIntent
    coid: str


@dataclass(frozen=True, slots=True)
class StoredClearance:
    """A clearance as the gate recorded it, and when it was spent (``None``: not yet)."""

    clearance: PretradeClearance
    used_at: datetime | None


@dataclass(frozen=True, slots=True)
class OpenClearance:
    """A clearance with what the gate needs to count it: whether it was spent, and its intent's
    cOID and state."""

    clearance: PretradeClearance
    used_at: datetime | None
    coid: str
    intent_state: IntentState


@dataclass(frozen=True, slots=True)
class AccountRead[T]:
    """One complete read of positions or cash; ``rows`` empty is an empty account."""

    observed_at: datetime
    rows: tuple[T, ...]


def _decimal(text: str | None, table: str, column: str) -> Decimal | None:
    if text is None:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        raise StoreError(f"{table}.{column} holds {text!r}, which is not a decimal") from None


def _required_decimal(text: str, table: str, column: str) -> Decimal:
    value = _decimal(text, table, column)
    if value is None:  # pragma: no cover - NOT NULL columns
        raise StoreError(f"{table}.{column} is empty")
    return value


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _build[T](table: str, make: Callable[[], T]) -> T:
    try:
        return make()
    except ValueError as error:  # a bad enum member or a model validation failure
        raise StoreError(f"a {table} row cannot be read: {error}") from None


# ── intents ──


def _intent_from_row(row: sqlite3.Row) -> StoredIntent:
    def make() -> OrderIntent:
        return OrderIntent(
            intent_id=UUID(row["intent_id"]),
            sleeve=Sleeve(row["sleeve"]),
            thesis_id=row["thesis_id"],
            role=row["role"],
            terms=OrderTerms.model_validate_json(row["terms"]),
            reason=row["reason"],
            mode=ExecutionMode(row["mode"]),
            approval_required=bool(row["approval_required"]),
            created_at=read_stamp(row["created_at"], "intents", "created_at"),
            expires_at=read_stamp(row["expires_at"], "intents", "expires_at"),
            parent_id=None if row["parent_id"] is None else UUID(row["parent_id"]),
            protective_id=None if row["protective_id"] is None else UUID(row["protective_id"]),
            protective=None
            if row["protective_spec"] is None
            else ProtectiveSpec.model_validate_json(row["protective_spec"]),
            state=IntentState(row["state"]),
        )

    return StoredIntent(intent=_build("intents", make), coid=row["coid"])


def _append_event(
    store: Store,
    intent_id: UUID,
    *,
    from_state: IntentState | None,
    to_state: IntentState,
    at: datetime,
    detail: str,
) -> IntentEvent:
    row = store.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM intent_events WHERE intent_id = ?", (str(intent_id),)
    ).fetchone()
    event = IntentEvent(
        intent_id=intent_id,
        seq=int(row[0]) + 1,
        at=at,
        from_state=from_state,
        to_state=to_state,
        detail=detail,
    )
    store.execute(
        "INSERT INTO intent_events (intent_id, seq, at, from_state, to_state, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            str(intent_id),
            event.seq,
            stamp_text(event.at),
            None if from_state is None else from_state.value,
            to_state.value,
            detail,
        ),
    )
    return event


def insert_intent(store: Store, intent: OrderIntent, *, coid: str, at: datetime) -> IntentEvent:
    """Store a new intent and its first event. A repeated intent id or cOID is refused."""
    with store.transaction():
        store.execute(
            f"INSERT INTO intents ({_INTENT_COLUMNS}, conid, side, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(intent.intent_id),
                coid,
                intent.sleeve.value,
                intent.thesis_id,
                intent.role.value,
                intent.terms.model_dump_json(),
                intent.reason,
                intent.mode.value,
                int(intent.approval_required),
                stamp_text(intent.created_at),
                stamp_text(intent.expires_at),
                None if intent.parent_id is None else str(intent.parent_id),
                None if intent.protective_id is None else str(intent.protective_id),
                None if intent.protective is None else intent.protective.model_dump_json(),
                intent.state.value,
                intent.terms.conid,
                intent.terms.side.value,
                stamp_text(at),
            ),
        )
        return _append_event(
            store,
            intent.intent_id,
            from_state=None,
            to_state=intent.state,
            at=at,
            detail="created",
        )


def get_intent(store: Store, intent_id: UUID) -> StoredIntent | None:
    row = store.execute(
        f"SELECT {_INTENT_COLUMNS} FROM intents WHERE intent_id = ?", (str(intent_id),)
    ).fetchone()
    return None if row is None else _intent_from_row(row)


def intent_by_coid(store: Store, coid: str) -> StoredIntent | None:
    row = store.execute(f"SELECT {_INTENT_COLUMNS} FROM intents WHERE coid = ?", (coid,)).fetchone()
    return None if row is None else _intent_from_row(row)


def intents_for_conid(store: Store, conid: int) -> list[StoredIntent]:
    """Every intent on ``conid``, oldest first (uuid7 ids sort by creation)."""
    rows = store.execute(
        f"SELECT {_INTENT_COLUMNS} FROM intents WHERE conid = ? ORDER BY intent_id", (conid,)
    ).fetchall()
    return [_intent_from_row(row) for row in rows]


def intents_in_states(store: Store, states: Iterable[IntentState]) -> list[StoredIntent]:
    """Every intent currently in one of ``states``, oldest first."""
    wanted = [state.value for state in states]
    if not wanted:
        return []
    marks = ", ".join("?" * len(wanted))
    rows = store.execute(
        f"SELECT {_INTENT_COLUMNS} FROM intents WHERE state IN ({marks}) ORDER BY intent_id",
        wanted,
    ).fetchall()
    return [_intent_from_row(row) for row in rows]


def transition_intent(
    store: Store,
    intent_id: UUID,
    *,
    from_state: IntentState,
    to_state: IntentState,
    at: datetime,
    detail: str,
) -> IntentEvent:
    """Move an intent from ``from_state`` to ``to_state`` and append the event.

    A compare-and-set: when the intent is not in ``from_state`` (another process moved it), the
    store refuses and nothing is written. The legality of the move is the state machine's
    (``execution/state.py``), not the store's.
    """
    with store.transaction():
        changed = store.execute(
            "UPDATE intents SET state = ?, updated_at = ? WHERE intent_id = ? AND state = ?",
            (to_state.value, stamp_text(at), str(intent_id), from_state.value),
        ).rowcount
        if changed != 1:
            raise StoreError(
                f"intent {intent_id} is not in state {from_state.value}; nothing was changed"
            )
        return _append_event(
            store, intent_id, from_state=from_state, to_state=to_state, at=at, detail=detail
        )


def record_intent_fact(
    store: Store,
    intent_id: UUID,
    *,
    state: IntentState,
    at: datetime,
    detail: str,
    terms: OrderTerms | None = None,
) -> IntentEvent:
    """Append a fact within ``state`` (a reply, a fill, a modify) without changing it.

    ``terms`` replaces the intent's terms (a modify the broker accepted). Refused when the
    intent is no longer in ``state``.
    """
    with store.transaction():
        if terms is None:
            changed = store.execute(
                "UPDATE intents SET updated_at = ? WHERE intent_id = ? AND state = ?",
                (stamp_text(at), str(intent_id), state.value),
            ).rowcount
        else:
            changed = store.execute(
                "UPDATE intents SET terms = ?, updated_at = ? WHERE intent_id = ? AND state = ? "
                "AND conid = ? AND side = ?",
                (
                    terms.model_dump_json(),
                    stamp_text(at),
                    str(intent_id),
                    state.value,
                    terms.conid,
                    terms.side.value,
                ),
            ).rowcount
        if changed != 1:
            raise StoreError(
                f"intent {intent_id} is not in state {state.value} (or the terms change its "
                "instrument or side); nothing was recorded"
            )
        return _append_event(
            store, intent_id, from_state=state, to_state=state, at=at, detail=detail
        )


def intent_history(store: Store, intent_id: UUID) -> list[IntentEvent]:
    rows = store.execute(
        "SELECT intent_id, seq, at, from_state, to_state, detail FROM intent_events "
        "WHERE intent_id = ? ORDER BY seq",
        (str(intent_id),),
    ).fetchall()
    return [_event_from_row(row) for row in rows]


def _event_from_row(row: sqlite3.Row) -> IntentEvent:
    return _build(
        "intent_events",
        lambda: IntentEvent(
            intent_id=UUID(row["intent_id"]),
            seq=row["seq"],
            at=read_stamp(row["at"], "intent_events", "at"),
            from_state=None if row["from_state"] is None else IntentState(row["from_state"]),
            to_state=IntentState(row["to_state"]),
            detail=row["detail"],
        ),
    )


# ── clearances ──


def record_clearance(store: Store, clearance: PretradeClearance) -> None:
    """Record a clearance the gate issued. Only ``execution/pretrade.py`` calls this."""
    terms = clearance.terms
    with store.transaction():
        store.execute(
            "INSERT INTO clearances (clearance_id, intent_id, action, order_id, conid, side, "
            "quantity, terms_digest, effect, clearance, issued_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(clearance.clearance_id),
                str(clearance.intent_id),
                clearance.action.value,
                clearance.order_id,
                terms.conid,
                terms.side.value,
                _text(terms.quantity),
                clearance.terms_digest,
                clearance.effect.value,
                clearance.model_dump_json(),
                stamp_text(clearance.issued_at),
                stamp_text(clearance.expires_at),
            ),
        )


def get_clearance(store: Store, clearance_id: UUID) -> StoredClearance | None:
    row = store.execute(
        "SELECT clearance, used_at FROM clearances WHERE clearance_id = ?", (str(clearance_id),)
    ).fetchone()
    if row is None:
        return None
    clearance = _build("clearances", lambda: PretradeClearance.model_validate_json(row[0]))
    used = None if row[1] is None else read_stamp(row[1], "clearances", "used_at")
    return StoredClearance(clearance=clearance, used_at=used)


def spend_clearance(
    store: Store,
    clearance_id: UUID,
    *,
    intent_id: UUID,
    action: ClearanceAction,
    order_id: str | None,
    terms_digest: str,
    now: datetime,
) -> str | None:
    """Spend a clearance for exactly this write; ``None`` when spent, else why it was refused.

    One statement matches every binding and marks the clearance used, so two processes can
    never spend it twice. A refusal changes nothing.
    """
    with store.transaction():
        changed = store.execute(
            "UPDATE clearances SET used_at = ? WHERE clearance_id = ? AND used_at IS NULL "
            "AND expires_at > ? AND intent_id = ? AND action = ? AND order_id IS ? "
            "AND terms_digest = ?",
            (
                stamp_text(now),
                str(clearance_id),
                stamp_text(now),
                str(intent_id),
                action.value,
                order_id,
                terms_digest,
            ),
        ).rowcount
        if changed == 1:
            return None
        row = store.execute(
            "SELECT intent_id, action, order_id, terms_digest, expires_at, used_at "
            "FROM clearances WHERE clearance_id = ?",
            (str(clearance_id),),
        ).fetchone()
    if row is None:
        return "no such clearance was issued"
    if row["used_at"] is not None:
        return "the clearance was already used"
    if row["intent_id"] != str(intent_id):
        return "the clearance is for another intent"
    if row["action"] != action.value or row["order_id"] != order_id:
        return "the clearance is for another action or broker order"
    if row["terms_digest"] != terms_digest:
        return "the order's terms are not the terms that were cleared"
    return "the clearance has expired"


_CLEARANCE_JOIN = (
    "SELECT c.clearance, c.used_at, i.coid, i.state FROM clearances AS c "
    "JOIN intents AS i ON i.intent_id = c.intent_id "
)


def _open_clearance(row: sqlite3.Row) -> OpenClearance:
    clearance = _build("clearances", lambda: PretradeClearance.model_validate_json(row[0]))
    return OpenClearance(
        clearance=clearance,
        used_at=None if row[1] is None else read_stamp(row[1], "clearances", "used_at"),
        coid=row[2],
        intent_state=_build("intents", lambda: IntentState(row[3])),
    )


def open_clearances(store: Store) -> list[OpenClearance]:
    """Every clearance of an intent that has not finished, oldest first: what could still reach
    the broker, or may be working there. Read whole: a time bound could hide a sell in flight."""
    terminal = sorted(state.value for state in TERMINAL_STATES)
    marks = ", ".join("?" * len(terminal))
    rows = store.execute(
        f"{_CLEARANCE_JOIN}WHERE i.state NOT IN ({marks}) ORDER BY c.issued_at, c.clearance_id",
        terminal,
    ).fetchall()
    return [_open_clearance(row) for row in rows]


def clearances_issued(store: Store, *, since: datetime, as_of: datetime) -> list[OpenClearance]:
    """Clearances issued after ``since`` and at or before ``as_of``, whatever became of them."""
    rows = store.execute(
        f"{_CLEARANCE_JOIN}WHERE c.issued_at > ? AND c.issued_at <= ? "
        "ORDER BY c.issued_at, c.clearance_id",
        (stamp_text(since), stamp_text(as_of)),
    ).fetchall()
    return [_open_clearance(row) for row in rows]


def spent_effect(store: Store, intent_id: UUID) -> RiskEffect | None:
    """What the intent's spent clearances say about risk: ``adds_risk`` when any of them does
    (a spent modify may never have reached the broker, so the latest cannot be trusted to
    describe the working order), ``reduces_risk`` when all do, ``None`` when none was spent."""
    row = store.execute(
        "SELECT MAX(effect = 'adds_risk'), COUNT(*) FROM clearances "
        "WHERE intent_id = ? AND used_at IS NOT NULL",
        (str(intent_id),),
    ).fetchone()
    if not row[1]:
        return None
    return RiskEffect.ADDS if row[0] else RiskEffect.REDUCES


# ── what the broker reported ──


def upsert_broker_orders(store: Store, orders: Sequence[BrokerOrder]) -> int:
    """Record the latest state of each order; the intent is found by its cOID.

    A field the new report lacks (the status endpoint carries no cOID) keeps its known value.
    """
    with store.transaction():
        for order in orders:
            store.execute(
                "INSERT INTO broker_orders (order_id, coid, intent_id, conid, side, sec_type, "
                "order_type, status, total_quantity, filled_quantity, remaining_quantity, "
                "average_price, tif, observed_at) "
                "VALUES (?, ?, (SELECT intent_id FROM intents WHERE coid = ?), "
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (order_id) DO UPDATE SET "
                "coid = COALESCE(excluded.coid, coid), "
                "intent_id = COALESCE(excluded.intent_id, intent_id), "
                "conid = COALESCE(excluded.conid, conid), side = COALESCE(excluded.side, side), "
                "sec_type = COALESCE(excluded.sec_type, sec_type), "
                "order_type = COALESCE(excluded.order_type, order_type), "
                "status = excluded.status, "
                "total_quantity = COALESCE(excluded.total_quantity, total_quantity), "
                "filled_quantity = COALESCE(excluded.filled_quantity, filled_quantity), "
                "remaining_quantity = COALESCE(excluded.remaining_quantity, remaining_quantity), "
                "average_price = COALESCE(excluded.average_price, average_price), "
                "tif = COALESCE(excluded.tif, tif), observed_at = excluded.observed_at",
                (
                    order.order_id,
                    order.coid,
                    order.coid,
                    order.conid,
                    None if order.side is None else order.side.value,
                    order.sec_type,
                    order.order_type,
                    order.status,
                    _text(order.total_quantity),
                    _text(order.filled_quantity),
                    _text(order.remaining_quantity),
                    _text(order.average_price),
                    order.tif,
                    stamp_text(order.observed_at),
                ),
            )
    return len(orders)


def _broker_order_from_row(row: sqlite3.Row) -> BrokerOrder:
    table = "broker_orders"
    return _build(
        table,
        lambda: BrokerOrder(
            order_id=row["order_id"],
            coid=row["coid"],
            conid=row["conid"],
            side=row["side"],
            sec_type=row["sec_type"],
            order_type=row["order_type"],
            status=row["status"],
            total_quantity=_decimal(row["total_quantity"], table, "total_quantity"),
            filled_quantity=_decimal(row["filled_quantity"], table, "filled_quantity"),
            remaining_quantity=_decimal(row["remaining_quantity"], table, "remaining_quantity"),
            average_price=_decimal(row["average_price"], table, "average_price"),
            tif=row["tif"],
            observed_at=read_stamp(row["observed_at"], table, "observed_at"),
        ),
    )


def intent_for_order(store: Store, order_id: str) -> UUID | None:
    """The intent a broker order was placed for; ``None`` for one the bot did not place."""
    row = store.execute(
        "SELECT intent_id FROM broker_orders WHERE order_id = ?", (order_id,)
    ).fetchone()
    return None if row is None or row[0] is None else UUID(row[0])


def broker_orders_for_intent(store: Store, intent_id: UUID) -> list[BrokerOrder]:
    rows = store.execute(
        "SELECT * FROM broker_orders WHERE intent_id = ? ORDER BY observed_at, order_id",
        (str(intent_id),),
    ).fetchall()
    return [_broker_order_from_row(row) for row in rows]


def upsert_fills(store: Store, fills: Sequence[Fill]) -> int:
    """Record executions; one already recorded (by execution id) is left as it is."""
    inserted = 0
    with store.transaction():
        for fill in fills:
            inserted += store.execute(
                "INSERT OR IGNORE INTO fills (execution_id, intent_id, coid, conid, side, "
                "quantity, price, commission, executed_at, simulated) "
                "VALUES (?, (SELECT intent_id FROM intents WHERE coid = ?), "
                "?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fill.execution_id,
                    fill.coid,
                    fill.coid,
                    fill.conid,
                    fill.side.value,
                    str(fill.quantity),
                    str(fill.price),
                    _text(fill.commission),
                    stamp_text(fill.executed_at),
                    int(fill.simulated),
                ),
            ).rowcount
    return inserted


def fills_for_intent(store: Store, intent_id: UUID) -> list[Fill]:
    rows = store.execute(
        "SELECT * FROM fills WHERE intent_id = ? ORDER BY executed_at, execution_id",
        (str(intent_id),),
    ).fetchall()
    return [_fill_from_row(row) for row in rows]


def _fill_from_row(row: sqlite3.Row) -> Fill:
    table = "fills"
    return _build(
        table,
        lambda: Fill(
            execution_id=row["execution_id"],
            coid=row["coid"],
            conid=row["conid"],
            side=row["side"],
            quantity=_required_decimal(row["quantity"], table, "quantity"),
            price=_required_decimal(row["price"], table, "price"),
            commission=_decimal(row["commission"], table, "commission"),
            executed_at=read_stamp(row["executed_at"], table, "executed_at"),
            simulated=bool(row["simulated"]),
        ),
    )


def fills_by_coid(store: Store, coids: Iterable[str]) -> dict[str, list[Fill]]:
    """The recorded fills of each cOID (absent when none): the live order path's own fills."""
    wanted = sorted(set(coids))
    if not wanted:
        return {}
    marks = ", ".join("?" * len(wanted))
    rows = store.execute(
        f"SELECT * FROM fills WHERE coid IN ({marks}) ORDER BY executed_at, execution_id", wanted
    ).fetchall()
    found: dict[str, list[Fill]] = {}
    for row in rows:
        found.setdefault(row["coid"], []).append(_fill_from_row(row))
    return found


def fills_for_conid(store: Store, conid: int, *, since: datetime, as_of: datetime) -> list[Fill]:
    """The instrument's fills executed at or after ``since`` and at or before ``as_of``."""
    rows = store.execute(
        "SELECT * FROM fills WHERE conid = ? AND executed_at >= ? AND executed_at <= ? "
        "ORDER BY executed_at, execution_id",
        (conid, stamp_text(since), stamp_text(as_of)),
    ).fetchall()
    return [_fill_from_row(row) for row in rows]


def fills_between(store: Store, *, since: datetime, as_of: datetime) -> list[Fill]:
    """Every fill executed at or after ``since`` and at or before ``as_of``."""
    rows = store.execute(
        "SELECT * FROM fills WHERE executed_at >= ? AND executed_at <= ? "
        "ORDER BY executed_at, execution_id",
        (stamp_text(since), stamp_text(as_of)),
    ).fetchall()
    return [_fill_from_row(row) for row in rows]


@dataclass(frozen=True, slots=True)
class IntentFill:
    """A fill of one of the bot's intents, with what the intent was."""

    fill: Fill
    sleeve: Sleeve
    mode: ExecutionMode
    role: IntentRole
    reason: str


def intent_fills(store: Store) -> list[IntentFill]:
    """Every fill of an intent the bot made (foreign fills left out), oldest first."""
    rows = store.execute(
        "SELECT f.*, i.sleeve AS i_sleeve, i.mode AS i_mode, i.role AS i_role, "
        "i.reason AS i_reason FROM fills f JOIN intents i ON i.intent_id = f.intent_id "
        "ORDER BY f.executed_at, f.execution_id"
    ).fetchall()
    return [_intent_fill_from_row(row) for row in rows]


def _intent_fill_from_row(row: sqlite3.Row) -> IntentFill:
    def make() -> IntentFill:
        return IntentFill(
            fill=_fill_from_row(row),
            sleeve=Sleeve(row["i_sleeve"]),
            mode=ExecutionMode(row["i_mode"]),
            role=IntentRole(row["i_role"]),
            reason=row["i_reason"],
        )

    return _build("fills", make)


def _new_read(store: Store, kind: str, observed_at: datetime) -> int:
    cursor = store.execute(
        "INSERT INTO account_reads (kind, observed_at) VALUES (?, ?)",
        (kind, stamp_text(observed_at)),
    )
    if cursor.lastrowid is None:  # pragma: no cover - an INSERT always sets it
        raise StoreError("account read was not recorded")
    return cursor.lastrowid


def record_positions(store: Store, positions: Sequence[Position], *, observed_at: datetime) -> int:
    """Record one complete positions read (an empty one included); returns its read id."""
    with store.transaction():
        read_id = _new_read(store, "positions", observed_at)
        store.executemany(
            "INSERT INTO positions_snap (read_id, conid, quantity, average_cost, currency) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (read_id, p.conid, str(p.quantity), _text(p.average_cost), p.currency)
                for p in positions
            ],
        )
    return read_id


def _latest_read(store: Store, kind: str, as_of: datetime) -> sqlite3.Row | None:
    row: sqlite3.Row | None = store.execute(
        "SELECT read_id, observed_at FROM account_reads WHERE kind = ? AND observed_at <= ? "
        "ORDER BY observed_at DESC, read_id DESC LIMIT 1",
        (kind, stamp_text(as_of)),
    ).fetchone()
    return row


def positions_as_of(store: Store, *, as_of: datetime) -> AccountRead[Position] | None:
    """The latest positions read observed at or before ``as_of``; ``None`` when there is none."""
    with store.read():
        head = _latest_read(store, "positions", as_of)
        if head is None:
            return None
        observed = read_stamp(head["observed_at"], "account_reads", "observed_at")
        rows = store.execute(
            "SELECT conid, quantity, average_cost, currency FROM positions_snap "
            "WHERE read_id = ? ORDER BY conid",
            (head["read_id"],),
        ).fetchall()
    return AccountRead(
        observed_at=observed, rows=tuple(_position_from_row(row, observed) for row in rows)
    )


def _position_from_row(row: sqlite3.Row, observed: datetime) -> Position:
    table = "positions_snap"
    return _build(
        table,
        lambda: Position(
            conid=row["conid"],
            quantity=_required_decimal(row["quantity"], table, "quantity"),
            average_cost=_decimal(row["average_cost"], table, "average_cost"),
            currency=row["currency"],
            observed_at=observed,
        ),
    )


def position_reads(
    store: Store, *, since: datetime, as_of: datetime
) -> list[AccountRead[Position]]:
    """Every positions read observed at or after ``since`` and at or before ``as_of``, oldest
    first."""
    with store.read():
        heads = store.execute(
            "SELECT read_id, observed_at FROM account_reads WHERE kind = 'positions' "
            "AND observed_at >= ? AND observed_at <= ? ORDER BY observed_at, read_id",
            (stamp_text(since), stamp_text(as_of)),
        ).fetchall()
        reads = []
        for head in heads:
            observed = read_stamp(head["observed_at"], "account_reads", "observed_at")
            rows = store.execute(
                "SELECT conid, quantity, average_cost, currency FROM positions_snap "
                "WHERE read_id = ? ORDER BY conid",
                (head["read_id"],),
            ).fetchall()
            reads.append(
                AccountRead(
                    observed_at=observed,
                    rows=tuple(_position_from_row(row, observed) for row in rows),
                )
            )
    return reads


def record_cash(store: Store, balances: Sequence[CashBalance], *, observed_at: datetime) -> int:
    """Record one complete cash read; returns its read id."""
    with store.transaction():
        read_id = _new_read(store, "cash", observed_at)
        store.executemany(
            "INSERT INTO cash_snap (read_id, currency, cash, settled_cash) VALUES (?, ?, ?, ?)",
            [(read_id, b.currency, str(b.cash), _text(b.settled_cash)) for b in balances],
        )
    return read_id


def cash_as_of(store: Store, *, as_of: datetime) -> AccountRead[CashBalance] | None:
    """The latest cash read observed at or before ``as_of``; ``None`` when there is none."""
    with store.read():
        head = _latest_read(store, "cash", as_of)
        if head is None:
            return None
        observed = read_stamp(head["observed_at"], "account_reads", "observed_at")
        rows = store.execute(
            "SELECT currency, cash, settled_cash FROM cash_snap WHERE read_id = ? "
            "ORDER BY currency",
            (head["read_id"],),
        ).fetchall()
    return AccountRead(
        observed_at=observed, rows=tuple(_cash_from_row(row, observed) for row in rows)
    )


def _cash_from_row(row: sqlite3.Row, observed: datetime) -> CashBalance:
    table = "cash_snap"
    return _build(
        table,
        lambda: CashBalance(
            currency=row["currency"],
            cash=_required_decimal(row["cash"], table, "cash"),
            settled_cash=_decimal(row["settled_cash"], table, "settled_cash"),
            observed_at=observed,
        ),
    )


# ── modes, kill switch, holdings ──


def record_mode_change(store: Store, change: ModeChange) -> None:
    with store.transaction():
        store.execute(
            "INSERT INTO mode_audit (at, from_mode, to_mode, direction, actor, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                stamp_text(change.at),
                change.from_mode.value,
                change.to_mode.value,
                change.direction,
                change.actor,
                change.reason,
            ),
        )


def latest_mode_change(store: Store) -> ModeChange | None:
    """The last recorded change (by insertion, not by stamp); ``None`` when there was none."""
    row = store.execute(
        "SELECT at, from_mode, to_mode, direction, actor, reason FROM mode_audit "
        "ORDER BY change_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return _build(
        "mode_audit",
        lambda: ModeChange(
            at=read_stamp(row["at"], "mode_audit", "at"),
            from_mode=ExecutionMode(row["from_mode"]),
            to_mode=ExecutionMode(row["to_mode"]),
            direction=row["direction"],
            actor=row["actor"],
            reason=row["reason"],
        ),
    )


def record_kill_switch(store: Store, record: KillSwitchRecord) -> None:
    with store.transaction():
        store.execute(
            "INSERT INTO killswitch (at, state, actor, reason) VALUES (?, ?, ?, ?)",
            (stamp_text(record.at), "on" if record.on else "off", record.actor, record.reason),
        )


def latest_kill_switch(store: Store) -> KillSwitchRecord | None:
    """The flag's last change (by insertion, not by stamp); ``None`` when it was never set."""
    row = store.execute(
        "SELECT at, state, actor, reason FROM killswitch ORDER BY change_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    if row["state"] not in ("on", "off"):  # pragma: no cover - the CHECK refuses anything else
        raise StoreError(f"killswitch.state holds {row['state']!r}")
    return _build(
        "killswitch",
        lambda: KillSwitchRecord(
            at=read_stamp(row["at"], "killswitch", "at"),
            on=row["state"] == "on",
            actor=row["actor"],
            reason=row["reason"],
        ),
    )


def assign_holding(store: Store, assignment: HoldingAssignment) -> None:
    owner = assignment.owner
    with store.transaction():
        store.execute(
            "INSERT INTO holding_assignments (conid, owner, assigned_by, assigned_at, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                assignment.conid,
                owner if owner == UNMANAGED else Sleeve(owner).value,
                assignment.assigned_by,
                stamp_text(assignment.assigned_at),
                assignment.reason,
            ),
        )


def _assignment_from_row(row: sqlite3.Row) -> HoldingAssignment:
    return _build(
        "holding_assignments",
        lambda: HoldingAssignment(
            conid=row["conid"],
            owner=row["owner"] if row["owner"] == UNMANAGED else Sleeve(row["owner"]),
            assigned_by=row["assigned_by"],
            assigned_at=read_stamp(row["assigned_at"], "holding_assignments", "assigned_at"),
            reason=row["reason"],
        ),
    )


def holding_assignment(store: Store, conid: int) -> HoldingAssignment | None:
    """The holding's current assignment; ``None`` when it was never registered."""
    row = store.execute(
        "SELECT conid, owner, assigned_by, assigned_at, reason FROM holding_assignments "
        "WHERE conid = ? ORDER BY assignment_id DESC LIMIT 1",
        (conid,),
    ).fetchone()
    return None if row is None else _assignment_from_row(row)


def holding_assignments(store: Store) -> list[HoldingAssignment]:
    """Every registered holding's current assignment (its latest row), by conid."""
    rows = store.execute(
        "SELECT conid, owner, assigned_by, assigned_at, reason FROM holding_assignments "
        "WHERE assignment_id IN (SELECT MAX(assignment_id) FROM holding_assignments "
        "GROUP BY conid) ORDER BY conid"
    ).fetchall()
    return [_assignment_from_row(row) for row in rows]


# ── approvals and stop resizes ──


def record_approval(store: Store, decision: ApprovalDecision) -> None:
    with store.transaction():
        store.execute(
            "INSERT INTO approvals (intent_id, decision, actor, reason, at) VALUES (?, ?, ?, ?, ?)",
            (
                str(decision.intent_id),
                "approved" if decision.approved else "rejected",
                decision.actor,
                decision.reason,
                stamp_text(decision.at),
            ),
        )


def approval_for(store: Store, intent_id: UUID) -> ApprovalDecision | None:
    """The owner's latest decision on an intent (by insertion); ``None`` when there is none."""
    row = store.execute(
        "SELECT intent_id, decision, actor, reason, at FROM approvals WHERE intent_id = ? "
        "ORDER BY approval_id DESC LIMIT 1",
        (str(intent_id),),
    ).fetchone()
    if row is None:
        return None
    return _build(
        "approvals",
        lambda: ApprovalDecision(
            intent_id=UUID(row["intent_id"]),
            approved=row["decision"] == "approved",
            actor=row["actor"],
            reason=row["reason"],
            at=read_stamp(row["at"], "approvals", "at"),
        ),
    )


_RESIZE_COLUMNS = (
    "resize_id, exit_intent_id, stop_intent_id, stop_order_id, original_terms, "
    "resized_quantity, state, created_at, updated_at, detail"
)


def _resize_from_row(row: sqlite3.Row) -> StopResize:
    table = "stop_resizes"
    return _build(
        table,
        lambda: StopResize(
            resize_id=row["resize_id"],
            exit_intent_id=UUID(row["exit_intent_id"]),
            stop_intent_id=UUID(row["stop_intent_id"]),
            stop_order_id=row["stop_order_id"],
            original_terms=OrderTerms.model_validate_json(row["original_terms"]),
            resized_quantity=_required_decimal(row["resized_quantity"], table, "resized_quantity"),
            state=StopResizeState(row["state"]),
            created_at=read_stamp(row["created_at"], table, "created_at"),
            updated_at=read_stamp(row["updated_at"], table, "updated_at"),
            detail=row["detail"],
        ),
    )


def insert_stop_resize(store: Store, resize: StopResize) -> int:
    """Record a stop cut for an exit; returns its id."""
    with store.transaction():
        cursor = store.execute(
            "INSERT INTO stop_resizes (exit_intent_id, stop_intent_id, stop_order_id, "
            "original_terms, resized_quantity, state, created_at, updated_at, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(resize.exit_intent_id),
                str(resize.stop_intent_id),
                resize.stop_order_id,
                resize.original_terms.model_dump_json(),
                str(resize.resized_quantity),
                resize.state.value,
                stamp_text(resize.created_at),
                stamp_text(resize.updated_at),
                resize.detail,
            ),
        )
    if cursor.lastrowid is None:  # pragma: no cover - an INSERT always sets it
        raise StoreError("the stop resize was not recorded")
    return cursor.lastrowid


def record_stop_resize_state(
    store: Store, resize_id: int, *, state: StopResizeState, at: datetime, detail: str
) -> None:
    """Move a resize to ``state``; a settled one (closed, restored) is refused by the schema."""
    with store.transaction():
        changed = store.execute(
            "UPDATE stop_resizes SET state = ?, updated_at = ?, detail = ? WHERE resize_id = ?",
            (state.value, stamp_text(at), detail, resize_id),
        ).rowcount
    if changed != 1:
        raise StoreError(f"no stop resize {resize_id}; nothing was changed")


def stop_resizes_in_states(store: Store, states: Iterable[StopResizeState]) -> list[StopResize]:
    """Every resize in one of ``states``, oldest first."""
    wanted = [state.value for state in states]
    if not wanted:
        return []
    marks = ", ".join("?" * len(wanted))
    rows = store.execute(
        f"SELECT {_RESIZE_COLUMNS} FROM stop_resizes WHERE state IN ({marks}) ORDER BY resize_id",
        wanted,
    ).fetchall()
    return [_resize_from_row(row) for row in rows]


def stop_resizes_for_exit(store: Store, exit_intent_id: UUID) -> list[StopResize]:
    rows = store.execute(
        f"SELECT {_RESIZE_COLUMNS} FROM stop_resizes WHERE exit_intent_id = ? ORDER BY resize_id",
        (str(exit_intent_id),),
    ).fetchall()
    return [_resize_from_row(row) for row in rows]


# ── reconciliations ──


def record_reconciliation(
    store: Store,
    run: ReconcileRun,
    *,
    positions_read: int | None = None,
    cash_read: int | None = None,
    anchor: bool = False,
) -> None:
    """Record a run; ``anchor``: its reads are what later runs compare the broker against."""
    findings = [finding.model_dump(mode="json") for finding in run.findings]
    with store.transaction():
        store.execute(
            "INSERT INTO reconciliations (at, status, findings, positions_read, cash_read, anchor) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                stamp_text(run.at),
                run.status.value,
                json.dumps(findings, sort_keys=True),
                positions_read,
                cash_read,
                int(anchor),
            ),
        )


def latest_reconciliation(store: Store) -> ReconcileRun | None:
    """The run in force: the latest by insertion, not by stamp; ``None`` before the first."""
    row = store.execute(
        "SELECT at, status, findings FROM reconciliations ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return _build(
        "reconciliations",
        lambda: ReconcileRun(
            at=read_stamp(row["at"], "reconciliations", "at"),
            status=ReconcileStatus(row["status"]),
            findings=tuple(
                ReconcileFinding.model_validate(item) for item in json.loads(row["findings"])
            ),
        ),
    )


def _read_by_id[T](
    store: Store, read_id: int, kind: str, make: Callable[[sqlite3.Row, datetime], T]
) -> AccountRead[T]:
    head = store.execute(
        "SELECT observed_at FROM account_reads WHERE read_id = ? AND kind = ?", (read_id, kind)
    ).fetchone()
    if head is None:
        raise StoreError(f"no {kind} read {read_id}")
    observed = read_stamp(head["observed_at"], "account_reads", "observed_at")
    table = "positions_snap" if kind == "positions" else "cash_snap"
    rows = store.execute(f"SELECT * FROM {table} WHERE read_id = ?", (read_id,)).fetchall()
    return AccountRead(observed_at=observed, rows=tuple(make(row, observed) for row in rows))


def reconciliation_anchor(
    store: Store,
) -> tuple[AccountRead[Position], AccountRead[CashBalance]] | None:
    """The positions and cash reads of the latest anchoring run (green and settled), which
    later runs compare the broker against; ``None`` before the first."""
    with store.read():
        row = store.execute(
            "SELECT positions_read, cash_read FROM reconciliations WHERE anchor = 1 "
            "ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return (
            _read_by_id(store, row["positions_read"], "positions", _position_from_row),
            _read_by_id(store, row["cash_read"], "cash", _cash_from_row),
        )
