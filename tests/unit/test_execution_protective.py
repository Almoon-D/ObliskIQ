"""Protective exits on the fake broker: stops placed after fills and resized with them, verified at
every heartbeat (re-placed with the kill switch on), cut to the position, raised by roll-ups, and
a gap through the limit repriced through the gate, never to a market order (DECISIONS D67).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from decimal import Decimal
from typing import Any

import pytest
from order_rig import STOCK, spec

from obliskiq.config import ObliskConfig
from obliskiq.domain.orders import (
    IntentRole,
    IntentState,
    OrderSide,
    PretradeClearance,
    Sleeve,
)
from obliskiq.execution.boundary import OrderAlertKind
from obliskiq.execution.protective import (
    GAP_REPRICE,
    ProtectiveManager,
    limit_offset,
    protective_spec,
)
from obliskiq.ibkr.orders import PlacementStatus
from obliskiq.store import orders as repo


def manager(rig: Any, settings: ObliskConfig | None = None) -> ProtectiveManager:
    return ProtectiveManager(
        settings if settings is not None else rig.settings,
        gate=rig.gate,
        boundary=rig.boundary,
        book=rig.book,
        worker=rig.worker,
        reader=rig.reader,
        rules=rig.ibkr.contracts,
        modes=rig.modes,
        alert=rig.alerts.append,
        now=rig.clock,
    )


@pytest.fixture
def rig(order_rig: Any) -> Any:
    order_rig.ready()
    order_rig.protective = manager(order_rig)
    return order_rig


async def entry(rig: Any, quantity: str = "10", stop: str = "95") -> Any:
    """A placed entry that carries a protective spec: a stop at ``stop``, 2 % deep."""
    stored = await rig.intent(
        terms_for(quantity),
        role=IntentRole.ENTRY,
        reason="entry",
        protective=spec(stop, Decimal("0.02")),
    )
    clearance = await rig.gate.clear(stored.intent)
    assert isinstance(clearance, PretradeClearance), clearance
    placed = await rig.boundary.place(stored.intent.intent_id, clearance)
    assert placed.status is PlacementStatus.PLACED
    rig.entry_order = placed.order_id
    await rig.assign(STOCK, Sleeve.CORE)
    return stored


def terms_for(quantity: str) -> Any:
    from order_rig import terms

    return terms(quantity=quantity, limit="100.05")


async def fill(rig: Any, order_id: str, quantity: str | None = None) -> None:
    """The broker fills, and the store records the trades and the order as the executor does."""
    rig.broker.fill(order_id, quantity)
    await rig.store(repo.upsert_fills, list(await rig.client.trades()))
    await rig.boundary.sync_order(order_id)


def stops(rig: Any) -> list[Any]:
    return [o for o in rig.broker.live() if o.side == "SELL" and o.order_type == "STOP_LIMIT"]


def working_sells(rig: Any) -> Decimal:
    return sum((o.quantity - o.filled for o in rig.broker.live() if o.side == "SELL"), Decimal(0))


# ── the spec ──


def test_the_limit_offset_widens_with_volatility_and_gaps(shipped_settings: ObliskConfig) -> None:
    floor, basis = limit_offset(shipped_settings, daily_sigma=None, gap_quantile=None)
    assert floor == Decimal("0.01")
    assert "no daily sigma or gap quantile" in basis
    vol, _ = limit_offset(shipped_settings, daily_sigma=Decimal("0.02"), gap_quantile=None)
    assert vol == Decimal("0.02")
    gaps, basis = limit_offset(
        shipped_settings, daily_sigma=Decimal("0.02"), gap_quantile=Decimal("0.045")
    )
    assert gaps == Decimal("0.045")
    assert basis.startswith("gap quantile")
    assert floor < vol < gaps
    capped, basis = limit_offset(shipped_settings, daily_sigma=None, gap_quantile=Decimal("0.3"))
    assert capped == Decimal("0.10")
    assert "capped at 0.10" in basis
    built = protective_spec(
        shipped_settings, Decimal(95), daily_sigma=Decimal("0.03"), gap_quantile=None
    )
    assert (built.stop, built.limit_offset_fraction) == (Decimal(95), Decimal("0.03"))


# ── coverage ──


async def test_a_fill_gets_its_stop_and_a_partial_fill_resizes_it(rig: Any) -> None:
    await entry(rig)
    await fill(rig, rig.entry_order, "4")
    outcome = await rig.protective.protect(STOCK)
    assert outcome.action == "placed"
    (stop,) = stops(rig)
    assert (stop.quantity, stop.aux_price, stop.price) == (
        Decimal(4),
        Decimal(95),
        Decimal("93.1"),  # 2 % below 95, rounded up to the cent
    )
    assert stop.tif == "GTC"
    await fill(rig, rig.entry_order, "6")
    outcome = await rig.protective.protect(STOCK)
    assert outcome.action == "resized up"
    (stop,) = stops(rig)
    assert stop.quantity == Decimal(10)
    assert (await rig.protective.protect(STOCK)).action == "none"
    assert rig.alerts == []  # placing a stop after a fill is no alert


async def test_a_missing_stop_is_replaced_and_alerted_also_with_the_kill_switch_on(
    rig: Any,
) -> None:
    await entry(rig)
    await fill(rig, rig.entry_order)
    await rig.protective.protect(STOCK)
    (stop,) = stops(rig)
    stop.status = "Cancelled"  # someone cancelled it at the broker
    await rig.kill.turn_on(actor="owner", reason="drill")
    (outcome,) = await rig.protective.verify()
    assert outcome.action == "placed"
    (replaced,) = stops(rig)
    assert replaced.order_id != stop.order_id
    assert replaced.quantity == Decimal(10)
    assert working_sells(rig) == Decimal(10)
    assert [alert.kind for alert in rig.alerts] == [OrderAlertKind.STOP_MISSING]
    assert "placed" in rig.alerts[0].detail


async def test_a_stop_beyond_the_position_is_cut_to_it(rig: Any) -> None:
    await entry(rig)
    await fill(rig, rig.entry_order)
    await rig.protective.protect(STOCK)
    rig.broker.hold(STOCK, "6")  # four shares left the account outside the bot
    (outcome,) = await rig.protective.verify()
    assert outcome.action == "resized down"
    (stop,) = stops(rig)
    assert stop.quantity == Decimal(6)
    rig.broker.hold(STOCK, "0")
    (outcome,) = await rig.protective.verify()
    assert stops(rig) == []  # a stop on no position would sell short: cancelled


async def test_a_partial_exit_beside_the_stop_never_oversells(rig: Any) -> None:
    await entry(rig)
    await fill(rig, rig.entry_order)
    await rig.protective.protect(STOCK)
    exit_intent = await rig.staged(side=OrderSide.SELL, quantity="4", limit="99.6")
    result = await rig.coordinator.place_exit(exit_intent.intent.intent_id)
    assert result.placed
    assert working_sells(rig) == Decimal(10)
    (outcome,) = await rig.protective.verify()
    assert outcome.action == "none"  # the stop covers the six the exit does not sell
    assert working_sells(rig) == Decimal(10)
    assert rig.alerts == []


async def test_a_stop_replaced_while_flattens_exit_works_and_is_not_listed_never_oversells(
    rig: Any,
) -> None:
    await entry(rig)
    await fill(rig, rig.entry_order)
    await rig.protective.protect(STOCK)
    rig.broker.listing_delay = 5  # the flatten exit is taken, but the broker lists it late
    report = await rig.flattener.flatten(actor="owner")
    assert report.ok, report.render()
    assert stops(rig) == []  # flatten's exit took the stop's place
    outcomes = await rig.protective.verify()
    assert [o.action for o in outcomes] == ["none"]
    assert working_sells(rig) == Decimal(10)
    # Even a stop placed for the whole position is refused while the exit may sell it (I2).
    stop = await rig.staged(
        side=OrderSide.SELL, stop="95", limit="94.5", role=IntentRole.PROTECTIVE
    )
    refusal = await rig.gate.clear(stop.intent)
    assert not isinstance(refusal, PretradeClearance)
    assert "short" in refusal.codes


async def test_a_position_without_a_spec_is_not_protected(rig: Any) -> None:
    core = await rig.staged()  # a core buy carries no protective spec
    clearance = await rig.gate.clear(core.intent)
    placed = await rig.boundary.place(core.intent.intent_id, clearance)
    await rig.assign(STOCK)
    await fill(rig, placed.order_id)
    assert await rig.protective.verify() == []
    assert stops(rig) == []


async def test_the_heartbeat_reports_what_it_cannot_read(rig: Any) -> None:
    rig.broker.unavailable.add("positions")
    assert await rig.protective.verify() == []
    assert rig.alerts[-1].kind is OrderAlertKind.STOP_MISSING
    assert "cannot be verified" in rig.alerts[-1].detail
    rig.broker.unavailable.clear()
    rig.broker.snapshot = False
    assert await rig.protective.verify() == []
    assert "partial" in rig.alerts[-1].detail
    outcome = await rig.protective.protect(STOCK)
    assert outcome.action == "unread"


async def test_a_stop_the_gate_refuses_is_alerted_and_rejected(rig: Any) -> None:
    await entry(rig, stop="99.9")  # a stop inside the collar's band is refused
    await fill(rig, rig.entry_order)
    (outcome,) = await rig.protective.verify()
    assert outcome.action == "refused"
    assert "protective_spec" in outcome.detail
    assert stops(rig) == []
    assert rig.alerts[-1].kind is OrderAlertKind.STOP_MISSING
    rejected = await rig.store(repo.intents_in_states, [IntentState.REJECTED])
    assert [s.intent.role for s in rejected] == [IntentRole.PROTECTIVE]


async def test_an_unlisted_stop_counts_while_new_and_is_looked_up_after_the_grace(
    rig: Any,
) -> None:
    await entry(rig)
    await fill(rig, rig.entry_order)
    rig.broker.listing_delay = 3
    await rig.protective.protect(STOCK)
    assert (await rig.protective.protect(STOCK)).action == "none"  # within the grace period
    (stop,) = stops(rig)
    rig.clock.advance(rig.settings.execution.reconcile.grace_seconds + 1)
    stop.hidden_reads = 10
    assert (await rig.protective.protect(STOCK)).action == "none"  # its status says working
    rig.broker.status_unavailable = True
    rig.clock.advance(rig.settings.execution.reconcile.grace_seconds + 1)
    (outcome,) = await rig.protective.verify()
    assert outcome.action == "refused"  # the gate refuses a second stop while one may work
    assert working_sells(rig) == Decimal(10)


# ── roll-ups ──


async def test_a_roll_up_raises_the_stop_and_never_lowers_it(rig: Any) -> None:
    await entry(rig)
    await fill(rig, rig.entry_order)
    await rig.protective.protect(STOCK)
    assert await rig.protective.roll_up(STOCK, Decimal(97)) == []
    (stop,) = stops(rig)
    assert (stop.aux_price, stop.price) == (Decimal(97), Decimal("95.06"))
    assert await rig.protective.roll_up(STOCK, Decimal(96)) == []
    assert stops(rig)[0].aux_price == Decimal(97)
    failures = await rig.protective.roll_up(STOCK, Decimal("99.9"))  # inside the collar's band
    assert len(failures) == 1
    assert "protective_spec" in failures[0]


# ── gaps ──


async def gapped(rig: Any) -> Any:
    await entry(rig)
    await fill(rig, rig.entry_order)
    await rig.protective.protect(STOCK)
    rig.broker.quote(STOCK, bid="90", ask="90.2", last="90.1")  # opened through the stop
    (stop,) = stops(rig)
    return stop


async def test_a_gap_through_the_limit_is_repriced_through_the_gate_never_to_a_market_order(
    rig: Any,
) -> None:
    stop = await gapped(rig)
    limits = []
    for _ in range(8):
        done = await rig.protective.reprice_gaps()
        if done:
            limits.append(stop.price)
        assert stop.order_type == "STOP_LIMIT"
        rig.clock.advance(rig.settings.execution.protective.gap.step_interval_minutes * 60)
        rig.broker.quote(STOCK, bid="90", ask="90.2", last="90.1")
    assert limits == [
        Decimal("92.64"),
        Decimal("92.18"),
        Decimal("91.72"),
        Decimal("91.26"),
        Decimal("90.80"),
        Decimal("90.34"),
    ]  # six steps of 0.5 % of the reference (90.1) each, rounded down to the cent
    stored = await rig.store(repo.intents_in_states, [IntentState.WORKING])
    (protective,) = [s for s in stored if s.intent.role is IntentRole.PROTECTIVE]
    history = await rig.book.history(protective.intent.intent_id)
    assert sum(event.detail.startswith(GAP_REPRICE) for event in history) == 6
    for _, body in rig.broker.modifies:
        assert body["orderType"] == "STOP_LIMIT"
        assert body["price"] > 0


async def test_a_reprice_waits_its_interval_and_skips_a_stop_not_triggered(rig: Any) -> None:
    stop = await gapped(rig)
    assert await rig.protective.reprice_gaps() != []
    assert await rig.protective.reprice_gaps() == []  # the interval has not passed
    rig.clock.advance(3600)
    rig.broker.quote(STOCK)  # the market came back above the stop
    assert await rig.protective.reprice_gaps() == []
    assert stop.price == Decimal("92.64")


def with_gap(settings: ObliskConfig, **changes: Any) -> ObliskConfig:
    """``settings`` with other gap repricing values."""
    protective = settings.execution.protective
    gap = protective.gap.model_copy(update=changes)
    execution = settings.execution.model_copy(
        update={"protective": protective.model_copy(update={"gap": gap})}
    )
    return settings.model_copy(update={"execution": execution})


async def test_a_gap_past_the_collar_is_alerted_and_the_stop_rests(rig: Any) -> None:
    settings = with_gap(rig.settings, step_fraction=Decimal("0.05"))
    rig.protective = manager(rig, settings)
    stop = await gapped(rig)
    rig.broker.quote(STOCK, bid="85", ask="95", last="90")  # the bid far below the mid
    assert await rig.protective.reprice_gaps() == []
    (alert,) = [a for a in rig.alerts if a.kind is OrderAlertKind.STOP_GAPPED]
    assert "emergency limit, which is off" in alert.detail
    assert stop.price == Decimal("93.1")  # it rests where it was
    assert stop.order_type == "STOP_LIMIT"


async def test_the_owners_emergency_limit_lets_the_last_step_reach_below_the_bid(
    rig: Any, shipped_settings: ObliskConfig
) -> None:
    emergency = rig.settings.execution.protective.gap.emergency
    settings = with_gap(
        rig.settings, max_steps=1, emergency=emergency.model_copy(update={"enabled": True})
    )
    rig.make_gate(settings=settings)
    rig.protective = manager(rig, settings)
    stop = await gapped(rig)
    assert await rig.protective.reprice_gaps() != []
    assert stop.price == Decimal("85.50")  # 5 % below the bid of 90
    assert stop.order_type == "STOP_LIMIT"


# ── failure branches ──


async def test_an_unfilled_entry_needs_no_stop_and_an_unreadable_book_is_reported(
    rig: Any,
) -> None:
    await entry(rig)
    await rig.staged(side=OrderSide.SELL, quantity="1", limit="99.6")  # an exit not sent yet
    assert (await rig.protective.protect(STOCK)).action == "none"  # nothing filled yet
    rig.broker.unavailable.add("positions")
    outcome = await rig.protective.protect(STOCK)
    assert (outcome.action, outcome.detail.startswith("cannot read")) == ("unread", True)


async def test_a_stop_whose_status_reads_cancelled_covers_nothing_and_is_replaced(
    rig: Any,
) -> None:
    await entry(rig)
    await fill(rig, rig.entry_order)
    await rig.protective.protect(STOCK)
    (stop,) = stops(rig)
    stop.status = "Cancelled"
    stop.hidden_reads = 10  # no longer listed; its status read says cancelled
    rig.clock.advance(rig.settings.execution.reconcile.grace_seconds + 1)
    (outcome,) = await rig.protective.verify()
    assert outcome.action == "placed"
    assert working_sells(rig) == Decimal(10)


async def test_a_stop_without_a_spec_cannot_be_topped_up(rig: Any) -> None:
    from order_rig import terms

    await rig.hold(STOCK, "20")
    await rig.intent(
        terms(side=OrderSide.SELL, quantity="10", stop="95", limit="94"),
        role=IntentRole.PROTECTIVE,
        reason="stop",
        state=IntentState.SUBMITTED,
    )
    (outcome,) = await rig.protective.verify()
    assert (outcome.action, outcome.detail) == ("refused", "no protective spec to place a stop by")
    assert rig.alerts[-1].kind is OrderAlertKind.STOP_MISSING


async def test_a_resize_the_gate_or_the_broker_refuses_is_reported(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections import deque

    from obliskiq.errors import ObliskError

    await entry(rig)
    await fill(rig, rig.entry_order, "4")
    await rig.protective.protect(STOCK)
    await fill(rig, rig.entry_order, "6")
    rig.broker.questions = deque([(["o99999"], "a warning nobody knows")])
    outcome = await rig.protective.protect(STOCK)
    assert (outcome.action, outcome.detail.startswith("declined")) == ("refused", True)
    await rig.protective.protect(STOCK)  # a declined modify leaves no stop: placed afresh

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise ObliskError("the boundary refused")

    monkeypatch.setattr(rig.boundary, "modify", broken)
    rig.broker.hold(STOCK, "12")
    assert (await rig.protective.protect(STOCK)).detail == "the boundary refused"
    rig.broker.unavailable.add("snapshot")  # no quote: the gate refuses the resize
    assert (await rig.protective.protect(STOCK)).action == "refused"


async def test_cuts_take_the_largest_listed_stop_first(rig: Any) -> None:
    await entry(rig)
    await fill(rig, rig.entry_order, "4")
    await rig.protective.protect(STOCK)
    await fill(rig, rig.entry_order, "6")
    rig.broker.listing_delay = 3
    stops(rig)[0].hidden_reads = 3  # the first stop is not listed now: a second one is placed
    assert (await rig.protective.protect(STOCK)).action == "placed"
    rig.broker.hold(STOCK, "7")
    (outcome,) = await rig.protective.verify()
    assert outcome.action == "resized down"  # the unlisted six skipped, the four cut to one
    assert sorted(o.quantity for o in stops(rig)) == [Decimal(1), Decimal(6)]
    for order in rig.broker.live():
        order.hidden_reads = 0
    rig.broker.hold(STOCK, "4")
    await rig.protective.verify()  # the six cut to three; the one left alone
    assert sorted(o.quantity for o in stops(rig)) == [Decimal(1), Decimal(3)]
    for order in rig.broker.live():
        order.hidden_reads = 5
    rig.broker.hold(STOCK, "0")
    (outcome,) = await rig.protective.verify()
    assert outcome.detail == "nothing listed to cut"


async def test_a_cancel_that_fails_while_cutting_is_alerted(rig: Any) -> None:
    await entry(rig)
    await fill(rig, rig.entry_order)
    await rig.protective.protect(STOCK)
    rig.broker.hold(STOCK, "0")
    rig.broker.cancel_failure = "cannot cancel now"
    (outcome,) = await rig.protective.verify()
    assert outcome.action == "resized down"
    assert "the cancel failed" in outcome.detail
    assert "a stop beyond its position" in rig.alerts[-1].detail


async def test_a_stop_cannot_be_placed_without_rules_a_sleeve_a_mode_or_the_broker(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obliskiq.domain.orders import UNMANAGED
    from obliskiq.errors import ObliskError

    await entry(rig)
    await fill(rig, rig.entry_order)
    rig.broker.unavailable.add("rules")
    outcome = await rig.protective.protect(STOCK)
    assert outcome.detail == "the contract's rules give no tick for the stop"
    rig.broker.unavailable.clear()
    await rig.assign(STOCK, UNMANAGED)
    assert (await rig.protective.protect(STOCK)).detail == "the holding is assigned to no sleeve"
    await rig.assign(STOCK, Sleeve.CORE)

    async def unreadable() -> Any:
        raise RuntimeError("the store is gone")

    monkeypatch.setattr(rig.modes, "current", unreadable)
    assert "RuntimeError" in (await rig.protective.protect(STOCK)).detail
    monkeypatch.undo()
    rig.broker.reject_next = "no permission"
    assert (await rig.protective.protect(STOCK)).detail.startswith("rejected")

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise ObliskError("the boundary refused")

    monkeypatch.setattr(rig.boundary, "place", broken)
    outcome = await rig.protective.protect(STOCK)
    assert outcome.detail == "not placed: the boundary refused"
    assert stops(rig) == []


async def test_a_roll_up_without_rules_is_reported(rig: Any) -> None:
    await entry(rig)
    await fill(rig, rig.entry_order)
    await rig.protective.protect(STOCK)
    rig.broker.unavailable.add("rules")
    (failure,) = await rig.protective.roll_up(STOCK, Decimal(97))
    assert "no tick or no working order" in failure


async def test_a_gap_step_needs_a_quote_rules_and_a_limit_above_zero(rig: Any) -> None:
    emergency = rig.settings.execution.protective.gap.emergency
    settings = with_gap(
        rig.settings, max_steps=1, emergency=emergency.model_copy(update={"enabled": True})
    )
    rig.protective = manager(rig, settings)
    stop = await gapped(rig)
    await rig.staged(side=OrderSide.SELL, quantity="1", state=IntentState.SUBMITTED)  # no stop
    rig.broker.unavailable.add("snapshot")
    assert await rig.protective.reprice_gaps() == []
    rig.broker.unavailable = {"rules"}
    assert await rig.protective.reprice_gaps() == []
    rig.broker.unavailable.clear()
    rig.broker.quote(STOCK, bid="0.004", ask="0.006", last="0.005")  # under one tick
    assert await rig.protective.reprice_gaps() == []
    assert stop.price == Decimal("93.1")


async def test_the_emergency_limit_needs_a_bid_and_bounds_the_depth(rig: Any) -> None:
    from obliskiq.domain.orders import ClearanceAction, StopLimitPrice

    emergency = rig.settings.execution.protective.gap.emergency
    settings = with_gap(rig.settings, emergency=emergency.model_copy(update={"enabled": True}))
    rig.make_gate(settings=settings)
    rig.protective = manager(rig, settings)
    stop = await gapped(rig)
    (stored,) = [
        s
        for s in await rig.store(repo.intents_in_states, [IntentState.WORKING])
        if s.intent.role is IntentRole.PROTECTIVE
    ]

    async def modify_to(limit: str) -> Any:
        price = StopLimitPrice(stop=Decimal(95), limit=Decimal(limit))
        return await rig.gate.clear(
            stored.intent,
            action=ClearanceAction.MODIFY,
            order_id=stop.order_id,
            terms=stored.intent.terms.model_copy(update={"price": price}),
        )

    deep = await modify_to("80")  # 5 % below the bid of 90 is 85.5
    assert "below the emergency limit" in deep.reason
    assert isinstance(await modify_to("86"), PretradeClearance)
    rig.broker.quote(STOCK, bid="0", ask="90.2", last="90.1")  # no bid to measure from
    assert "no bid" in (await modify_to("86")).reason
