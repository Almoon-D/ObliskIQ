"""The executor on the fake broker: an intent becomes a filled, protected, reconciled position —
funded in its currency, repriced and cancelled at the window's end, protected after its fills,
resumed by its cOID after a crash, expired on time, simulated in shadow.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fake_broker import EURUSD
from order_rig import EUR_ETF, LEVERAGED, STOCK, spec, terms

from obliskiq.domain.orders import (
    ExecutionMode,
    IntentRole,
    IntentState,
    OrderSide,
    PretradeClearance,
    ReconcileFinding,
    ReconcileRun,
    ReconcileStatus,
    Sleeve,
    StopResizeState,
)
from obliskiq.execution.boundary import OrderAlertKind
from obliskiq.execution.executor import REPRICED, RETRY_NEXT_WINDOW
from obliskiq.ibkr.orders import PlacementStatus
from obliskiq.store import orders as repo

#: The NYSE's last minutes before the order window's end (15 minutes before the 20:00 UTC close).
NEAR_CLOSE = datetime(2026, 10, 13, 19, 42, tzinfo=UTC)
#: The next morning, inside the window again.
NEXT_MORNING = datetime(2026, 10, 14, 14, 30, tzinfo=UTC)


@pytest.fixture
def rig(order_rig: Any) -> Any:
    order_rig.ready()
    order_rig.broker.settle_cash = True
    order_rig.make_execution()
    return order_rig


async def entry(
    rig: Any,
    *,
    quantity: str = "10",
    limit: str = "99.95",
    stop: str | None = "95",
    ttl: timedelta = timedelta(hours=6),
    conid: int = STOCK,
) -> Any:
    """A proposed entry, as the decision layer leaves it."""
    return await rig.intent(
        terms(quantity=quantity, limit=limit, conid=conid),
        role=IntentRole.ENTRY,
        reason="entry",
        state=IntentState.PROPOSED,
        protective=None if stop is None else spec(stop, Decimal("0.02")),
        ttl=ttl,
    )


async def state_of(rig: Any, stored: Any) -> IntentState:
    return (await rig.book.get(stored.intent.intent_id)).intent.state


def orders_for(rig: Any, stored: Any) -> list[Any]:
    return [o for o in rig.broker.orders.values() if o.coid == stored.coid]


def stops(rig: Any) -> list[Any]:
    return [o for o in rig.broker.live() if o.side == "SELL" and o.order_type == "STOP_LIMIT"]


async def test_an_entry_is_placed_filled_protected_and_reconciled(rig: Any) -> None:
    stored = await entry(rig)
    report = await rig.executor.cycle()
    assert await state_of(rig, stored) is IntentState.WORKING
    assert [s.action for s in report.steps if s.intent_id == stored.intent.intent_id] == ["placed"]
    assert report.reconcile is not None
    assert report.reconcile.status is ReconcileStatus.GREEN, report.reconcile.findings
    (order,) = orders_for(rig, stored)
    rig.clock.advance(60)
    rig.broker.fill(order.order_id, price="99.95")
    report = await rig.executor.cycle()
    assert await state_of(rig, stored) is IntentState.FILLED
    (stop,) = stops(rig)
    assert (stop.quantity, stop.aux_price, stop.price, stop.tif) == (
        Decimal(10),
        Decimal(95),
        Decimal("93.1"),
        "GTC",
    )
    holder = await rig.store(repo.holding_assignment, STOCK)
    assert holder.owner is Sleeve.CORE
    assert [p.action for p in report.protective] == ["none"]
    assert rig.alerts == []  # a stop placed after a fill is no alert
    rig.clock.advance(rig.settings.execution.reconcile.grace_seconds + 1)
    assert (await rig.executor.cycle()).reconcile.status is ReconcileStatus.GREEN


async def test_a_partial_fill_gets_a_stop_that_grows_with_the_fills(rig: Any) -> None:
    stored = await entry(rig)
    await rig.executor.cycle()
    (order,) = orders_for(rig, stored)
    rig.broker.fill(order.order_id, "4", price="99.95")
    await rig.executor.cycle()
    assert await state_of(rig, stored) is IntentState.PARTIALLY_FILLED
    assert [s.quantity for s in stops(rig)] == [Decimal(4)]
    rig.broker.fill(order.order_id, "6", price="99.95")
    await rig.executor.cycle()
    assert [s.quantity for s in stops(rig)] == [Decimal(10)]


async def test_a_buy_in_a_foreign_currency_is_funded_by_a_conversion_first(rig: Any) -> None:
    rig.broker.cash["USD"] = (Decimal(100), Decimal("0.86"))
    rig.broker.quote(EURUSD, bid="1.1649", ask="1.1651", last="1.165", sigma="8%")

    async def fills_conversions(seconds: float) -> None:
        rig.clock.advance(seconds)
        for order in rig.broker.live():
            if order.sec_type == "CASH":
                rig.broker.fill(order.order_id, price="1.1645")

    rig.make_execution(sleep=fills_conversions)
    stored = await entry(rig)
    report = await rig.executor.cycle()
    (conversion,) = report.conversions[:1]
    assert conversion.status == "filled"
    assert await state_of(rig, stored) is IntentState.WORKING
    placements = [body.get("isCcyConv", False) for body in rig.broker.placements]
    assert placements == [True, False]  # the conversion first, then the trade


async def test_a_conversion_that_fails_before_the_trade_leaves_it_unplaced(rig: Any) -> None:
    rig.broker.cash["USD"] = (Decimal(100), Decimal("0.86"))
    rig.broker.quote(EURUSD, bid="1.1649", ask="1.1651", last="1.165", sigma="8%")
    stored = await entry(rig)
    rig.broker.reject_next = "Order rejected: currency conversion not permitted"
    report = await rig.executor.cycle()
    assert await state_of(rig, stored) is IntentState.STAGED
    assert orders_for(rig, stored) == []
    waiting = [s for s in report.steps if s.intent_id == stored.intent.intent_id]
    assert [s.action for s in waiting] == ["waiting"]
    assert "failed" in waiting[0].detail
    assert any(a.kind is OrderAlertKind.CONVERSION_FAILED for a in rig.alerts)


async def test_reprices_climb_to_the_touch_then_the_order_is_retried_afresh(rig: Any) -> None:
    stored = await entry(rig, stop=None, ttl=timedelta(days=2))
    await rig.executor.cycle()
    (order,) = orders_for(rig, stored)
    seen = []
    for _ in range(3):
        rig.clock.advance(rig.settings.execution.pricing.reprice_interval_minutes * 60)
        await rig.executor.cycle()
        seen.append(order.price)
    assert seen == [Decimal("100"), Decimal("100.05"), Decimal("100.1")]
    history = await rig.book.history(stored.intent.intent_id)
    assert sum(event.detail.startswith(REPRICED) for event in history) == 3
    rig.clock.advance(rig.settings.execution.pricing.reprice_interval_minutes * 60)
    report = await rig.executor.cycle()
    assert await state_of(rig, stored) is IntentState.CANCELLED
    retried = [s for s in report.steps if s.action == "retried"]
    assert len(retried) == 1
    successor = await rig.book.get(retried[0].intent_id)
    assert successor.intent.parent_id == stored.intent.intent_id
    assert successor.intent.terms.price.limit == Decimal("99.99")  # priced afresh
    assert successor.intent.state is IntentState.WORKING


async def test_an_order_is_cancelled_before_the_window_ends_and_retried_the_next_day(
    rig: Any,
) -> None:
    stored = await entry(rig, stop=None, ttl=timedelta(days=2))
    await rig.executor.cycle()
    rig.clock.now = NEAR_CLOSE
    report = await rig.executor.cycle()
    assert await state_of(rig, stored) is IntentState.CANCELLED
    assert [s for s in report.steps if s.action == "retried"] == []  # not after the close
    history = await rig.book.history(stored.intent.intent_id)
    assert any(event.detail.startswith(RETRY_NEXT_WINDOW) for event in history)
    rig.clock.now = NEXT_MORNING
    report = await rig.executor.cycle()
    (retried,) = [s for s in report.steps if s.action == "retried"]
    assert (await rig.book.get(retried.intent_id)).intent.state is IntentState.WORKING


async def test_an_exit_cancelled_at_the_window_end_restores_its_stop(rig: Any) -> None:
    await rig.hold(STOCK, "10")
    stop = await rig.staged(
        side=OrderSide.SELL, stop="95", limit="94.5", role=IntentRole.PROTECTIVE
    )
    clearance = await rig.gate.clear(stop.intent)
    placed = await rig.boundary.place(stop.intent.intent_id, clearance)
    assert placed.status is PlacementStatus.PLACED
    exit_intent = await rig.intent(
        terms(side=OrderSide.SELL, quantity="4", limit="100.05"),
        role=IntentRole.EXIT,
        reason="target",
        state=IntentState.PROPOSED,
    )
    await rig.executor.cycle()
    assert await state_of(rig, exit_intent) is IntentState.WORKING
    assert rig.broker.orders[placed.order_id].quantity == Decimal(6)  # cut beside the exit
    rig.clock.now = NEAR_CLOSE
    await rig.executor.cycle()
    assert await state_of(rig, exit_intent) is IntentState.CANCELLED
    assert rig.broker.orders[placed.order_id].quantity == Decimal(10)  # restored (I5)
    (resize,) = await rig.store(repo.stop_resizes_for_exit, exit_intent.intent.intent_id)
    assert resize.state is StopResizeState.RESTORED


async def test_the_kill_switch_during_a_reprice_keeps_the_protective_stop(rig: Any) -> None:
    stored = await entry(rig)
    await rig.executor.cycle()
    (order,) = orders_for(rig, stored)
    rig.broker.fill(order.order_id, "4", price="99.95")
    await rig.executor.cycle()
    (stop,) = stops(rig)
    await rig.kill.turn_on(actor="owner", reason="drill")
    rig.clock.advance(rig.settings.execution.pricing.reprice_interval_minutes * 60)
    report = await rig.executor.cycle()
    refused = [s for s in report.steps if s.intent_id == stored.intent.intent_id]
    assert [s.action for s in refused] == ["not repriced"]
    assert "kill_switch_on" in refused[0].detail
    assert order.price == Decimal("99.95")  # the entry was not moved toward the touch
    assert stops(rig) == [stop]  # the stop rests, untouched
    assert stop.quantity == Decimal(4)


async def test_a_crash_after_place_resumes_by_coid_without_a_second_order(rig: Any) -> None:
    stored = await entry(rig, stop=None)
    for step in (IntentState.APPROVED, IntentState.STAGED):
        await rig.book.advance(stored.intent.intent_id, step, "test")
    clearance = await rig.gate.clear(stored.intent)
    assert isinstance(clearance, PretradeClearance)
    rig.broker.lose_next_answer = True  # the order is taken, its answer lost: then the crash
    await rig.boundary.place(stored.intent.intent_id, clearance)
    assert await state_of(rig, stored) is IntentState.SUBMITTED
    restarted = rig.make_execution()
    steps = await restarted.resume()
    assert [(s.intent_id, s.action) for s in steps] == [(stored.intent.intent_id, "found")]
    assert await state_of(rig, stored) is IntentState.WORKING
    await restarted.cycle()
    assert len(orders_for(rig, stored)) == 1


async def test_an_order_listed_late_is_not_placed_twice_after_a_restart(rig: Any) -> None:
    stored = await entry(rig, stop=None)
    for step in (IntentState.APPROVED, IntentState.STAGED):
        await rig.book.advance(stored.intent.intent_id, step, "test")
    clearance = await rig.gate.clear(stored.intent)
    assert isinstance(clearance, PretradeClearance)
    rig.broker.lose_next_answer = True
    rig.broker.listing_delay = 4  # and IBKR is slow to list it
    await rig.boundary.place(stored.intent.intent_id, clearance)
    restarted = rig.make_execution()
    (step,) = await restarted.resume()
    assert step.action == "not found"
    await restarted.cycle()  # inside the grace period: no second placement
    assert len(orders_for(rig, stored)) == 1
    rig.clock.advance(rig.settings.execution.reconcile.grace_seconds + 1)
    rig.broker.orders[orders_for(rig, stored)[0].order_id].hidden_reads = 0
    await restarted.cycle()
    assert len(orders_for(rig, stored)) == 1
    assert await state_of(rig, stored) is IntentState.WORKING


async def test_a_restart_with_staged_and_submitted_intents(rig: Any) -> None:
    staged = await entry(rig, stop=None)
    working = await entry(rig, stop=None)
    unknown = await entry(rig, stop=None)
    for stored in (staged, working, unknown):
        await rig.book.advance(stored.intent.intent_id, IntentState.APPROVED, "test")
        await rig.book.advance(stored.intent.intent_id, IntentState.STAGED, "test")
    for stored, lose in ((working, False), (unknown, True)):
        clearance = await rig.gate.clear(stored.intent)
        assert isinstance(clearance, PretradeClearance)
        rig.broker.lose_next_answer = lose
        await rig.boundary.place(stored.intent.intent_id, clearance)
    assert await state_of(rig, unknown) is IntentState.SUBMITTED
    restarted = rig.make_execution()
    steps = {s.intent_id: s.action for s in await restarted.resume()}
    assert steps == {working.intent.intent_id: "found", unknown.intent.intent_id: "found"}
    await restarted.cycle()
    for stored in (staged, working, unknown):
        assert await state_of(rig, stored) is IntentState.WORKING
        assert len(orders_for(rig, stored)) == 1


async def test_resume_alerts_an_intent_the_broker_does_not_list(rig: Any) -> None:
    stored = await entry(rig, stop=None)
    await rig.executor.cycle()
    for order in orders_for(rig, stored):
        del rig.broker.orders[order.order_id]  # gone without a trace
    (step,) = await rig.make_execution().resume()
    assert step.action == "unresolved"
    assert rig.alerts[-1].kind is OrderAlertKind.RESUME_UNRESOLVED
    rig.broker.snapshot = False
    (step,) = await rig.make_execution().resume()
    assert "lookup failed" in step.detail


async def test_intents_end_at_their_expiry_and_a_working_order_is_cancelled(rig: Any) -> None:
    waiting = await rig.intent(
        terms(quantity="1", limit="99.95"),
        role=IntentRole.ENTRY,
        reason="entry",
        state=IntentState.PROPOSED,
        approval_required=True,  # held for the owner, who never answers
        ttl=timedelta(minutes=30),
    )
    working = await entry(rig, stop=None, ttl=timedelta(minutes=30))
    await rig.executor.cycle()
    assert await state_of(rig, working) is IntentState.WORKING
    rig.clock.advance(31 * 60)
    await rig.executor.cycle()
    assert await state_of(rig, waiting) is IntentState.EXPIRED
    assert await state_of(rig, working) is IntentState.CANCELLED
    assert [o.status for o in orders_for(rig, working)] == ["Cancelled"]


async def test_a_permanent_refusal_rejects_and_a_passing_one_waits(rig: Any) -> None:
    prohibited = await entry(rig, stop=None, conid=LEVERAGED)
    await rig.executor.cycle()
    assert await state_of(rig, prohibited) is IntentState.REJECTED
    rig.clock.now = NEXT_MORNING.replace(hour=4)  # before the NYSE opens
    later = await entry(rig, stop=None)
    report = await rig.executor.cycle()
    assert await state_of(rig, later) is IntentState.STAGED
    (step,) = [s for s in report.steps if s.intent_id == later.intent.intent_id]
    assert step.action == "waiting"
    assert "venue_not_tradable" in step.detail


async def test_a_frozen_book_stages_no_new_risk_and_approval_holds_an_intent(rig: Any) -> None:
    finding = ReconcileFinding(kind="test", detail="a test finding", freezes=True)
    await rig.store(
        repo.record_reconciliation,
        ReconcileRun(at=rig.clock(), status=ReconcileStatus.DRIFT, findings=(finding,)),
    )
    buy = await entry(rig, stop=None)
    held = await rig.intent(
        terms(quantity="1", limit="99.95"),
        role=IntentRole.ENTRY,
        reason="entry",
        state=IntentState.PROPOSED,
        approval_required=True,
    )
    report = await rig.executor.cycle()
    assert await state_of(rig, buy) is IntentState.APPROVED
    assert ("frozen", buy.intent.intent_id) in {(s.action, s.intent_id) for s in report.steps}
    assert await state_of(rig, held) is IntentState.PROPOSED


async def test_shadow_mode_simulates_the_fill_on_the_listing_venue(rig: Any) -> None:
    collar = rig.settings.execution.collar.model_copy(update={"base_fraction": 0.02})
    settings = rig.settings.model_copy(
        update={"execution": rig.settings.execution.model_copy(update={"collar": collar})}
    )
    rig.make_gate(settings=settings)
    rig.make_execution(settings=settings)
    await rig.modes.downgrade(ExecutionMode.SHADOW, actor="test", reason="test")
    stored = await rig.intent(
        terms(quantity="10", limit="101.5", conid=EUR_ETF),
        role=IntentRole.ENTRY,
        reason="entry",
        state=IntentState.PROPOSED,
        mode=ExecutionMode.SHADOW,
    )
    rig.broker.whatif_answer["amount"]["total"] = "1001 EUR"
    report = await rig.executor.cycle()
    assert await state_of(rig, stored) is IntentState.FILLED
    (fill,) = await rig.store(repo.fills_for_intent, stored.intent.intent_id)
    assert fill.simulated
    assert fill.price == Decimal("100.1")  # the ask: a limit through the touch fills there
    assert fill.commission == Decimal("3.00")  # europe_eur, the listing venue's row
    assert rig.broker.placements == []  # nothing reached the broker
    assert [s.action for s in report.steps if s.intent_id == stored.intent.intent_id] == [
        "shadow fill"
    ]


async def test_a_shadow_order_outside_its_limit_rests_unfilled(rig: Any) -> None:
    await rig.modes.downgrade(ExecutionMode.SHADOW, actor="test", reason="test")
    stored = await rig.intent(
        terms(quantity="10", limit="99.95"),
        role=IntentRole.ENTRY,
        reason="entry",
        state=IntentState.PROPOSED,
        mode=ExecutionMode.SHADOW,
    )
    report = await rig.executor.cycle()
    assert await state_of(rig, stored) is IntentState.SUBMITTED
    (step,) = [s for s in report.steps if s.intent_id == stored.intent.intent_id]
    assert step.action == "shadow, unfilled"
    assert rig.broker.placements == []


async def shadow_resting(rig: Any) -> Any:
    """A shadow entry whose patient limit rests below the ask after its first simulation."""
    await rig.modes.downgrade(ExecutionMode.SHADOW, actor="test", reason="test")
    rig.clock.now = datetime(2026, 10, 13, 14, 30, tzinfo=UTC)
    rig.broker.whatif_answer["amount"]["total"] = "1001 EUR"
    stored = await rig.intent(
        terms(quantity="10", limit="99.95", conid=EUR_ETF),
        role=IntentRole.ENTRY,
        reason="entry",
        state=IntentState.PROPOSED,
        mode=ExecutionMode.SHADOW,
    )
    await rig.executor.cycle()
    assert await state_of(rig, stored) is IntentState.SUBMITTED
    return stored


async def test_a_resting_shadow_order_is_repriced_to_the_touch_and_fills(rig: Any) -> None:
    """DECISIONS D103: repriced on the real clock, each reprice only changes its terms."""
    stored = await shadow_resting(rig)
    report = await rig.executor.cycle()  # too soon: the interval has not passed
    assert not [s for s in report.steps if s.action == "repriced in shadow"]
    limits = []
    for _ in range(3):
        rig.clock.advance(5 * 60)
        report = await rig.executor.cycle()
        limits.append((await rig.book.get(stored.intent.intent_id)).intent.terms.price.limit)
    assert limits[-1] == Decimal("100.1")  # the last reprice reaches the ask
    assert await state_of(rig, stored) is IntentState.FILLED
    (fill,) = await rig.store(repo.fills_for_intent, stored.intent.intent_id)
    assert fill.price == Decimal("100.1")
    assert rig.broker.placements == []


async def test_an_unfilled_shadow_order_is_given_up_and_retried_at_the_next_window(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obliskiq.execution.shadow import ShadowOutcome

    stored = await shadow_resting(rig)
    resting = ShadowOutcome(fill=None, cost=None, reason="the market never came to it")
    monkeypatch.setattr(rig.executor._shadow, "simulate", lambda *_a, **_k: resting)
    for _ in range(4):
        rig.clock.advance(5 * 60)
        await rig.executor.cycle()
    history = await rig.book.history(stored.intent.intent_id)
    assert sum(e.detail.startswith(REPRICED) for e in history) == 3
    assert await state_of(rig, stored) is IntentState.CANCELLED
    assert any(e.detail.startswith(RETRY_NEXT_WINDOW) for e in history)
    rig.clock.now = NEXT_MORNING
    await rig.executor.cycle()
    successors = [
        s
        for s in await rig.store(repo.intents_for_conid, EUR_ETF)
        if s.intent.parent_id == stored.intent.intent_id
    ]
    assert len(successors) == 1
    assert successors[0].intent.mode is ExecutionMode.SHADOW


async def test_a_shadow_limit_the_collar_stops_waits_for_the_windows_end(rig: Any) -> None:
    stored = await shadow_resting(rig)
    rig.broker.quote(EUR_ETF, bid="99.0", ask="101.0", last="100")  # the touch beyond the collar
    for _ in range(4):
        rig.clock.advance(5 * 60)
        await rig.executor.cycle()
    assert await state_of(rig, stored) is IntentState.SUBMITTED
    limit = (await rig.book.get(stored.intent.intent_id)).intent.terms.price.limit
    assert limit == Decimal("100.5")  # the collar: 0.5 % above the reference


async def test_a_shadow_order_is_given_up_while_its_window_is_still_open(rig: Any) -> None:
    stored = await shadow_resting(rig)
    rig.clock.now = NEAR_CLOSE
    report = await rig.executor.cycle()
    assert await state_of(rig, stored) is IntentState.CANCELLED
    assert [s.detail for s in report.steps if s.action == "cancelled in shadow"] == [
        "its venue's window is ending"
    ]


async def test_a_shadow_reprice_without_a_quote_or_rules_waits(rig: Any) -> None:
    stored = await shadow_resting(rig)
    rig.clock.advance(5 * 60)
    rig.broker.unavailable.add("rules")
    report = await rig.executor.cycle()
    ours = [s for s in report.steps if s.intent_id == stored.intent.intent_id]
    assert any(s.action == "not repriced" and "cannot read" in s.detail for s in ours)
    rig.broker.unavailable.clear()
    del rig.broker.quotes[EUR_ETF]
    report = await rig.executor.cycle()
    ours = [s for s in report.steps if s.intent_id == stored.intent.intent_id]
    assert any(s.action == "not repriced" and s.detail == "no quote" for s in ours)


async def test_a_shadow_reprice_that_would_not_move_waits(rig: Any) -> None:
    stored = await shadow_resting(rig)
    rig.broker.quote(EUR_ETF, bid="99.0", ask="99.9", last="99.5")  # the ask is under the limit
    rig.clock.advance(5 * 60)
    report = await rig.executor.cycle()
    ours = [s for s in report.steps if s.intent_id == stored.intent.intent_id]
    assert any(s.action == "not repriced" and "gives no better limit" in s.detail for s in ours)


async def test_shadow_orders_are_not_worked_outside_shadow_mode_or_without_a_mode(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = await shadow_resting(rig)
    await rig.modes.upgrade(ExecutionMode.PAPER, actor="test", reason="test")
    rig.clock.advance(5 * 60)
    await rig.executor.cycle()
    history = await rig.book.history(stored.intent.intent_id)
    assert not any(e.detail.startswith(REPRICED) for e in history)

    async def unreadable() -> Any:
        raise RuntimeError("the store is locked")

    monkeypatch.setattr(rig.modes, "current", unreadable)
    await rig.executor._work_shadow(type("R", (), {"add": lambda *_a: None})())


# ── failure branches ──


def broken(message: str = "the boundary refused") -> Any:
    """A boundary call that fails as the broker's errors do."""
    from obliskiq.errors import ObliskError

    async def call(*args: Any, **kwargs: Any) -> Any:
        raise ObliskError(message)

    return call


async def placed(rig: Any, **kwargs: Any) -> Any:
    """An entry placed by one cycle, and its order at the broker."""
    stored = await entry(rig, stop=None, ttl=timedelta(days=2), **kwargs)
    await rig.executor.cycle()
    (order,) = orders_for(rig, stored)
    return stored, order


def steps_of(report: Any, stored: Any) -> list[tuple[str, str]]:
    return [(s.action, s.detail) for s in report.steps if s.intent_id == stored.intent.intent_id]


def interval(rig: Any) -> int:
    return int(rig.settings.execution.pricing.reprice_interval_minutes * 60)


async def test_a_cycle_whose_reads_fail_places_nothing_and_is_never_green(rig: Any) -> None:
    rig.broker.unavailable.add("orders")
    stored = await entry(rig, stop=None)
    report = await rig.executor.cycle()
    assert report.reconcile is not None
    assert report.reconcile.status is ReconcileStatus.INCOMPLETE
    assert orders_for(rig, stored) == []


async def test_a_reprice_that_cannot_read_or_move_is_reported(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections import deque

    stored, order = await placed(rig)
    rig.clock.advance(interval(rig))
    rig.broker.unavailable.add("rules")
    assert steps_of(await rig.executor.cycle(), stored)[0][1].startswith("cannot read")
    rig.broker.unavailable.clear()
    del rig.broker.quotes[STOCK]
    assert steps_of(await rig.executor.cycle(), stored) == [("not repriced", "no quote")]
    rig.broker.quote(STOCK, bid="99.4", ask="99.5", last="99.45")  # the touch fell below it
    action, detail = steps_of(await rig.executor.cycle(), stored)[0]
    assert (action, detail.startswith("the touch 99.5")) == ("not repriced", True)
    rig.broker.quote(STOCK)
    monkeypatch.setattr(rig.boundary, "modify", broken())
    assert steps_of(await rig.executor.cycle(), stored) == [
        ("not repriced", "the boundary refused")
    ]
    monkeypatch.undo()
    rig.broker.questions = deque([(["o99999"], "a warning nobody knows")])
    action, detail = steps_of(await rig.executor.cycle(), stored)[0]
    assert (action, detail.startswith("declined")) == ("not repriced", True)
    assert order.price == Decimal("99.95")


async def test_an_unknown_venue_window_gives_the_order_up(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obliskiq.errors import CalendarError

    stored, _ = await placed(rig)

    def unknown(*args: Any, **kwargs: Any) -> Any:
        raise CalendarError("no such venue")

    monkeypatch.setattr(rig.calendars, "tradability", unknown)
    await rig.executor.cycle()
    assert await state_of(rig, stored) is IntentState.CANCELLED


async def test_a_working_intent_with_no_order_recorded_is_left_alone(rig: Any) -> None:
    stored = await rig.intent(
        terms(quantity="1", limit="99.95"), role=IntentRole.ENTRY, state=IntentState.SUBMITTED
    )
    await rig.book.advance(stored.intent.intent_id, IntentState.WORKING, "test")
    rig.clock.advance(interval(rig))
    report = await rig.executor.cycle()
    assert steps_of(report, stored) == []
    assert await state_of(rig, stored) is IntentState.WORKING


async def test_a_cancel_that_fails_at_expiry_is_reported_and_an_unsent_one_expires(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    working = await entry(rig, stop=None, ttl=timedelta(minutes=30))
    await rig.executor.cycle()
    unsent = await rig.intent(
        terms(quantity="1", limit="99.95"),
        role=IntentRole.ENTRY,
        state=IntentState.SUBMITTED,
        ttl=timedelta(minutes=30),
    )
    rig.broker.listing_delay = 0
    rig.clock.advance(31 * 60)
    monkeypatch.setattr(rig.boundary, "cancel", broken())
    report = await rig.executor.cycle()
    assert steps_of(report, working)[0] == ("cancel failed", "expired: the boundary refused")
    assert await state_of(rig, working) is IntentState.WORKING
    assert await state_of(rig, unsent) is IntentState.EXPIRED  # nothing working to cancel


async def test_a_retry_needs_its_reads_a_price_and_a_size(rig: Any) -> None:
    stored, _ = await placed(rig)
    rig.clock.now = NEAR_CLOSE
    await rig.executor.cycle()  # given up before the close
    rig.clock.now = NEXT_MORNING
    rig.broker.unavailable.add("rules")
    report = await rig.executor.cycle()
    assert steps_of(report, stored)[0][0] == "not retried"
    rig.broker.unavailable.clear()
    rig.broker.rules_by_conid[STOCK] = {"orderTypes": ["limit"], "incrementRules": []}
    report = await rig.executor.cycle()
    assert steps_of(report, stored) == [
        ("not retried", "the contract's rules give no tick at this price")
    ]
    del rig.broker.rules_by_conid[STOCK]
    del rig.broker.quotes[STOCK]
    assert steps_of(await rig.executor.cycle(), stored) == []  # no quote: it waits
    rig.broker.quote(STOCK)
    (retried,) = [s for s in (await rig.executor.cycle()).steps if s.action == "retried"]
    report = await rig.executor.cycle()
    assert [s for s in report.steps if s.action == "retried"] == []  # one successor only
    assert (await rig.book.get(retried.intent_id)).intent.parent_id == stored.intent.intent_id


async def test_a_cancelled_order_is_retried_only_when_the_executor_gave_it_up(rig: Any) -> None:
    stored, order = await placed(rig)
    order.status = "Cancelled"  # the owner cancelled it at the broker
    report = await rig.executor.cycle()
    assert await state_of(rig, stored) is IntentState.CANCELLED
    assert [s for s in report.steps if s.action == "retried"] == []
    cash = await rig.intent(
        terms(cash="500", limit="99.95"), role=IntentRole.ENTRY, state=IntentState.SUBMITTED
    )
    await rig.book.fact(cash.intent.intent_id, f"{RETRY_NEXT_WINDOW}: test")
    await rig.book.advance(cash.intent.intent_id, IntentState.CANCELLED, "test")
    report = await rig.executor.cycle()
    assert [s for s in report.steps if s.action == "retried"] == []  # sized in cash: not retried


async def test_an_approval_carries_over_to_the_successor(rig: Any) -> None:
    from obliskiq.domain.orders import ApprovalDecision

    stored = await rig.intent(
        terms(quantity="1", limit="99.95"),
        role=IntentRole.ENTRY,
        state=IntentState.APPROVED,
        approval_required=True,
        ttl=timedelta(days=2),
    )
    await rig.store(
        repo.record_approval,
        ApprovalDecision(
            intent_id=stored.intent.intent_id,
            approved=True,
            actor="owner",
            reason="yes",
            at=rig.clock(),
        ),
    )
    await rig.executor.cycle()
    rig.clock.now = NEAR_CLOSE
    await rig.executor.cycle()
    rig.clock.now = NEXT_MORNING
    (retried,) = [s for s in (await rig.executor.cycle()).steps if s.action == "retried"]
    decision = await rig.store(repo.approval_for, retried.intent_id)
    assert decision.approved
    assert "carried over" in decision.reason


async def test_placement_failures_leave_the_intent_staged(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = await entry(rig, stop=None)

    async def unreadable() -> Any:
        raise RuntimeError("the store is gone")

    monkeypatch.setattr(rig.modes, "current", unreadable)
    await rig.executor.cycle()
    assert await state_of(rig, stored) is IntentState.STAGED
    monkeypatch.undo()
    monkeypatch.setattr(rig.boundary, "place", broken())
    report = await rig.executor.cycle()
    assert steps_of(report, stored) == [("not placed", "the boundary refused")]
    assert await state_of(rig, stored) is IntentState.STAGED


async def test_an_exit_the_gate_refuses_waits(rig: Any) -> None:
    exit_intent = await rig.intent(
        terms(side=OrderSide.SELL, quantity="4", limit="100.05"),
        role=IntentRole.EXIT,
        reason="target",
        state=IntentState.PROPOSED,
    )
    report = await rig.executor.cycle()  # nothing held: the sale would go short
    (step,) = steps_of(report, exit_intent)
    assert step[0] == "waiting"
    assert await state_of(rig, exit_intent) is IntentState.STAGED


async def test_an_unknown_outcome_is_placed_again_past_the_grace_and_the_coid_holds(
    rig: Any,
) -> None:
    stored = await entry(rig, stop=None)
    for step in (IntentState.APPROVED, IntentState.STAGED):
        await rig.book.advance(stored.intent.intent_id, step, "test")
    clearance = await rig.gate.clear(stored.intent)
    assert isinstance(clearance, PretradeClearance)
    rig.broker.lose_next_answer = True
    rig.broker.listing_delay = 100  # taken, and not listed for a long while
    await rig.boundary.place(stored.intent.intent_id, clearance)
    conversion = await rig.intent(  # a conversion its own manager follows
        terms(side=OrderSide.SELL, cash="1000", limit="1.162", conversion=True, conid=EURUSD),
        role=IntentRole.CONVERSION,
        reason="funding",
        state=IntentState.SUBMITTED,
    )
    rig.clock.advance(rig.settings.execution.reconcile.grace_seconds + 1)
    report = await rig.executor.cycle()
    assert [a for a, _ in steps_of(report, stored)] == ["duplicate"]  # IBKR refused the cOID
    assert len(orders_for(rig, stored)) == 1
    assert steps_of(report, conversion) == []


async def test_managed_roles_are_never_staged_by_the_executor(rig: Any) -> None:
    proposed = await rig.intent(
        terms(side=OrderSide.SELL, cash="1000", limit="1.162", conversion=True, conid=EURUSD),
        role=IntentRole.CONVERSION,
        reason="funding",
        state=IntentState.PROPOSED,
    )
    approved = await rig.intent(
        terms(side=OrderSide.SELL, quantity="1", stop="95", limit="94"),
        role=IntentRole.PROTECTIVE,
        reason="stop",
        state=IntentState.APPROVED,
        protective=spec("95"),
    )
    await rig.executor.cycle()
    assert await state_of(rig, proposed) is IntentState.PROPOSED
    assert await state_of(rig, approved) is IntentState.APPROVED


async def test_a_fill_without_a_spec_is_registered_and_followed_once(rig: Any) -> None:
    _, order = await placed(rig)
    rig.broker.fill(order.order_id, price="99.95")
    await rig.executor.cycle()
    assert (await rig.store(repo.holding_assignment, STOCK)).owner is Sleeve.CORE
    assert stops(rig) == []
    rig.clock.advance(60)
    await rig.executor.cycle()
    rig.clock.advance(60)
    await rig.executor.cycle()  # filled before the previous cycle: not followed again
    assert stops(rig) == []


async def test_resume_leaves_shadow_intents_to_the_simulator(rig: Any) -> None:
    await rig.intent(
        terms(quantity="1", limit="99.95"),
        role=IntentRole.ENTRY,
        state=IntentState.SUBMITTED,
        mode=ExecutionMode.SHADOW,
    )
    assert await rig.make_execution().resume() == []


async def test_shadow_refusals_and_unreadable_quotes_are_reported(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await rig.modes.downgrade(ExecutionMode.SHADOW, actor="test", reason="test")
    refused = await rig.intent(
        terms(quantity="10", limit="99.95", conid=LEVERAGED),
        role=IntentRole.ENTRY,
        state=IntentState.STAGED,
        mode=ExecutionMode.SHADOW,
    )
    paper = await rig.intent(  # an intent of the paper mode: not simulated
        terms(quantity="1", limit="99.95"), role=IntentRole.ENTRY, state=IntentState.STAGED
    )
    report = await rig.executor.cycle()
    assert await state_of(rig, refused) is IntentState.REJECTED
    assert steps_of(report, paper) == []
    unfilled = await rig.intent(
        terms(quantity="10", limit="99.95"),
        role=IntentRole.ENTRY,
        state=IntentState.STAGED,
        mode=ExecutionMode.SHADOW,
    )
    await rig.executor.cycle()
    assert await state_of(rig, unfilled) is IntentState.SUBMITTED
    report = await rig.executor.cycle()  # submitted already: simulated again
    assert steps_of(report, unfilled)[0][0] == "shadow, unfilled"
    reader_quote = rig.reader.quote
    calls = 0

    async def fails_after_the_gate(conid: int, *, option: bool) -> Any:
        nonlocal calls
        calls += 1
        if calls % 2 == 0:  # the gate's read passes; the simulator's fails
            raise broken_error()
        return await reader_quote(conid, option=option)

    def broken_error() -> Exception:
        from obliskiq.errors import ObliskError

        return ObliskError("the quote went away")

    monkeypatch.setattr(rig.reader, "quote", fails_after_the_gate)
    report = await rig.executor.cycle()
    assert steps_of(report, unfilled)[0] == ("not simulated", "the quote went away")

    async def none_after_the_gate(conid: int, *, option: bool) -> Any:
        nonlocal calls
        calls += 1
        return None if calls % 2 == 0 else await reader_quote(conid, option=option)

    calls = 0
    monkeypatch.setattr(rig.reader, "quote", none_after_the_gate)
    report = await rig.executor.cycle()
    assert steps_of(report, unfilled)[0] == ("not simulated", "no instrument or quote")


async def test_an_order_still_working_outside_its_window_is_given_up(rig: Any) -> None:
    stored, _ = await placed(rig)
    rig.clock.now = NEXT_MORNING.replace(hour=4)  # the venue closed with the order working
    await rig.executor.cycle()
    assert await state_of(rig, stored) is IntentState.CANCELLED
    history = await rig.book.history(stored.intent.intent_id)
    assert any(event.detail.startswith(RETRY_NEXT_WINDOW) for event in history)
