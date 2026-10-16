"""No exit leaves a position unprotected (I5), on the fake broker.

An exit that needs its protective stop cut passes its other checks first; the stop is restored
when the exit is refused, cancelled or expires; a failed restore alerts and is tried again.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections import deque
from decimal import Decimal
from typing import Any

import pytest
from order_rig import STOCK

from obliskiq.domain.orders import (
    ExecutionMode,
    IntentRole,
    IntentState,
    OrderSide,
    PretradeClearance,
    StopResizeState,
)
from obliskiq.execution.boundary import OrderAlertKind
from obliskiq.execution.pretrade import PretradeFailure, PretradeRefusal, Reason
from obliskiq.ibkr.orders import PlacementStatus
from obliskiq.store import orders as repo


@pytest.fixture
async def rig(order_rig: Any) -> Any:
    """Ten shares held, protected by a resting stop-limit for all ten."""
    order_rig.ready()
    await order_rig.hold(STOCK, "10")
    stop = await order_rig.staged(
        side=OrderSide.SELL, stop="95", limit="94.5", role=IntentRole.PROTECTIVE
    )
    clearance = await order_rig.gate.clear(stop.intent)
    assert isinstance(clearance, PretradeClearance)
    placed = await order_rig.boundary.place(stop.intent.intent_id, clearance)
    assert placed.status is PlacementStatus.PLACED
    order_rig.stop_intent = stop.intent.intent_id
    order_rig.stop_order = placed.order_id
    return order_rig


def stop_size(rig: Any) -> Decimal:
    return rig.broker.orders[rig.stop_order].quantity


async def exit_of(rig: Any, quantity: str, limit: str = "99.6") -> Any:
    return await rig.staged(side=OrderSide.SELL, quantity=quantity, limit=limit)


async def resizes(rig: Any, exit_intent: Any) -> list[Any]:
    return await rig.store(repo.stop_resizes_for_exit, exit_intent.intent.intent_id)


async def test_a_partial_exit_cuts_the_stop_and_both_rest_within_the_position(rig: Any) -> None:
    exit_intent = await exit_of(rig, "4")
    result = await rig.coordinator.place_exit(exit_intent.intent.intent_id)
    assert result.placed
    assert result.refusal is None
    assert stop_size(rig) == Decimal(6)
    (cut,) = result.resized
    assert cut.state is StopResizeState.RESIZED
    assert cut.resized_quantity == Decimal(6)
    assert cut.original_terms.quantity == Decimal(10)
    live = rig.broker.live()
    assert sum(o.quantity - o.filled for o in live if o.side == "SELL") == Decimal(10)


async def test_an_exit_without_a_stop_is_placed_as_it_is(order_rig: Any) -> None:
    order_rig.ready()
    await order_rig.hold(STOCK, "10")
    exit_intent = await exit_of(order_rig, "10")
    result = await order_rig.coordinator.place_exit(exit_intent.intent.intent_id)
    assert result.placed
    assert result.resized == ()


async def test_the_exits_other_checks_run_before_the_stop_is_touched(rig: Any) -> None:
    breach = await exit_of(rig, "4", limit="90")  # below the collar
    result = await rig.coordinator.place_exit(breach.intent.intent_id)
    assert not result.placed
    assert result.refusal is not None
    assert result.refusal.codes == ("collar",)
    assert rig.broker.modifies == []
    assert rig.broker.cancels == []
    assert stop_size(rig) == Decimal(10)
    assert await resizes(rig, breach) == []


async def test_an_exit_the_broker_refuses_after_the_cut_restores_the_stop(rig: Any) -> None:
    exit_intent = await exit_of(rig, "4")
    rig.broker.reject_next = "We cannot accept an order at the limit price you selected"
    result = await rig.coordinator.place_exit(exit_intent.intent.intent_id)
    assert not result.placed
    assert result.placement is not None
    assert result.placement.status is PlacementStatus.REJECTED
    (restored,) = result.restored
    assert restored.state is StopResizeState.RESTORED
    assert stop_size(rig) == Decimal(10)
    assert all(alert.kind is not OrderAlertKind.RESTORE_FAILED for alert in rig.alerts)


async def test_an_exit_the_gate_refuses_after_the_cut_restores_the_stop(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    exit_intent = await exit_of(rig, "4")
    clear = rig.gate.clear

    async def refuse_the_exit(intent: Any, **kwargs: Any) -> Any:
        if intent.intent_id == exit_intent.intent.intent_id:
            failure = PretradeFailure(Reason.QUOTE_POLICY, "the quote went stale meanwhile")
            return PretradeRefusal(intent.intent_id, (failure,))
        return await clear(intent, **kwargs)

    monkeypatch.setattr(rig.gate, "clear", refuse_the_exit)
    result = await rig.coordinator.place_exit(exit_intent.intent.intent_id)
    assert result.refusal is not None
    assert result.refusal.codes == ("quote_policy",)
    assert [r.state for r in result.restored] == [StopResizeState.RESTORED]
    assert stop_size(rig) == Decimal(10)
    assert (await rig.book.get(exit_intent.intent.intent_id)).intent.state is (
        IntentState.CANCELLED
    )


async def test_an_exit_cancelled_later_restores_the_stop_less_what_it_sold(rig: Any) -> None:
    exit_intent = await exit_of(rig, "4")
    result = await rig.coordinator.place_exit(exit_intent.intent.intent_id)
    assert result.placement is not None
    assert result.placement.order_id is not None
    exit_order = result.placement.order_id
    rig.clock.advance(1)
    rig.broker.fill(exit_order, "1")
    assert await rig.coordinator.settle() == []  # still working: nothing to restore yet
    rig.clock.advance(1)  # a read in the fill's own second counts the fill against it
    await rig.boundary.cancel(exit_order)
    (restored,) = await rig.coordinator.settle()
    assert restored.state is StopResizeState.RESTORED
    assert "restored to 9" in restored.detail
    assert stop_size(rig) == Decimal(9)
    assert await rig.coordinator.settle() == []  # settled once


async def test_an_exit_that_expires_restores_the_stop(rig: Any) -> None:
    exit_intent = await exit_of(rig, "4")
    result = await rig.coordinator.place_exit(exit_intent.intent.intent_id)
    assert result.placement is not None
    assert result.placement.order_id is not None
    rig.broker.orders[result.placement.order_id].status = "Cancelled"  # the day ended
    await rig.book.advance(exit_intent.intent.intent_id, IntentState.EXPIRED, "the day ended")
    (restored,) = await rig.coordinator.settle(exit_intent.intent.intent_id)
    assert restored.state is StopResizeState.RESTORED
    assert stop_size(rig) == Decimal(10)


async def test_an_exit_that_fills_closes_the_resize_and_keeps_the_stop_cut(rig: Any) -> None:
    exit_intent = await exit_of(rig, "4")
    result = await rig.coordinator.place_exit(exit_intent.intent.intent_id)
    assert result.placement is not None
    assert result.placement.order_id is not None
    rig.broker.fill(result.placement.order_id)
    (closed,) = await rig.coordinator.settle()
    assert closed.state is StopResizeState.CLOSED
    assert stop_size(rig) == Decimal(6)


async def test_a_full_exit_cancels_the_stop_and_places_it_again_if_the_exit_ends(
    rig: Any,
) -> None:
    exit_intent = await exit_of(rig, "10")
    result = await rig.coordinator.place_exit(exit_intent.intent.intent_id)
    assert result.placed
    assert rig.broker.orders[rig.stop_order].status == "Cancelled"
    (cut,) = result.resized
    assert cut.resized_quantity == Decimal(0)
    assert result.placement is not None
    assert result.placement.order_id is not None
    await rig.boundary.cancel(result.placement.order_id)
    (restored,) = await rig.coordinator.settle()
    assert restored.state is StopResizeState.RESTORED
    replacement = [
        o for o in rig.broker.live() if o.side == "SELL" and o.order_type == "STOP_LIMIT"
    ]
    assert [o.quantity for o in replacement] == [Decimal(10)]
    stored = await rig.book.by_coid(replacement[0].coid)
    assert stored is not None
    assert stored.intent.role is IntentRole.PROTECTIVE
    assert stored.intent.terms.price == cut.original_terms.price


async def test_a_failed_restore_alerts_and_is_tried_again(rig: Any) -> None:
    exit_intent = await exit_of(rig, "4")
    result = await rig.coordinator.place_exit(exit_intent.intent.intent_id)
    assert result.placement is not None
    assert result.placement.order_id is not None
    await rig.modes.downgrade(ExecutionMode.SHADOW, actor="test", reason="a breaker fired")
    await rig.boundary.cancel(result.placement.order_id)  # cancels work in every mode
    (failed,) = await rig.coordinator.settle()
    assert failed.state is StopResizeState.RESTORE_FAILED
    (alert,) = [a for a in rig.alerts if a.kind is OrderAlertKind.RESTORE_FAILED]
    assert alert.intent_id == rig.stop_intent
    assert "mode_forbids_write" in alert.detail
    assert stop_size(rig) == Decimal(6)
    await rig.to_paper()
    (restored,) = await rig.coordinator.settle()
    assert restored.state is StopResizeState.RESTORED
    assert stop_size(rig) == Decimal(10)


async def test_a_failed_restore_without_an_alert_hook_still_fails_loudly(rig: Any) -> None:
    rig.coordinator._alert = None
    exit_intent = await exit_of(rig, "4")
    result = await rig.coordinator.place_exit(exit_intent.intent.intent_id)
    assert result.placement is not None
    assert result.placement.order_id is not None
    await rig.modes.downgrade(ExecutionMode.SHADOW, actor="test", reason="a breaker fired")
    await rig.boundary.cancel(result.placement.order_id)
    (failed,) = await rig.coordinator.settle()
    assert failed.state is StopResizeState.RESTORE_FAILED


async def test_a_broken_alert_hook_does_not_stop_the_settle(rig: Any) -> None:
    def broken(alert: Any) -> None:
        raise RuntimeError("telegram down")

    rig.coordinator._alert = broken
    exit_intent = await exit_of(rig, "4")
    result = await rig.coordinator.place_exit(exit_intent.intent.intent_id)
    assert result.placement is not None
    assert result.placement.order_id is not None
    await rig.modes.downgrade(ExecutionMode.SHADOW, actor="test", reason="a breaker fired")
    await rig.boundary.cancel(result.placement.order_id)
    (failed,) = await rig.coordinator.settle()
    assert failed.state is StopResizeState.RESTORE_FAILED


async def test_a_cut_the_broker_declines_ends_the_exit_unsent(rig: Any) -> None:
    exit_intent = await exit_of(rig, "4")
    rig.broker.questions = deque([(["o999"], "an unknown warning")])
    result = await rig.coordinator.place_exit(exit_intent.intent.intent_id)
    assert not result.placed
    assert result.resized == ()
    assert "could not be cut" in result.detail
    assert stop_size(rig) == Decimal(10)  # a declined modify leaves the order as it was
    assert rig.broker.placements[-1]["side"] == "SELL"  # only the stop was ever placed
    assert (await rig.book.get(exit_intent.intent.intent_id)).intent.state is (IntentState.STAGED)


async def test_a_stop_that_cannot_be_cancelled_ends_the_exit_unsent(rig: Any) -> None:
    exit_intent = await exit_of(rig, "10")
    rig.broker.cancel_failure = "the order is locked"
    result = await rig.coordinator.place_exit(exit_intent.intent.intent_id)
    assert not result.placed
    assert "could not be cancelled" in result.detail
    assert rig.broker.orders[rig.stop_order].status == "Submitted"


async def test_a_restore_left_with_nothing_to_protect_closes(rig: Any) -> None:
    exit_intent = await exit_of(rig, "4")
    result = await rig.coordinator.place_exit(exit_intent.intent.intent_id)
    assert result.placement is not None
    assert result.placement.order_id is not None
    order = rig.broker.orders[result.placement.order_id]
    order.filled = Decimal(10)  # it sold the stop's whole size (an edge: reported, not real)
    order.status = "Cancelled"
    await rig.boundary.sync_order(order.order_id)
    (closed,) = await rig.coordinator.settle()
    assert closed.state is StopResizeState.CLOSED
    assert "nothing left" in closed.detail


async def test_an_exit_is_a_sell(rig: Any) -> None:
    from obliskiq.errors import ExecutionError

    buy = await rig.staged()
    with pytest.raises(ExecutionError, match="a sell"):
        await rig.coordinator.place_exit(buy.intent.intent_id)


async def test_a_replacement_stop_is_not_made_for_an_expired_stop_intent(rig: Any) -> None:
    exit_intent = await exit_of(rig, "10")
    result = await rig.coordinator.place_exit(exit_intent.intent.intent_id)
    assert result.placement is not None
    assert result.placement.order_id is not None
    rig.clock.advance(7 * 3600)  # past the old stop intent's expiry
    await rig.boundary.cancel(result.placement.order_id)
    (failed,) = await rig.coordinator.settle()
    assert failed.state is StopResizeState.RESTORE_FAILED
    assert "expired" in failed.detail


# ── the edges of cutting and restoring ──


async def second_stop(rig: Any, quantity: str) -> str:
    stop = await rig.staged(
        side=OrderSide.SELL, quantity=quantity, stop="95", limit="94.5", role=IntentRole.PROTECTIVE
    )
    clearance = await rig.gate.clear(stop.intent)
    assert isinstance(clearance, PretradeClearance)
    placed = await rig.boundary.place(stop.intent.intent_id, clearance)
    assert placed.order_id is not None
    return placed.order_id


async def test_an_exit_cuts_the_largest_stop_first_and_no_more_than_it_needs(
    order_rig: Any,
) -> None:
    order_rig.ready()
    await order_rig.hold(STOCK, "10")
    large = await second_stop(order_rig, "6")
    small = await second_stop(order_rig, "4")
    buy = await order_rig.staged()  # a working buy of the same instrument is no stop
    clearance = await order_rig.gate.clear(buy.intent)
    assert isinstance(clearance, PretradeClearance)
    await order_rig.boundary.place(buy.intent.intent_id, clearance)
    exit_intent = await exit_of(order_rig, "3")
    result = await order_rig.coordinator.place_exit(exit_intent.intent.intent_id)
    assert result.placed
    assert order_rig.broker.orders[large].quantity == Decimal(3)
    assert order_rig.broker.orders[small].quantity == Decimal(4)
    assert [cut.stop_order_id for cut in result.resized] == [large]


async def test_a_stop_the_store_still_lists_but_the_broker_finished_is_not_cut(rig: Any) -> None:
    rig.broker.orders[rig.stop_order].status = "Cancelled"  # cancelled at the broker, unread
    exit_intent = await exit_of(rig, "4")
    result = await rig.coordinator.place_exit(exit_intent.intent.intent_id)
    assert result.placed
    assert result.resized == ()


async def test_a_downgrade_between_the_cut_and_the_send_refuses_the_exit_and_alerts(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    exit_intent = await exit_of(rig, "4")
    clear = rig.gate.clear

    async def then_downgrade(intent: Any, **kwargs: Any) -> Any:
        answer = await clear(intent, **kwargs)
        if intent.intent_id == exit_intent.intent.intent_id:
            await rig.modes.downgrade(ExecutionMode.SHADOW, actor="breaker", reason="drawdown")
        return answer

    monkeypatch.setattr(rig.gate, "clear", then_downgrade)
    result = await rig.coordinator.place_exit(exit_intent.intent.intent_id)
    assert not result.placed
    assert result.detail == "refused before sending"
    assert [r.state for r in result.restored] == [StopResizeState.RESTORE_FAILED]
    assert [a.kind for a in rig.alerts] == [OrderAlertKind.RESTORE_FAILED]
    assert stop_size(rig) == Decimal(6)  # cut; the restore waits for a mode that can write
    monkeypatch.undo()
    await rig.to_paper()
    (restored,) = await rig.coordinator.settle()
    assert restored.state is StopResizeState.RESTORED
    assert stop_size(rig) == Decimal(10)


async def test_a_modify_the_boundary_refuses_is_a_failed_cut(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obliskiq.errors import WriteRefusedError

    async def refuse(*args: Any, **kwargs: Any) -> Any:
        raise WriteRefusedError("mode_forbids_write", "shadow mode sends no order")

    monkeypatch.setattr(rig.boundary, "modify", refuse)
    result = await rig.coordinator.place_exit((await exit_of(rig, "4")).intent.intent_id)
    assert not result.placed
    assert "mode_forbids_write" in result.detail


async def full_exit_then_cancel(rig: Any) -> Any:
    exit_intent = await exit_of(rig, "10")
    result = await rig.coordinator.place_exit(exit_intent.intent.intent_id)
    assert result.placement is not None
    assert result.placement.order_id is not None
    await rig.boundary.cancel(result.placement.order_id)
    return exit_intent


async def test_a_replacement_stop_the_gate_refuses_is_a_failed_restore(rig: Any) -> None:
    await full_exit_then_cancel(rig)
    rig.broker.quote(STOCK, availability="ZP")  # frozen: no quote policy accepts it
    (failed,) = await rig.coordinator.settle()
    assert failed.state is StopResizeState.RESTORE_FAILED
    assert "quote_policy" in failed.detail
    rejected = [
        s
        for s in await rig.store(repo.intents_in_states, [IntentState.REJECTED])
        if s.intent.role is IntentRole.PROTECTIVE
    ]
    assert len(rejected) == 1  # the refused replacement does not linger


async def test_a_replacement_stop_the_broker_refuses_is_a_failed_restore(rig: Any) -> None:
    await full_exit_then_cancel(rig)
    rig.broker.reject_next = "the stop price is invalid"
    (failed,) = await rig.coordinator.settle()
    assert failed.state is StopResizeState.RESTORE_FAILED
    assert "was rejected" in failed.detail


async def test_a_stop_cut_and_then_finished_at_the_broker_is_placed_again(rig: Any) -> None:
    exit_intent = await exit_of(rig, "4")
    result = await rig.coordinator.place_exit(exit_intent.intent.intent_id)
    assert result.placement is not None
    assert result.placement.order_id is not None
    rig.broker.orders[rig.stop_order].status = "Cancelled"  # the stop went away meanwhile
    await rig.boundary.cancel(result.placement.order_id)
    (restored,) = await rig.coordinator.settle()
    assert restored.state is StopResizeState.RESTORED
    live_stops = [o for o in rig.broker.live() if o.order_type == "STOP_LIMIT"]
    assert [o.quantity for o in live_stops] == [Decimal(10)]


async def test_a_stop_that_already_sold_what_remains_needs_no_replacement(rig: Any) -> None:
    exit_intent = await exit_of(rig, "10")
    result = await rig.coordinator.place_exit(exit_intent.intent.intent_id)
    assert result.placement is not None
    assert result.placement.order_id is not None
    stop_intent = await rig.book.get(rig.stop_intent)
    rig.broker.trades.append(
        {
            "execution_id": "exec-stop",
            "side": "S",
            "size": 10,
            "price": "95",
            "order_ref": stop_intent.coid,
            "conid": STOCK,
            "trade_time_r": int(rig.clock().timestamp() * 1000),
        }
    )
    await rig.store(repo.upsert_fills, await rig.client.trades())
    await rig.boundary.cancel(result.placement.order_id)
    (restored,) = await rig.coordinator.settle()
    assert restored.state is StopResizeState.RESTORED  # nothing left for a new stop to cover
    assert [o for o in rig.broker.live() if o.order_type == "STOP_LIMIT"] == []


def test_a_stop_without_a_remaining_size_is_sized_from_its_total() -> None:
    from datetime import UTC, datetime

    from obliskiq.domain.orders import BrokerOrder
    from obliskiq.execution.exits import _Stop

    order = BrokerOrder(
        order_id="1",
        coid="c",
        conid=STOCK,
        side=OrderSide.SELL,
        sec_type="STK",
        order_type="STOP_LIMIT",
        status="Submitted",
        total_quantity=Decimal(10),
        filled_quantity=Decimal(3),
        remaining_quantity=None,
        average_price=None,
        tif="GTC",
        observed_at=datetime(2026, 10, 13, tzinfo=UTC),
    )
    assert _Stop(stored=None, order=order).remaining == Decimal(7)  # type: ignore[arg-type]
