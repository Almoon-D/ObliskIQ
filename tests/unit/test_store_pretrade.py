"""The store of the pre-trade rails (``003_pretrade.sql``): the owner's approvals and the stops
cut for an exit, and the reads the gate counts with.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from order_rig import CONID, quote, terms

from obliskiq.domain.enums import AssetClass
from obliskiq.domain.orders import (
    ApprovalDecision,
    ClearanceAction,
    ExecutionMode,
    Fill,
    IntentRole,
    IntentState,
    OrderIntent,
    OrderSide,
    Position,
    PretradeClearance,
    RiskEffect,
    Sleeve,
    StopResize,
    StopResizeState,
)
from obliskiq.errors import StoreError
from obliskiq.store import Store
from obliskiq.store import orders as repo

NOW = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)


def stored_intent(store: Store, **overrides: Any) -> OrderIntent:
    fields: dict[str, Any] = {
        "intent_id": uuid.uuid7(),
        "sleeve": Sleeve.CORE,
        "role": IntentRole.EXIT,
        "terms": terms(side=OrderSide.SELL),
        "reason": "target",
        "mode": ExecutionMode.PAPER,
        "approval_required": False,
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
    }
    fields.update(overrides)
    intent = OrderIntent(**fields)
    repo.insert_intent(store, intent, coid=str(intent.intent_id), at=NOW)
    return intent


def resize_of(exit_intent: OrderIntent, stop: OrderIntent) -> StopResize:
    return StopResize(
        exit_intent_id=exit_intent.intent_id,
        stop_intent_id=stop.intent_id,
        stop_order_id="1001",
        original_terms=terms(side=OrderSide.SELL, stop="95", limit="94"),
        resized_quantity=Decimal(6),
        state=StopResizeState.RESIZED,
        created_at=NOW,
        updated_at=NOW,
        detail="cut for the exit",
    )


def test_approvals_are_append_only_and_the_latest_decides(store: Store) -> None:
    intent = stored_intent(store)
    assert repo.approval_for(store, intent.intent_id) is None
    first = ApprovalDecision(
        intent_id=intent.intent_id, approved=False, actor="owner", reason="no", at=NOW
    )
    repo.record_approval(store, first)
    repo.record_approval(store, first.model_copy(update={"approved": True, "reason": "yes"}))
    latest = repo.approval_for(store, intent.intent_id)
    assert latest is not None
    assert (latest.approved, latest.reason) == (True, "yes")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.execute("UPDATE approvals SET decision = 'rejected'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.execute("DELETE FROM approvals")


def test_a_stop_resize_moves_only_its_state_and_settles_once(store: Store) -> None:
    exit_intent, stop = stored_intent(store), stored_intent(store, role=IntentRole.PROTECTIVE)
    resize_id = repo.insert_stop_resize(store, resize_of(exit_intent, stop))
    (stored,) = repo.stop_resizes_for_exit(store, exit_intent.intent_id)
    assert stored.resize_id == resize_id
    assert stored.original_terms.quantity == Decimal(10)
    assert repo.stop_resizes_in_states(store, [StopResizeState.RESIZED]) == [stored]
    assert repo.stop_resizes_in_states(store, []) == []
    later = NOW + timedelta(minutes=1)
    repo.record_stop_resize_state(
        store, resize_id, state=StopResizeState.RESTORE_FAILED, at=later, detail="refused"
    )
    repo.record_stop_resize_state(
        store, resize_id, state=StopResizeState.RESTORED, at=later, detail="restored"
    )
    with pytest.raises(StoreError, match="a settled stop resize never changes"):
        repo.record_stop_resize_state(
            store, resize_id, state=StopResizeState.RESIZED, at=later, detail="again"
        )
    with pytest.raises(sqlite3.IntegrityError, match="identity"):
        store.execute("UPDATE stop_resizes SET resized_quantity = '1'")
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        store.execute("DELETE FROM stop_resizes")
    with pytest.raises(StoreError, match="no stop resize 99"):
        repo.record_stop_resize_state(
            store, 99, state=StopResizeState.CLOSED, at=later, detail="none"
        )


def clearance_for(intent: OrderIntent, *, issued: datetime) -> PretradeClearance:
    return PretradeClearance(
        clearance_id=uuid.uuid4(),
        intent_id=intent.intent_id,
        action=ClearanceAction.PLACE,
        terms=intent.terms,
        effect=RiskEffect.REDUCES,
        asset_class=AssetClass.STOCK,
        quote=quote(at=issued, conid=intent.conid),
        issued_at=issued,
        expires_at=issued + timedelta(seconds=60),
    )


def test_the_open_clearances_are_those_of_unfinished_intents(store: Store) -> None:
    working, done = stored_intent(store), stored_intent(store)
    for intent in (working, done):
        repo.record_clearance(store, clearance_for(intent, issued=NOW))
    repo.transition_intent(
        store,
        done.intent_id,
        from_state=IntentState.PROPOSED,
        to_state=IntentState.CANCELLED,
        at=NOW,
        detail="test",
    )
    (entry,) = repo.open_clearances(store)
    assert entry.clearance.intent_id == working.intent_id
    assert (entry.used_at, entry.coid) == (None, str(working.intent_id))
    window = repo.clearances_issued(store, since=NOW - timedelta(days=1), as_of=NOW)
    assert len(window) == 2  # the day's clearances, whatever became of them
    assert repo.clearances_issued(store, since=NOW, as_of=NOW) == []


def test_fills_and_position_reads_are_read_by_window(store: Store) -> None:
    fill = Fill(
        execution_id="e1",
        coid="c1",
        conid=CONID,
        side=OrderSide.SELL,
        quantity=Decimal(2),
        price=Decimal(100),
        commission=None,
        executed_at=NOW,
    )
    repo.upsert_fills(store, [fill])
    assert repo.fills_by_coid(store, ["c1", "c2"]) == {"c1": [fill]}
    assert repo.fills_by_coid(store, []) == {}
    assert repo.fills_for_conid(store, CONID, since=NOW, as_of=NOW) == [fill]
    assert repo.fills_for_conid(store, CONID, since=NOW + timedelta(seconds=1), as_of=NOW) == []
    held = Position(
        conid=CONID, quantity=Decimal(5), average_cost=None, currency="USD", observed_at=NOW
    )
    repo.record_positions(store, [held], observed_at=NOW)
    repo.record_positions(store, [], observed_at=NOW + timedelta(minutes=1))
    reads = repo.position_reads(store, since=NOW, as_of=NOW + timedelta(minutes=1))
    assert [len(read.rows) for read in reads] == [1, 0]
    assert repo.position_reads(store, since=NOW, as_of=NOW - timedelta(seconds=1)) == []
