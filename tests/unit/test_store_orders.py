"""The order path's tables: intents and their append-only history, clearances spent once, broker
reports, account reads, modes, the kill switch and the holdings registry.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from order_rig import quote, terms

from obliskiq.domain.enums import AssetClass
from obliskiq.domain.orders import (
    UNMANAGED,
    BrokerOrder,
    CashBalance,
    ClearanceAction,
    ExecutionMode,
    Fill,
    HoldingAssignment,
    IntentRole,
    IntentState,
    KillSwitchRecord,
    ModeChange,
    OrderIntent,
    OrderSide,
    Position,
    PretradeClearance,
    RiskEffect,
    Sleeve,
)
from obliskiq.errors import StoreError
from obliskiq.store import Store
from obliskiq.store import orders as repo

NOW = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)


def _intent(**overrides: Any) -> OrderIntent:
    fields: dict[str, Any] = {
        "intent_id": uuid.uuid7(),
        "sleeve": Sleeve.SATELLITE,
        "thesis_id": "thesis-1",
        "role": IntentRole.ENTRY,
        "terms": terms(),
        "reason": "entry",
        "mode": ExecutionMode.PAPER,
        "approval_required": True,
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
    }
    fields.update(overrides)
    return OrderIntent(**fields)


def _stored(store: Store, **overrides: Any) -> OrderIntent:
    intent = _intent(**overrides)
    repo.insert_intent(store, intent, coid=str(intent.intent_id), at=NOW)
    return intent


def _clearance(intent: OrderIntent, **overrides: Any) -> PretradeClearance:
    fields: dict[str, Any] = {
        "clearance_id": uuid.uuid4(),
        "intent_id": intent.intent_id,
        "action": ClearanceAction.PLACE,
        "terms": intent.terms,
        "effect": RiskEffect.ADDS,
        "asset_class": AssetClass.STOCK,
        "quote": quote(at=NOW, conid=intent.conid),
        "sigma": 0.3,
        "issued_at": NOW,
        "expires_at": NOW + timedelta(seconds=60),
    }
    fields.update(overrides)
    return PretradeClearance(**fields)


# ── intents ──


def test_an_intent_round_trips_with_its_first_event(store: Store) -> None:
    parent = _stored(store)
    intent = _stored(store, parent_id=parent.intent_id, role=IntentRole.EXIT, reason="target")
    stored = repo.get_intent(store, intent.intent_id)
    assert stored is not None
    assert stored.intent == intent
    assert stored.coid == str(intent.intent_id)
    assert repo.intent_by_coid(store, stored.coid) == stored
    (event,) = repo.intent_history(store, intent.intent_id)
    assert (event.seq, event.from_state, event.to_state) == (1, None, IntentState.PROPOSED)
    assert repo.get_intent(store, uuid.uuid7()) is None
    assert repo.intent_by_coid(store, "nope") is None


def test_a_repeated_intent_or_coid_is_refused(store: Store) -> None:
    intent = _stored(store)
    with pytest.raises(StoreError):
        repo.insert_intent(store, intent, coid="another", at=NOW)
    with pytest.raises(StoreError):
        repo.insert_intent(store, _intent(), coid=str(intent.intent_id), at=NOW)


def test_a_transition_is_a_compare_and_set_with_its_event(store: Store) -> None:
    intent = _stored(store)
    event = repo.transition_intent(
        store,
        intent.intent_id,
        from_state=IntentState.PROPOSED,
        to_state=IntentState.APPROVED,
        at=NOW,
        detail="approved",
    )
    assert (event.seq, event.from_state, event.to_state) == (
        2,
        IntentState.PROPOSED,
        IntentState.APPROVED,
    )
    with pytest.raises(StoreError):  # another process moved it first
        repo.transition_intent(
            store,
            intent.intent_id,
            from_state=IntentState.PROPOSED,
            to_state=IntentState.REJECTED,
            at=NOW,
            detail="late",
        )
    assert len(repo.intent_history(store, intent.intent_id)) == 2
    assert [s.intent.intent_id for s in repo.intents_in_states(store, [IntentState.APPROVED])] == [
        intent.intent_id
    ]
    assert repo.intents_in_states(store, []) == []


def test_a_fact_keeps_the_state_and_a_modify_replaces_the_terms(store: Store) -> None:
    intent = _stored(store)
    repo.record_intent_fact(store, intent.intent_id, state=IntentState.PROPOSED, at=NOW, detail="x")
    moved = terms(limit="99")
    repo.record_intent_fact(
        store, intent.intent_id, state=IntentState.PROPOSED, at=NOW, detail="m", terms=moved
    )
    stored = repo.get_intent(store, intent.intent_id)
    assert stored is not None
    assert stored.intent.terms == moved
    with pytest.raises(StoreError):  # the wrong state
        repo.record_intent_fact(
            store, intent.intent_id, state=IntentState.WORKING, at=NOW, detail="x"
        )
    with pytest.raises(StoreError):  # a modify that changes the side
        repo.record_intent_fact(
            store,
            intent.intent_id,
            state=IntentState.PROPOSED,
            at=NOW,
            detail="x",
            terms=terms(side=OrderSide.SELL),
        )


def test_the_history_is_append_only(store: Store) -> None:
    _stored(store)
    for statement in (
        "UPDATE intent_events SET detail = 'rewritten'",
        "DELETE FROM intent_events",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store.execute(statement)


@pytest.mark.parametrize("table", ["mode_audit", "killswitch", "holding_assignments"])
def test_the_audit_tables_are_append_only(store: Store, table: str) -> None:
    repo.record_mode_change(
        store,
        ModeChange(
            at=NOW,
            from_mode=ExecutionMode.SHADOW,
            to_mode=ExecutionMode.PAPER,
            direction="upgrade",
            actor="owner",
            reason="r",
        ),
    )
    repo.record_kill_switch(store, KillSwitchRecord(at=NOW, on=True, actor="owner", reason="r"))
    repo.assign_holding(
        store,
        HoldingAssignment(conid=1, owner=UNMANAGED, assigned_by="bot", assigned_at=NOW, reason="r"),
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.execute(f"DELETE FROM {table}")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.execute(f"UPDATE {table} SET reason = 'x'")


def test_an_unreadable_intent_row_raises(store: Store) -> None:
    intent = _stored(store)
    store.execute("UPDATE intents SET terms = '{}' WHERE intent_id = ?", (str(intent.intent_id),))
    with pytest.raises(StoreError):
        repo.get_intent(store, intent.intent_id)


def test_an_intents_identity_never_changes(store: Store) -> None:
    intent = _stored(store)
    for column, value in (("side", "SELL"), ("conid", 1), ("coid", "x"), ("role", "exit")):
        with pytest.raises(sqlite3.IntegrityError, match="identity"):
            store.execute(
                f"UPDATE intents SET {column} = ? WHERE intent_id = ?",
                (value, str(intent.intent_id)),
            )


def test_a_clearance_is_spent_once_and_never_changed(store: Store) -> None:
    intent = _stored(store)
    clearance = _clearance(intent)
    repo.record_clearance(store, clearance)
    key = (str(clearance.clearance_id),)
    for statement in (
        "UPDATE clearances SET effect = 'reduces_risk' WHERE clearance_id = ?",
        "UPDATE clearances SET expires_at = '2030-01-01T00:00:00Z' WHERE clearance_id = ?",
        "DELETE FROM clearances WHERE clearance_id = ?",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            store.execute(statement, key)
    store.execute(
        "UPDATE clearances SET used_at = '2026-10-13T14:00:01Z' WHERE clearance_id = ?", key
    )
    for statement in (
        "UPDATE clearances SET used_at = NULL WHERE clearance_id = ?",
        "UPDATE clearances SET used_at = '2026-10-13T14:00:02Z' WHERE clearance_id = ?",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="spent once"):
            store.execute(statement, key)


# ── clearances ──


def test_a_clearance_is_spent_once_for_exactly_its_write(store: Store) -> None:
    intent = _stored(store)
    clearance = _clearance(intent)
    repo.record_clearance(store, clearance)
    stored = repo.get_clearance(store, clearance.clearance_id)
    assert stored is not None
    assert stored.clearance == clearance
    assert stored.used_at is None

    def spend(**overrides: Any) -> str | None:
        fields: dict[str, Any] = {
            "intent_id": intent.intent_id,
            "action": ClearanceAction.PLACE,
            "order_id": None,
            "terms_digest": intent.terms.digest(),
            "now": NOW + timedelta(seconds=1),
        }
        fields.update(overrides)
        return repo.spend_clearance(store, clearance.clearance_id, **fields)

    assert "another intent" in (spend(intent_id=uuid.uuid7()) or "")
    assert "another action" in (spend(action=ClearanceAction.MODIFY) or "")
    assert "another action" in (spend(order_id="1") or "")
    assert "terms" in (spend(terms_digest=terms(limit="1").digest()) or "")
    assert "expired" in (spend(now=NOW + timedelta(seconds=60)) or "")
    assert spend() is None
    assert "already used" in (spend() or "")
    assert (
        repo.spend_clearance(
            store,
            uuid.uuid4(),
            intent_id=intent.intent_id,
            action=ClearanceAction.PLACE,
            order_id=None,
            terms_digest="0" * 64,
            now=NOW,
        )
        == "no such clearance was issued"
    )
    spent = repo.get_clearance(store, clearance.clearance_id)
    assert spent is not None
    assert spent.used_at is not None
    assert repo.spent_effect(store, intent.intent_id) is RiskEffect.ADDS
    assert repo.get_clearance(store, uuid.uuid4()) is None


def test_a_clearance_needs_its_intent_and_a_coherent_effect(store: Store) -> None:
    with pytest.raises(StoreError):
        repo.record_clearance(store, _clearance(_intent()))  # no such intent
    intent = _stored(store)
    with pytest.raises(sqlite3.IntegrityError):
        store.execute(
            "INSERT INTO clearances VALUES ('x' || hex(randomblob(17)), ?, 'place', NULL, 1, "
            "'BUY', '1', ?, 'reduces_risk', '{}', ?, ?, NULL)",
            (str(intent.intent_id), "0" * 64, "2026-10-13T14:00:00Z", "2026-10-13T14:01:00Z"),
        )


def test_no_spent_clearance_has_no_effect(store: Store) -> None:
    intent = _stored(store)
    repo.record_clearance(store, _clearance(intent))
    assert repo.spent_effect(store, intent.intent_id) is None


# ── broker reports ──


def _order(order_id: str, **overrides: Any) -> BrokerOrder:
    fields: dict[str, Any] = {
        "order_id": order_id,
        "coid": None,
        "conid": 265598,
        "side": OrderSide.BUY,
        "sec_type": "STK",
        "order_type": "LMT",
        "status": "Submitted",
        "total_quantity": Decimal(10),
        "filled_quantity": Decimal(0),
        "remaining_quantity": Decimal(10),
        "average_price": None,
        "tif": "DAY",
        "observed_at": NOW,
    }
    fields.update(overrides)
    return BrokerOrder(**fields)


def test_broker_orders_link_to_their_intent_and_keep_what_a_later_report_lacks(
    store: Store,
) -> None:
    intent = _stored(store)
    coid = str(intent.intent_id)
    repo.upsert_broker_orders(store, [_order("1", coid=coid), _order("2")])
    assert repo.intent_for_order(store, "1") == intent.intent_id
    assert repo.intent_for_order(store, "2") is None
    assert repo.intent_for_order(store, "3") is None
    later = _order(
        "1", coid=None, conid=None, side=None, status="Filled", filled_quantity=Decimal(10)
    )
    repo.upsert_broker_orders(store, [later])
    (order,) = repo.broker_orders_for_intent(store, intent.intent_id)
    assert (order.status, order.coid, order.side) == ("Filled", coid, OrderSide.BUY)
    assert order.filled_quantity == Decimal(10)


def test_fills_are_recorded_once(store: Store) -> None:
    intent = _stored(store)
    fill = Fill(
        execution_id="e1",
        coid=str(intent.intent_id),
        conid=265598,
        side=OrderSide.BUY,
        quantity=Decimal("2.5"),
        price=Decimal("100.10"),
        commission=None,
        executed_at=NOW,
    )
    assert repo.upsert_fills(store, [fill, fill]) == 1
    assert repo.fills_for_intent(store, intent.intent_id) == [fill]


def test_a_corrupt_decimal_raises(store: Store) -> None:
    intent = _stored(store)
    repo.upsert_broker_orders(store, [_order("1", coid=str(intent.intent_id))])
    store.execute("UPDATE broker_orders SET filled_quantity = 'lots'")
    with pytest.raises(StoreError):
        repo.broker_orders_for_intent(store, intent.intent_id)


def test_account_reads_are_point_in_time_and_an_empty_read_is_not_no_read(store: Store) -> None:
    assert repo.positions_as_of(store, as_of=NOW) is None
    assert repo.cash_as_of(store, as_of=NOW) is None
    position = Position(
        conid=1, quantity=Decimal(5), average_cost=Decimal(10), currency="EUR", observed_at=NOW
    )
    repo.record_positions(store, [position], observed_at=NOW)
    repo.record_positions(store, [], observed_at=NOW + timedelta(minutes=5))
    earlier = repo.positions_as_of(store, as_of=NOW + timedelta(minutes=1))
    assert earlier is not None
    assert earlier.rows == (position,)
    latest = repo.positions_as_of(store, as_of=NOW + timedelta(hours=1))
    assert latest is not None
    assert latest.rows == ()
    balance = CashBalance(currency="EUR", cash=Decimal("-0.00"), settled_cash=None, observed_at=NOW)
    repo.record_cash(store, [balance], observed_at=NOW)
    cash = repo.cash_as_of(store, as_of=NOW)
    assert cash is not None
    assert cash.rows == (balance,)
    assert repo.cash_as_of(store, as_of=NOW - timedelta(seconds=1)) is None


# ── modes, kill switch, holdings ──


def test_the_latest_mode_change_and_kill_switch_are_by_insertion(store: Store) -> None:
    assert repo.latest_mode_change(store) is None
    assert repo.latest_kill_switch(store) is None
    up = ModeChange(
        at=NOW,
        from_mode=ExecutionMode.SHADOW,
        to_mode=ExecutionMode.PAPER,
        direction="upgrade",
        actor="owner",
        reason="r",
    )
    down = ModeChange(
        at=NOW - timedelta(days=1),  # a clock that stepped back still wins by insertion
        from_mode=ExecutionMode.PAPER,
        to_mode=ExecutionMode.SHADOW,
        direction="downgrade",
        actor="system",
        reason="breaker",
    )
    repo.record_mode_change(store, up)
    repo.record_mode_change(store, down)
    assert repo.latest_mode_change(store) == down
    repo.record_kill_switch(store, KillSwitchRecord(at=NOW, on=True, actor="owner", reason="r"))
    repo.record_kill_switch(
        store, KillSwitchRecord(at=NOW - timedelta(days=1), on=False, actor="owner", reason="r")
    )
    latest = repo.latest_kill_switch(store)
    assert latest is not None
    assert latest.on is False


def test_the_holdings_registry(store: Store) -> None:
    assert repo.holding_assignment(store, 1) is None
    first = HoldingAssignment(
        conid=1, owner=UNMANAGED, assigned_by="reconciliation", assigned_at=NOW, reason="found"
    )
    repo.assign_holding(store, first)
    assert repo.holding_assignment(store, 1) == first
    second = HoldingAssignment(
        conid=1, owner=Sleeve.CORE, assigned_by="owner", assigned_at=NOW, reason="released"
    )
    repo.assign_holding(store, second)
    assert repo.holding_assignment(store, 1) == second


def test_a_reconciliation_anchor_reads_back_its_reads_by_kind(store: Store) -> None:
    from obliskiq.domain.orders import CashBalance, ReconcileRun, ReconcileStatus

    at = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
    assert repo.reconciliation_anchor(store) is None
    cash = repo.record_cash(
        store,
        [CashBalance(currency="EUR", cash=Decimal(10), settled_cash=None, observed_at=at)],
        observed_at=at,
    )
    positions = repo.record_positions(store, [], observed_at=at)
    green = ReconcileRun(at=at, status=ReconcileStatus.GREEN, findings=())
    repo.record_reconciliation(store, green, positions_read=positions, cash_read=cash, anchor=True)
    read_positions, read_cash = repo.reconciliation_anchor(store) or ((), ())
    assert read_positions.rows == ()
    assert [row.currency for row in read_cash.rows] == ["EUR"]
    # Reads of the wrong kind cannot anchor a run.
    repo.record_reconciliation(store, green, positions_read=cash, cash_read=cash, anchor=True)
    with pytest.raises(StoreError, match="no positions read"):
        repo.reconciliation_anchor(store)
