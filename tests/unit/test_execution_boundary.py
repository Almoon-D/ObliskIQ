"""The broker-write boundary on the fake broker: every refusal it makes, the clearance lock (I1),
the kill switch at the boundary (I3, I4), modes and the paper guard, and what each placement
outcome does to the intent.

Adapted from the legacy paper gate's enforced block (legacy/old/ObliskIQ/tests/test_paper_gate.py:
``test_ibkr_write_blocked_when_live_disabled``, ``test_ibkr_place_order_blocked``): a write the
rules do not allow raises before anything reaches the broker.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections import deque
from decimal import Decimal
from typing import Any

import pytest
from order_rig import quote, terms

from obliskiq.domain.enums import MarketDataStatus
from obliskiq.domain.orders import (
    ClearanceAction,
    ExecutionMode,
    IntentRole,
    IntentState,
    OrderSide,
    RiskEffect,
)
from obliskiq.errors import StoreError, WriteRefusedError
from obliskiq.execution.boundary import OrderAlertKind, adds_risk, state_for
from obliskiq.execution.pretrade import PRETRADE_UNAVAILABLE, PretradeGate, PretradeRefusal
from obliskiq.ibkr.orders import PlacementStatus
from obliskiq.store import orders as repo


async def _state(rig: Any, stored: Any) -> IntentState:
    return (await rig.book.get(stored.intent.intent_id)).intent.state


async def _refused(rig: Any, stored: Any, clearance: Any) -> str:
    with pytest.raises(WriteRefusedError) as refused:
        await rig.boundary.place(stored.intent.intent_id, clearance)
    return refused.value.code


# ── I1: no write without a clearance the gate issued for exactly this write ──


async def test_a_cleared_intent_is_placed_and_working(order_rig: Any) -> None:
    stored = await order_rig.intent()
    result = await order_rig.boundary.place(
        stored.intent.intent_id, await order_rig.clear(stored.intent)
    )
    assert result.status is PlacementStatus.PLACED
    assert await _state(order_rig, stored) is IntentState.WORKING
    history = await order_rig.book.history(stored.intent.intent_id)
    assert [e.to_state for e in history][-2:] == [IntentState.SUBMITTED, IntentState.WORKING]
    orders = await order_rig.store(repo.broker_orders_for_intent, stored.intent.intent_id)
    assert [o.order_id for o in orders] == [result.order_id]


async def test_a_placement_without_a_recorded_clearance_is_refused(order_rig: Any) -> None:
    stored = await order_rig.intent()
    forged = await order_rig.clear(stored.intent, record=False)
    assert await _refused(order_rig, stored, forged) == "clearance_unknown"
    assert order_rig.broker.placements == []
    assert await _state(order_rig, stored) is IntentState.REJECTED


async def test_a_clearance_is_used_once(order_rig: Any) -> None:
    import httpx

    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent)
    order_rig.broker.respond_next = httpx.Response(401)  # spent, then refused before processing
    first = await order_rig.boundary.place(stored.intent.intent_id, clearance)
    assert first.status is PlacementStatus.NOT_SENT
    assert await _state(order_rig, stored) is IntentState.SUBMITTED
    assert await _refused(order_rig, stored, clearance) == "clearance_refused"
    assert len(order_rig.broker.placements) == 1
    working = await order_rig.boundary.place(
        stored.intent.intent_id, await order_rig.clear(stored.intent)
    )
    assert working.status is PlacementStatus.PLACED
    # A finished intent is never placed again, whatever it presents.
    code = await _refused(order_rig, stored, await order_rig.clear(stored.intent))
    assert code == "intent_state"


async def test_an_expired_clearance_is_refused(order_rig: Any) -> None:
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent, ttl_seconds=30)
    order_rig.clock.advance(31)
    assert await _refused(order_rig, stored, clearance) == "clearance_refused"
    assert order_rig.broker.placements == []


async def test_a_clearance_for_changed_terms_is_refused(order_rig: Any) -> None:
    stored = await order_rig.intent()
    other_terms = terms(quantity="11")
    clearance = await order_rig.clear(stored.intent, order_terms=other_terms)
    assert await _refused(order_rig, stored, clearance) == "clearance_refused"
    assert order_rig.broker.placements == []


async def test_an_altered_copy_of_a_clearance_is_refused(order_rig: Any) -> None:
    stored = await order_rig.intent(
        terms(side=OrderSide.SELL), role=IntentRole.EXIT, reason="target"
    )
    clearance = await order_rig.clear(stored.intent)
    forged = clearance.model_copy(update={"effect": RiskEffect.REDUCES})
    assert await _refused(order_rig, stored, forged) == "clearance_altered"


async def test_a_clearance_for_another_intent_is_refused(order_rig: Any) -> None:
    stored = await order_rig.intent()
    other = await order_rig.intent()
    clearance = await order_rig.clear(other.intent)
    assert await _refused(order_rig, stored, clearance) == "clearance_mismatch"


async def test_the_shipped_gate_refuses_every_intent(order_rig: Any) -> None:
    gate = PretradeGate()
    for side, role, reason in (
        (OrderSide.BUY, IntentRole.ENTRY, "entry"),
        (OrderSide.SELL, IntentRole.EXIT, "target"),
    ):
        stored = await order_rig.intent(terms(side=side), role=role, reason=reason)
        for action, order_id in ((ClearanceAction.PLACE, None), (ClearanceAction.MODIFY, "1")):
            answer = await gate.clear(stored.intent, action=action, order_id=order_id)
            assert isinstance(answer, PretradeRefusal)
            assert answer.code == PRETRADE_UNAVAILABLE
            assert answer.intent_id == stored.intent.intent_id


async def test_an_intent_not_yet_staged_or_already_done_is_not_placed(order_rig: Any) -> None:
    approved = await order_rig.intent(state=IntentState.APPROVED)
    code = await _refused(order_rig, approved, await order_rig.clear(approved.intent))
    assert code == "intent_state"


async def test_a_duplicate_intent_is_refused_by_the_store(order_rig: Any) -> None:
    stored = await order_rig.intent()
    with pytest.raises(StoreError):
        await order_rig.book.add(stored.intent.model_copy(update={"state": IntentState.PROPOSED}))


# ── modify ──


async def _working_stop(rig: Any) -> tuple[Any, str]:
    stored = await rig.intent(
        terms(side=OrderSide.SELL, stop="95", limit="94"), role=IntentRole.PROTECTIVE, reason="stop"
    )
    result = await rig.boundary.place(
        stored.intent.intent_id, await rig.clear(stored.intent, effect=RiskEffect.REDUCES)
    )
    return stored, result.order_id


async def test_a_modify_with_its_clearance_changes_the_order_and_the_intent(order_rig: Any) -> None:
    stored, order_id = await _working_stop(order_rig)
    moved = terms(side=OrderSide.SELL, stop="97", limit="96")
    clearance = await order_rig.clear(
        stored.intent,
        effect=RiskEffect.REDUCES,
        action=ClearanceAction.MODIFY,
        order_id=order_id,
        order_terms=moved,
    )
    result = await order_rig.boundary.modify(
        stored.intent.intent_id, order_id=order_id, terms=moved, clearance=clearance
    )
    assert result.status is PlacementStatus.PLACED
    assert (await order_rig.book.get(stored.intent.intent_id)).intent.terms == moved


async def test_a_moved_stop_needs_its_own_clearance(order_rig: Any) -> None:
    stored, order_id = await _working_stop(order_rig)
    cleared = terms(side=OrderSide.SELL, stop="97", limit="96")
    moved_further = terms(side=OrderSide.SELL, stop="98", limit="96")
    clearance = await order_rig.clear(
        stored.intent,
        effect=RiskEffect.REDUCES,
        action=ClearanceAction.MODIFY,
        order_id=order_id,
        order_terms=cleared,
    )
    with pytest.raises(WriteRefusedError) as refused:
        await order_rig.boundary.modify(
            stored.intent.intent_id, order_id=order_id, terms=moved_further, clearance=clearance
        )
    assert refused.value.code == "clearance_refused"
    assert order_rig.broker.modifies == []


async def test_a_modify_without_a_clearance_or_for_another_order_is_refused(
    order_rig: Any,
) -> None:
    stored, order_id = await _working_stop(order_rig)
    moved = terms(side=OrderSide.SELL, stop="97", limit="96")
    place_clearance = await order_rig.clear(stored.intent, effect=RiskEffect.REDUCES)
    other_order = await order_rig.clear(
        stored.intent,
        effect=RiskEffect.REDUCES,
        action=ClearanceAction.MODIFY,
        order_id="999",
        order_terms=moved,
    )
    for clearance, code in (
        (place_clearance, "clearance_mismatch"),
        (other_order, "clearance_refused"),
    ):
        with pytest.raises(WriteRefusedError) as refused:
            await order_rig.boundary.modify(
                stored.intent.intent_id, order_id=order_id, terms=moved, clearance=clearance
            )
        assert refused.value.code == code
    assert order_rig.broker.modifies == []


async def test_a_modify_cannot_change_the_instrument_or_side(order_rig: Any) -> None:
    stored, order_id = await _working_stop(order_rig)
    flipped = terms(side=OrderSide.BUY, limit="96")
    clearance = await order_rig.clear(
        stored.intent, action=ClearanceAction.MODIFY, order_id=order_id, order_terms=flipped
    )
    with pytest.raises(WriteRefusedError) as refused:
        await order_rig.boundary.modify(
            stored.intent.intent_id, order_id=order_id, terms=flipped, clearance=clearance
        )
    assert refused.value.code == "modify_changes_order"


async def test_a_modify_of_an_intent_with_no_order_is_refused(order_rig: Any) -> None:
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent, action=ClearanceAction.MODIFY, order_id="1")
    with pytest.raises(WriteRefusedError) as refused:
        await order_rig.boundary.modify(
            stored.intent.intent_id, order_id="1", terms=stored.intent.terms, clearance=clearance
        )
    assert refused.value.code == "intent_state"


async def test_a_declined_modify_alerts_and_keeps_the_terms(order_rig: Any) -> None:
    stored, order_id = await _working_stop(order_rig)
    moved = terms(side=OrderSide.SELL, stop="97", limit="96")
    order_rig.broker.questions = deque([(["o0"], "unlisted id")])
    result = await order_rig.boundary.modify(
        stored.intent.intent_id,
        order_id=order_id,
        terms=moved,
        clearance=await order_rig.clear(
            stored.intent,
            effect=RiskEffect.REDUCES,
            action=ClearanceAction.MODIFY,
            order_id=order_id,
            order_terms=moved,
        ),
    )
    assert result.status is PlacementStatus.DECLINED
    assert order_rig.alerts[-1].kind is OrderAlertKind.WARNING_DECLINED
    assert (await order_rig.book.get(stored.intent.intent_id)).intent.terms != moved


async def test_a_modify_refused_by_the_kill_switch_is_recorded(order_rig: Any) -> None:
    stored = await order_rig.intent()
    placed = await order_rig.boundary.place(
        stored.intent.intent_id, await order_rig.clear(stored.intent)
    )
    await order_rig.kill.turn_on(actor="test", reason="test")
    bigger = terms(quantity="20")
    with pytest.raises(WriteRefusedError) as refused:
        await order_rig.boundary.modify(
            stored.intent.intent_id,
            order_id=placed.order_id,
            terms=bigger,
            clearance=await order_rig.clear(
                stored.intent,
                action=ClearanceAction.MODIFY,
                order_id=placed.order_id,
                order_terms=bigger,
            ),
        )
    assert refused.value.code == "kill_switch_on"
    assert order_rig.broker.modifies == []


# ── I3/I4 at the boundary: the kill switch ──


async def test_with_the_kill_switch_on_a_reducing_sell_passes_and_a_buy_does_not(
    order_rig: Any,
) -> None:
    await order_rig.kill.turn_on(actor="test", reason="drill")
    buy = await order_rig.intent()
    assert await _refused(order_rig, buy, await order_rig.clear(buy.intent)) == "kill_switch_on"
    sell = await order_rig.intent(terms(side=OrderSide.SELL), role=IntentRole.EXIT, reason="target")
    result = await order_rig.boundary.place(
        sell.intent.intent_id, await order_rig.clear(sell.intent, effect=RiskEffect.REDUCES)
    )
    assert result.status is PlacementStatus.PLACED
    assert [p["side"] for p in order_rig.broker.placements] == ["SELL"]


async def test_with_the_kill_switch_on_a_sell_cleared_as_adding_risk_is_refused(
    order_rig: Any,
) -> None:
    await order_rig.kill.turn_on(actor="test", reason="drill")
    sell = await order_rig.intent(terms(side=OrderSide.SELL), role=IntentRole.EXIT, reason="target")
    code = await _refused(order_rig, sell, await order_rig.clear(sell.intent))
    assert code == "kill_switch_on"


async def test_a_protective_role_does_not_make_a_buy_reduce_risk(order_rig: Any) -> None:
    await order_rig.kill.turn_on(actor="test", reason="drill")
    buy = await order_rig.intent(role=IntentRole.PROTECTIVE, reason="stop")
    assert await _refused(order_rig, buy, await order_rig.clear(buy.intent)) == "kill_switch_on"
    assert adds_risk(await order_rig.clear(buy.intent))


async def test_the_kill_switch_is_checked_again_immediately_before_the_send(
    order_rig: Any,
) -> None:
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent)
    original = order_rig.client.find

    async def find_then_kill(coid: str) -> Any:
        lookup = await original(coid)
        await order_rig.kill.turn_on(actor="test", reason="switched during the lookup")
        return lookup

    order_rig.client.find = find_then_kill
    assert await _refused(order_rig, stored, clearance) == "kill_switch_on"
    assert order_rig.broker.placements == []


# ── modes and the paper guard ──


async def test_shadow_mode_places_nothing(order_rig: Any) -> None:
    await order_rig.modes.downgrade(ExecutionMode.SHADOW, actor="test", reason="test")
    stored = await order_rig.intent()
    assert await _refused(order_rig, stored, await order_rig.clear(stored.intent)) == (
        "mode_forbids_write"
    )
    assert order_rig.broker.placements == []


async def test_a_downgrade_mid_cycle_stops_the_next_write(order_rig: Any) -> None:
    first = await order_rig.intent()
    second = await order_rig.intent()
    await order_rig.boundary.place(first.intent.intent_id, await order_rig.clear(first.intent))
    clearance = await order_rig.clear(second.intent)
    # Another process downgrades between the two writes of one cycle.
    await order_rig.modes.downgrade(ExecutionMode.SHADOW, actor="system", reason="breaker")
    assert await _refused(order_rig, second, clearance) == "mode_forbids_write"
    assert len(order_rig.broker.placements) == 1


async def test_a_downgrade_during_the_lookup_stops_the_send(order_rig: Any) -> None:
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent)
    original = order_rig.client.find

    async def find_then_downgrade(coid: str) -> Any:
        lookup = await original(coid)
        await order_rig.modes.downgrade(ExecutionMode.SHADOW, actor="system", reason="breaker")
        return lookup

    order_rig.client.find = find_then_downgrade
    assert await _refused(order_rig, stored, clearance) == "mode_forbids_write"
    assert order_rig.broker.placements == []


async def test_a_cancel_after_a_downgrade_to_shadow_still_reaches_the_broker(
    order_rig: Any,
) -> None:
    order = order_rig.broker.add_order(side="BUY")
    await order_rig.modes.downgrade(ExecutionMode.SHADOW, actor="system", reason="breaker")
    await order_rig.kill.turn_on(actor="test", reason="drill")
    answer = await order_rig.boundary.cancel(order.order_id)
    assert answer.error is None
    assert order_rig.broker.cancels == [order.order_id]


async def test_a_mode_that_cannot_be_read_refuses(order_rig: Any) -> None:
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent)

    async def broken() -> ExecutionMode:
        raise StoreError("the store is gone")

    order_rig.modes.current = broken
    assert await _refused(order_rig, stored, clearance) == "mode_unreadable"


async def test_a_live_account_under_the_paper_profile_is_refused(order_rig: Any) -> None:
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent)
    object.__setattr__(order_rig.guard, "account_id", "U_LIVE_TEST")
    assert await _refused(order_rig, stored, clearance) == "paper_guard"
    with pytest.raises(WriteRefusedError) as refused:
        await order_rig.boundary.cancel("1")
    assert refused.value.code == "paper_guard"
    assert order_rig.broker.placements == []
    assert order_rig.broker.cancels == []


async def test_paper_mode_under_a_live_profile_is_refused(order_rig: Any) -> None:
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent)
    object.__setattr__(order_rig.guard, "profile", "live")
    order_rig.modes._profile = "live"
    assert await _refused(order_rig, stored, clearance) == "mode_forbids_write"


async def test_a_profile_mismatch_between_the_guard_and_the_modes_refuses(order_rig: Any) -> None:
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent)
    object.__setattr__(order_rig.guard, "profile", "live")
    assert await _refused(order_rig, stored, clearance) == "profile_mismatch"


# ── outcomes ──


async def test_a_rejection_rejects_the_intent(order_rig: Any) -> None:
    order_rig.broker.reject_next = "Invalid order price fields"
    stored = await order_rig.intent()
    result = await order_rig.boundary.place(
        stored.intent.intent_id, await order_rig.clear(stored.intent)
    )
    assert result.status is PlacementStatus.REJECTED
    assert await _state(order_rig, stored) is IntentState.REJECTED


async def test_an_unknown_warning_is_declined_cancelled_and_alerted(order_rig: Any) -> None:
    order_rig.broker.questions = deque([(["o382"], "tick size")])
    stored = await order_rig.intent()
    result = await order_rig.boundary.place(
        stored.intent.intent_id, await order_rig.clear(stored.intent)
    )
    assert result.status is PlacementStatus.DECLINED
    assert order_rig.broker.replies == [(order_rig.broker.replies[0][0], False)]
    assert await _state(order_rig, stored) is IntentState.REJECTED
    assert order_rig.alerts[-1].kind is OrderAlertKind.WARNING_DECLINED
    history = await order_rig.book.history(stored.intent.intent_id)
    assert any("o382" in event.detail and "declined" in event.detail for event in history)


async def test_an_unknown_outcome_stays_submitted_and_alerts(order_rig: Any) -> None:
    order_rig.broker.lose_next_answer = True
    stored = await order_rig.intent()
    result = await order_rig.boundary.place(
        stored.intent.intent_id, await order_rig.clear(stored.intent)
    )
    assert result.status is PlacementStatus.UNKNOWN
    assert await _state(order_rig, stored) is IntentState.SUBMITTED
    assert order_rig.alerts[-1].kind is OrderAlertKind.OUTCOME_UNKNOWN
    # The retry finds it: working, and still one placement.
    again = await order_rig.boundary.place(
        stored.intent.intent_id, await order_rig.clear(stored.intent)
    )
    assert again.status is PlacementStatus.ALREADY_PLACED
    assert await _state(order_rig, stored) is IntentState.WORKING
    assert len(order_rig.broker.placements) == 1


async def test_a_retry_refused_before_sending_keeps_the_intent_submitted(order_rig: Any) -> None:
    order_rig.broker.lose_next_answer = True
    stored = await order_rig.intent()
    await order_rig.boundary.place(stored.intent.intent_id, await order_rig.clear(stored.intent))
    order_rig.broker.orders.clear()  # not listed (yet)
    stale = await order_rig.clear(stored.intent, ttl_seconds=1)
    order_rig.clock.advance(2)
    assert await _refused(order_rig, stored, stale) == "clearance_refused"
    assert await _state(order_rig, stored) is IntentState.SUBMITTED


async def test_a_lookup_failure_before_sending_is_recorded(order_rig: Any) -> None:
    order_rig.broker.snapshot = False
    stored = await order_rig.intent()
    with pytest.raises(Exception, match="snapshot"):
        await order_rig.boundary.place(
            stored.intent.intent_id, await order_rig.clear(stored.intent)
        )
    history = await order_rig.book.history(stored.intent.intent_id)
    assert "not sent" in history[-1].detail
    assert order_rig.broker.placements == []


async def test_a_reused_coid_alerts_and_stays_submitted(order_rig: Any) -> None:
    stored = await order_rig.intent()
    order_rig.broker._used_coids.add(stored.coid)
    result = await order_rig.boundary.place(
        stored.intent.intent_id, await order_rig.clear(stored.intent)
    )
    assert result.status is PlacementStatus.DUPLICATE
    assert await _state(order_rig, stored) is IntentState.SUBMITTED
    assert order_rig.alerts[-1].kind is OrderAlertKind.DUPLICATE_COID


async def test_an_order_found_filled_among_trades_fills_the_intent(order_rig: Any) -> None:
    stored = await order_rig.intent()
    order = order_rig.broker.add_order(side="BUY", coid=stored.coid)
    order_rig.broker.fill(order.order_id)
    del order_rig.broker.orders[order.order_id]
    await order_rig.boundary.place(stored.intent.intent_id, await order_rig.clear(stored.intent))
    assert await _state(order_rig, stored) is IntentState.FILLED
    fills = await order_rig.store(repo.fills_for_intent, stored.intent.intent_id)
    assert [f.quantity for f in fills] == [Decimal(10)]


async def test_a_part_fill_found_among_trades_part_fills_the_intent(order_rig: Any) -> None:
    stored = await order_rig.intent()
    order = order_rig.broker.add_order(side="BUY", coid=stored.coid)
    order_rig.broker.fill(order.order_id, quantity="4")
    del order_rig.broker.orders[order.order_id]
    await order_rig.boundary.place(stored.intent.intent_id, await order_rig.clear(stored.intent))
    assert await _state(order_rig, stored) is IntentState.PARTIALLY_FILLED


async def test_a_confirmed_no_market_data_warning_is_recorded(order_rig: Any) -> None:
    order_rig.broker.questions = deque([(["o354"], "no market data")])
    stored = await order_rig.intent()
    delayed = quote(status=MarketDataStatus.DELAYED, delay=1200, at=order_rig.clock())
    result = await order_rig.boundary.place(
        stored.intent.intent_id, await order_rig.clear(stored.intent, evidence=delayed)
    )
    assert result.status is PlacementStatus.PLACED
    history = await order_rig.book.history(stored.intent.intent_id)
    assert any("o354" in e.detail and "confirmed" in e.detail for e in history)


# ── cancel ──


async def test_a_cancel_that_loses_the_race_to_a_fill_records_the_fill(order_rig: Any) -> None:
    stored = await order_rig.intent()
    placed = await order_rig.boundary.place(
        stored.intent.intent_id, await order_rig.clear(stored.intent)
    )
    order_rig.broker.fill_before_cancel = True
    answer = await order_rig.boundary.cancel(placed.order_id)
    assert answer.error is not None
    assert await _state(order_rig, stored) is IntentState.FILLED


async def test_a_cancel_cancels_the_intent(order_rig: Any) -> None:
    stored = await order_rig.intent()
    placed = await order_rig.boundary.place(
        stored.intent.intent_id, await order_rig.clear(stored.intent)
    )
    await order_rig.boundary.cancel(placed.order_id)
    assert await _state(order_rig, stored) is IntentState.CANCELLED


async def test_a_cancel_whose_order_cannot_be_read_back_still_answers(order_rig: Any) -> None:
    order = order_rig.broker.add_order(side="BUY")
    order_rig.broker.status_unavailable = True
    answer = await order_rig.boundary.cancel(order.order_id)
    assert answer.error is None
    assert order_rig.broker.cancels == [order.order_id]


async def test_a_repeated_report_changes_nothing(order_rig: Any) -> None:
    stored = await order_rig.intent()
    placed = await order_rig.boundary.place(
        stored.intent.intent_id, await order_rig.clear(stored.intent)
    )
    before = await order_rig.book.history(stored.intent.intent_id)
    await order_rig.boundary.sync_order(placed.order_id)
    assert await order_rig.book.history(stored.intent.intent_id) == before


async def test_an_alert_hook_that_fails_does_not_stop_the_order_path(order_rig: Any) -> None:
    def broken(alert: Any) -> None:
        raise RuntimeError("telegram is down")

    order_rig.boundary._alert = broken
    order_rig.broker.lose_next_answer = True
    stored = await order_rig.intent()
    result = await order_rig.boundary.place(
        stored.intent.intent_id, await order_rig.clear(stored.intent)
    )
    assert result.status is PlacementStatus.UNKNOWN
    order_rig.boundary._alert = None
    order_rig.broker.lose_next_answer = True
    other = await order_rig.intent()
    result = await order_rig.boundary.place(
        other.intent.intent_id, await order_rig.clear(other.intent)
    )
    assert result.status is PlacementStatus.UNKNOWN


async def test_a_modify_with_another_intents_clearance_is_refused(order_rig: Any) -> None:
    stored, order_id = await _working_stop(order_rig)
    other = await order_rig.intent()
    clearance = await order_rig.clear(
        other.intent, action=ClearanceAction.MODIFY, order_id=order_id
    )
    with pytest.raises(WriteRefusedError) as refused:
        await order_rig.boundary.modify(
            stored.intent.intent_id,
            order_id=order_id,
            terms=other.intent.terms,
            clearance=clearance,
        )
    assert refused.value.code == "clearance_mismatch"


async def test_a_stale_report_does_not_move_a_finished_intent_back(order_rig: Any) -> None:
    stored = await order_rig.intent()
    placed = await order_rig.boundary.place(
        stored.intent.intent_id, await order_rig.clear(stored.intent)
    )
    order_rig.broker.fill(placed.order_id)
    await order_rig.boundary.sync_order(placed.order_id)
    assert await _state(order_rig, stored) is IntentState.FILLED
    order_rig.broker.orders[placed.order_id].status = "Submitted"  # a stale read
    await order_rig.boundary.sync_order(placed.order_id)
    assert await _state(order_rig, stored) is IntentState.FILLED


def test_broker_statuses_map_to_intent_states() -> None:
    from datetime import UTC, datetime

    from obliskiq.domain.orders import BrokerOrder

    def order(status: str, filled: str | None = None) -> BrokerOrder:
        return BrokerOrder(
            order_id="1",
            coid=None,
            conid=1,
            side=OrderSide.BUY,
            sec_type=None,
            order_type=None,
            status=status,
            total_quantity=None,
            filled_quantity=None if filled is None else Decimal(filled),
            remaining_quantity=None,
            average_price=None,
            tif=None,
            observed_at=datetime(2026, 10, 13, tzinfo=UTC),
        )

    assert state_for(order("Filled")) is IntentState.FILLED
    assert state_for(order("Cancelled", "3")) is IntentState.CANCELLED
    assert state_for(order("Submitted", "3")) is IntentState.PARTIALLY_FILLED
    for status in ("Submitted", "PreSubmitted", "PendingSubmit", "Inactive", "WarnState", "?"):
        assert state_for(order(status)) is IntentState.WORKING


# ── defence in depth at the boundary ──


async def test_an_expired_intent_is_never_sent(order_rig: Any) -> None:
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent, ttl_seconds=7 * 3600)
    order_rig.clock.advance(6 * 3600)  # the rig's intents expire after six hours
    assert await _refused(order_rig, stored, clearance) == "intent_expired"
    assert await _state(order_rig, stored) is IntentState.EXPIRED
    assert order_rig.broker.placements == []


async def test_an_expired_submitted_intent_is_not_retried(order_rig: Any) -> None:
    order_rig.broker.lose_next_answer = True
    stored = await order_rig.intent()
    await order_rig.boundary.place(stored.intent.intent_id, await order_rig.clear(stored.intent))
    order_rig.clock.advance(6 * 3600)
    late = await order_rig.clear(stored.intent)
    assert await _refused(order_rig, stored, late) == "intent_expired"
    assert await _state(order_rig, stored) is IntentState.SUBMITTED  # it may exist: look it up
    assert len(order_rig.broker.placements) == 1


async def test_an_intent_made_for_another_mode_is_refused(order_rig: Any) -> None:
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent)
    shadow_intent = stored.intent.model_copy(update={"mode": ExecutionMode.SHADOW})
    original = order_rig.book.get

    async def get(intent_id: Any) -> Any:
        found = await original(intent_id)
        return type(found)(
            intent=shadow_intent.model_copy(update={"state": found.intent.state}), coid=found.coid
        )

    order_rig.book.get = get
    assert await _refused(order_rig, stored, clearance) == "intent_mode"
    assert order_rig.broker.placements == []


async def test_a_modify_must_name_an_order_of_its_intent(order_rig: Any) -> None:
    stored, order_id = await _working_stop(order_rig)
    _, other_order = await _working_stop(order_rig)
    moved = terms(side=OrderSide.SELL, stop="97", limit="96")
    clearance = await order_rig.clear(
        stored.intent,
        effect=RiskEffect.REDUCES,
        action=ClearanceAction.MODIFY,
        order_id=other_order,
        order_terms=moved,
    )
    with pytest.raises(WriteRefusedError) as refused:
        await order_rig.boundary.modify(
            stored.intent.intent_id, order_id=other_order, terms=moved, clearance=clearance
        )
    assert refused.value.code == "order_not_intents"
    assert order_rig.broker.modifies == []
    assert order_id != other_order


async def test_a_modify_of_an_expired_intent_is_refused(order_rig: Any) -> None:
    stored, order_id = await _working_stop(order_rig)
    order_rig.clock.advance(6 * 3600)
    moved = terms(side=OrderSide.SELL, stop="97", limit="96")
    clearance = await order_rig.clear(
        stored.intent,
        effect=RiskEffect.REDUCES,
        action=ClearanceAction.MODIFY,
        order_id=order_id,
        order_terms=moved,
    )
    with pytest.raises(WriteRefusedError) as refused:
        await order_rig.boundary.modify(
            stored.intent.intent_id, order_id=order_id, terms=moved, clearance=clearance
        )
    assert refused.value.code == "intent_expired"


def _expire_at_the_first_warning(rig: Any) -> None:
    """Let the intent expire when the first warning arrives, then let the real policy decide."""
    original = rig.replies.decider

    def decider(clearance: Any, now: Any) -> Any:
        decide = original(clearance, now)

        def expire_then_decide(question: Any) -> Any:
            rig.clock.advance(6 * 3600)
            return decide(question)

        return expire_then_decide

    rig.replies.decider = decider


async def test_an_intent_that_expires_during_the_lookup_is_not_sent(order_rig: Any) -> None:
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent)
    find = order_rig.client.find

    async def find_then_expire(*args: Any, **kwargs: Any) -> Any:
        found = await find(*args, **kwargs)
        order_rig.clock.advance(6 * 3600)
        return found

    order_rig.client.find = find_then_expire
    with pytest.raises(WriteRefusedError) as refused:
        await order_rig.boundary.place(stored.intent.intent_id, clearance)
    assert refused.value.code == "intent_expired"
    assert order_rig.broker.live() == []
    assert await _state(order_rig, stored) is IntentState.EXPIRED


async def test_a_placements_warning_is_not_confirmed_once_the_intent_has_expired(
    order_rig: Any,
) -> None:
    stored = await order_rig.intent()
    clearance = await order_rig.clear(stored.intent)
    order_rig.broker.questions = deque([(["o10153"], "price capped")])
    _expire_at_the_first_warning(order_rig)
    result = await order_rig.boundary.place(stored.intent.intent_id, clearance)
    assert result.status is PlacementStatus.DECLINED
    assert "intent_expired" in result.detail
    assert not any(confirmed for _, confirmed in order_rig.broker.replies)
    assert order_rig.broker.live() == []


async def test_a_modifys_warning_is_not_confirmed_once_the_intent_has_expired(
    order_rig: Any,
) -> None:
    stored, order_id = await _working_stop(order_rig)
    moved = terms(side=OrderSide.SELL, stop="97", limit="96")
    clearance = await order_rig.clear(
        stored.intent,
        effect=RiskEffect.REDUCES,
        action=ClearanceAction.MODIFY,
        order_id=order_id,
        order_terms=moved,
    )
    order_rig.broker.questions = deque([(["o10331"], "stop order risks")])
    _expire_at_the_first_warning(order_rig)
    result = await order_rig.boundary.modify(
        stored.intent.intent_id, order_id=order_id, terms=moved, clearance=clearance
    )
    assert result.status is PlacementStatus.DECLINED
    assert "intent_expired" in result.detail
    assert not any(confirmed for _, confirmed in order_rig.broker.replies)
    assert (await order_rig.book.get(stored.intent.intent_id)).intent.terms != moved
    assert order_id in [order.order_id for order in order_rig.broker.live()]  # left as it was


async def test_a_cancel_needs_only_the_paper_guard(order_rig: Any) -> None:
    order = order_rig.broker.add_order(side="BUY")
    order_rig.modes._profile = "live"  # a mismatch refuses writes, never a cancel
    answer = await order_rig.boundary.cancel(order.order_id)
    assert answer.error is None


async def test_a_declined_placement_whose_order_may_remain_stays_submitted(
    order_rig: Any,
) -> None:
    order_rig.broker.questions = deque([(["o999"], "unknown")])
    order_rig.broker.keep_declined = True
    order_rig.broker.cancel_failure = "503"
    stored = await order_rig.intent()
    result = await order_rig.boundary.place(
        stored.intent.intent_id, await order_rig.clear(stored.intent)
    )
    assert result.status is PlacementStatus.DECLINED
    assert result.cleanup_verified is False
    assert await _state(order_rig, stored) is IntentState.SUBMITTED
    assert order_rig.alerts[-1].kind is OrderAlertKind.WARNING_DECLINED
