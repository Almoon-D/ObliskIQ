"""Reconciliation on the fake broker: green when the broker and the store agree; an order inside
the grace period is not drift and one past it is; drift and partial data freeze new risk until a
green run; positions and cash are checked against the bot's own fills.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from decimal import Decimal
from typing import Any

import pytest
from fake_broker import EURUSD
from order_rig import EUR_ETF, STOCK

from obliskiq.capital.flows import CashLedger
from obliskiq.domain.capital import AdjustmentKind, Deposit, Withdrawal
from obliskiq.domain.orders import (
    UNMANAGED,
    IntentState,
    PretradeClearance,
    ReconcileStatus,
    Sleeve,
)
from obliskiq.execution.boundary import OrderAlertKind
from obliskiq.execution.pretrade import PretradeRefusal, Reason
from obliskiq.ibkr.orders import PlacementStatus
from obliskiq.store import capital as capital_repo
from obliskiq.store import orders as repo


@pytest.fixture
def rig(order_rig: Any) -> Any:
    order_rig.ready()
    order_rig.make_execution()
    return order_rig


def kinds(run: Any) -> list[str]:
    return [finding.kind for finding in run.findings]


async def placed(rig: Any, **kwargs: Any) -> Any:
    stored = await rig.staged(**kwargs)
    clearance = await rig.gate.clear(stored.intent)
    assert isinstance(clearance, PretradeClearance), clearance
    result = await rig.boundary.place(stored.intent.intent_id, clearance)
    assert result.status is PlacementStatus.PLACED
    return stored, result.order_id


async def test_a_broker_that_agrees_with_the_store_is_green(rig: Any) -> None:
    await placed(rig)
    run = await rig.reconciler.run()
    assert run.status is ReconcileStatus.GREEN, run.findings
    again = await rig.reconciler.run()
    assert again.status is ReconcileStatus.GREEN, again.findings
    assert (await rig.store(repo.latest_reconciliation)) == again
    assert rig.alerts == []


async def test_an_order_inside_the_grace_period_is_not_drift_and_one_past_it_is(
    rig: Any,
) -> None:
    rig.broker.listing_delay = 50  # taken, but not listed for a long while
    await placed(rig)
    inside = await rig.reconciler.run()
    assert inside.status is ReconcileStatus.GREEN
    rig.clock.advance(rig.settings.execution.reconcile.grace_seconds + 1)
    past = await rig.reconciler.run()
    assert past.status is ReconcileStatus.DRIFT
    assert "order_missing" in kinds(past)
    assert rig.alerts[-1].kind is OrderAlertKind.RECONCILIATION_DRIFT


async def test_drift_freezes_new_risk_until_a_green_run(rig: Any) -> None:
    rig.broker.add_order(side="BUY", conid=EUR_ETF)  # an order the bot did not place
    run = await rig.reconciler.run()
    assert run.status is ReconcileStatus.DRIFT
    assert "foreign_order" in kinds(run)
    buy = await rig.staged()
    refused = await rig.gate.clear(buy.intent)
    assert isinstance(refused, PretradeRefusal)
    assert Reason.RECONCILIATION_FROZEN.value in refused.codes
    for order in rig.broker.live():
        order.status = "Cancelled"
    assert (await rig.reconciler.run()).status is ReconcileStatus.GREEN
    assert isinstance(await rig.gate.clear(buy.intent), PretradeClearance)


@pytest.mark.parametrize("what", ["positions", "ledger", "snapshot"])
async def test_partial_or_missing_broker_data_is_never_green(rig: Any, what: str) -> None:
    if what == "snapshot":
        rig.broker.snapshot = False
    else:
        rig.broker.unavailable.add(what)
    run = await rig.reconciler.run()
    assert run.status is ReconcileStatus.INCOMPLETE
    assert kinds(run) == ["broker_unreadable"]
    assert rig.alerts[-1].kind is OrderAlertKind.RECONCILIATION_DRIFT
    assert "incomplete" in rig.alerts[-1].detail


async def test_a_position_that_moved_without_a_fill_is_drift_and_a_recent_fill_settles(
    rig: Any,
) -> None:
    await rig.hold(STOCK, "10")
    assert (await rig.reconciler.run()).status is ReconcileStatus.GREEN
    rig.broker.hold(STOCK, "7")  # three shares left with no fill the bot knows
    run = await rig.reconciler.run()
    assert "position_mismatch" in kinds(run)
    assert run.status is ReconcileStatus.DRIFT
    rig.broker.hold(STOCK, "10")
    assert (await rig.reconciler.run()).status is ReconcileStatus.GREEN
    # A fill IBKR reports before its position: settling inside the grace period, drift after.
    rig.clock.advance(1)
    _, order_id = await placed(rig, quantity="5")
    rig.broker.stale_positions = True
    rig.broker.fill(order_id)
    settling = await rig.reconciler.run()
    assert settling.status is ReconcileStatus.GREEN
    assert "position_settling" in kinds(settling)
    rig.clock.advance(rig.settings.execution.reconcile.grace_seconds + 1)
    assert "position_mismatch" in kinds(await rig.reconciler.run())


async def test_cash_follows_the_bots_fills_and_a_gap_beyond_tolerance_is_drift(rig: Any) -> None:
    rig.broker.settle_cash = True
    assert (await rig.reconciler.run()).status is ReconcileStatus.GREEN
    rig.clock.advance(1)
    _, order_id = await placed(rig, quantity="10")
    rig.broker.fill(order_id, price="100")
    rig.clock.advance(rig.settings.execution.reconcile.grace_seconds + 1)
    run = await rig.reconciler.run()
    assert run.status is ReconcileStatus.GREEN, run.findings  # 1,001 dollars out, as the fill says
    usd, rate = rig.broker.cash["USD"]
    rig.broker.cash["USD"] = (usd + Decimal(50), rate)  # a dividend nobody announced
    income = await rig.reconciler.run()
    # More cash than expected is unexplained too: new buys wait until it is classified.
    assert income.status is ReconcileStatus.DRIFT
    assert "cash_unexplained" in kinds(income)
    (event,) = await rig.store(capital_repo.cash_events, pending_only=True)
    assert (event.currency, event.amount) == ("USD", Decimal(50))
    assert f"E{event.event_id}" in income.findings[0].detail
    assert (await rig.reconciler.run()).status is ReconcileStatus.DRIFT  # until classified
    ledger = CashLedger(rig.worker, base_currency="EUR", now=rig.clock)
    pending = await ledger.pending_event(event.event_id)
    dividend = ledger.classification(
        pending, AdjustmentKind.DIVIDEND, actor="owner", in_book=False, note=None
    )
    await ledger.classify(pending, dividend, actor="owner")
    rig.clock.advance(1)
    assert (await rig.reconciler.run()).status is ReconcileStatus.GREEN
    assert (await rig.reconciler.run()).findings == ()  # the anchor holds it now
    rig.broker.cash["USD"] = (usd, rate)  # a fee nobody explained
    drift = await rig.reconciler.run()
    assert drift.status is ReconcileStatus.DRIFT
    assert "cash_unexplained" in kinds(drift)
    assert (await rig.reconciler.run()).status is ReconcileStatus.DRIFT  # until accepted
    accepted = await rig.reconciler.accept(actor="owner", reason="IBKR's monthly fee")
    assert accepted.status is ReconcileStatus.GREEN
    assert kinds(accepted) == ["accepted"]
    assert (await rig.reconciler.run()).status is ReconcileStatus.GREEN


async def test_a_conversion_moves_both_currencies_as_expected(rig: Any) -> None:
    rig.broker.quote(EURUSD, bid="1.1649", ask="1.1651", last="1.165", sigma="8%")
    assert (await rig.reconciler.run()).status is ReconcileStatus.GREEN
    rig.clock.advance(1)
    from order_rig import terms

    from obliskiq.domain.orders import IntentRole, OrderSide

    stored = await rig.intent(
        terms(side=OrderSide.SELL, cash="1000", limit="1.162", conversion=True, conid=EURUSD),
        role=IntentRole.CONVERSION,
        reason="funding",
    )
    clearance = await rig.gate.clear(stored.intent)
    assert isinstance(clearance, PretradeClearance), clearance
    result = await rig.boundary.place(stored.intent.intent_id, clearance)
    rig.broker.fill(result.order_id, price="1.1645")
    rig.clock.advance(rig.settings.execution.reconcile.grace_seconds + 1)
    run = await rig.reconciler.run()
    assert run.status is ReconcileStatus.GREEN, run.findings


async def test_a_negative_balance_is_always_drift(rig: Any) -> None:
    rig.broker.cash["USD"] = (Decimal(-5), Decimal("0.86"))
    run = await rig.reconciler.run()
    assert "negative_cash" in kinds(run)
    assert run.status is ReconcileStatus.DRIFT


async def test_a_fill_of_an_instrument_nobody_describes_cannot_be_explained(rig: Any) -> None:
    assert (await rig.reconciler.run()).status is ReconcileStatus.GREEN
    rig.clock.advance(1)
    stray = rig.broker.add_order(side="BUY", conid=999, coid=None)
    rig.broker.fill(stray.order_id)
    rig.clock.advance(rig.settings.execution.reconcile.grace_seconds + 1)
    run = await rig.reconciler.run()
    assert {"cash_unexplained", "foreign_fill"} <= set(
        kinds(run)
    )  # its own fill explains the position
    accepted = await rig.reconciler.accept(actor="owner", reason="my own trade")
    assert accepted.status is ReconcileStatus.GREEN
    assert (await rig.reconciler.run()).status is ReconcileStatus.GREEN


async def test_an_unlisted_order_with_a_final_status_settles_and_accept_needs_the_broker(
    rig: Any,
) -> None:
    stored, order_id = await placed(rig)
    rig.broker.orders[order_id].status = "Cancelled"
    rig.broker.orders[order_id].hidden_reads = 10  # no longer listed
    rig.clock.advance(rig.settings.execution.reconcile.grace_seconds + 1)
    run = await rig.reconciler.run()
    assert run.status is ReconcileStatus.GREEN, run.findings
    state = (await rig.book.get(stored.intent.intent_id)).intent.state
    assert state is IntentState.CANCELLED  # read by its id, and settled
    rig.broker.unavailable.add("ledger")
    refused = await rig.reconciler.accept(actor="owner", reason="test")
    assert refused.status is ReconcileStatus.INCOMPLETE


async def test_holdings_are_registered_to_their_sleeve_or_unmanaged(rig: Any) -> None:
    rig.broker.hold(EUR_ETF, "5", "EUR")  # held before the bot: unmanaged
    stored, order_id = await placed(rig, quantity="3")  # the bot's first fill of STOCK
    rig.broker.fill(order_id)
    await rig.store(repo.upsert_fills, list(await rig.client.trades()))
    run = await rig.reconciler.run()
    assert "holding_registered" in kinds(run)
    owner = await rig.store(repo.holding_assignment, EUR_ETF)
    assert owner.owner == UNMANAGED
    mine = await rig.store(repo.holding_assignment, STOCK)
    assert mine.owner is Sleeve.CORE
    assert stored.intent.sleeve is Sleeve.CORE


async def test_fills_the_store_never_recorded_are_drift(rig: Any) -> None:
    stored, order_id = await placed(rig, quantity="10")
    rig.broker.fill(order_id, "4")
    rig.broker.trades.clear()  # older than the trades IBKR still serves
    rig.clock.advance(rig.settings.execution.reconcile.grace_seconds + 1)
    run = await rig.reconciler.run()
    assert "fills_unreconciled" in kinds(run)
    state = (await rig.book.get(stored.intent.intent_id)).intent.state
    assert state is IntentState.PARTIALLY_FILLED


async def test_shadow_fills_and_flat_rows_are_not_reconciled(rig: Any) -> None:
    from obliskiq.domain.orders import Fill, OrderSide

    rig.broker.hold(EUR_ETF, "0", "EUR")  # a row IBKR still lists at zero
    assert (await rig.reconciler.run()).status is ReconcileStatus.GREEN
    rig.clock.advance(1)
    shadow = Fill(
        execution_id="shadow-1",
        coid=None,
        conid=STOCK,
        side=OrderSide.BUY,
        quantity=Decimal(5),
        price=Decimal(100),
        commission=None,
        executed_at=rig.clock(),
        simulated=True,
    )
    await rig.store(repo.upsert_fills, [shadow])
    rig.clock.advance(1)
    run = await rig.reconciler.run()
    assert run.status is ReconcileStatus.GREEN, run.findings
    assert await rig.store(repo.holding_assignment, EUR_ETF) is None


async def test_an_unlisted_order_whose_status_cannot_be_read_is_drift(rig: Any) -> None:
    _, order_id = await placed(rig)
    rig.broker.orders[order_id].hidden_reads = 10
    rig.broker.status_unavailable = True
    rig.clock.advance(rig.settings.execution.reconcile.grace_seconds + 1)
    run = await rig.reconciler.run()
    assert "order_missing" in kinds(run)


async def test_a_position_that_moved_beside_a_recent_order_is_settling(rig: Any) -> None:
    await rig.hold(STOCK, "10")
    assert (await rig.reconciler.run()).status is ReconcileStatus.GREEN
    rig.clock.advance(rig.settings.execution.reconcile.grace_seconds + 1)
    await placed(rig, quantity="3")  # an order of the instrument moved just now
    rig.broker.hold(STOCK, "13")  # its fill not reported yet
    run = await rig.reconciler.run()
    assert run.status is ReconcileStatus.GREEN, run.findings
    assert "position_settling" in kinds(run)


# ── the owner's recorded flows explain cash (Session 7) ──


async def _flow(rig: Any, flow: Any) -> Any:
    return await rig.store(capital_repo.record_flow, flow)


async def test_an_announced_deposit_explains_the_cash_it_brings_once(rig: Any) -> None:
    assert (await rig.reconciler.run()).status is ReconcileStatus.GREEN
    deposit = await _flow(
        rig,
        Deposit(
            amount=Decimal(1000),
            currency="EUR",
            on=rig.clock().date(),
            recorded_at=rig.clock(),
            recorded_by="owner",
        ),
    )
    assert (await rig.reconciler.run()).status is ReconcileStatus.GREEN  # not arrived: no gap
    eur, rate = rig.broker.cash["EUR"]
    rig.broker.cash["EUR"] = (eur + Decimal("1000.40"), rate)  # within the tolerance
    rig.clock.advance(1)
    arrived = await rig.reconciler.run()
    assert arrived.status is ReconcileStatus.GREEN
    assert "cash_flow_arrived" in kinds(arrived)
    assert "deposit F1" in arrived.findings[0].detail
    matched = await rig.store(capital_repo.get_flow, deposit.flow_id)
    assert matched.matched_at is not None
    rig.clock.advance(1)
    assert (await rig.reconciler.run()).findings == ()  # counted once, in the anchor now


async def test_a_recorded_withdrawal_explains_the_cash_that_left(rig: Any) -> None:
    assert (await rig.reconciler.run()).status is ReconcileStatus.GREEN
    await _flow(
        rig,
        Withdrawal(
            amount=Decimal(300),
            currency="USD",
            on=rig.clock().date(),
            recorded_at=rig.clock(),
            recorded_by="owner",
        ),
    )
    usd, rate = rig.broker.cash["USD"]
    rig.broker.cash["USD"] = (usd - Decimal(300), rate)
    rig.clock.advance(1)
    run = await rig.reconciler.run()
    assert run.status is ReconcileStatus.GREEN
    assert "cash_flow_arrived" in kinds(run)


async def test_a_deposit_of_another_amount_does_not_explain_the_cash(rig: Any) -> None:
    assert (await rig.reconciler.run()).status is ReconcileStatus.GREEN
    await _flow(
        rig,
        Deposit(
            amount=Decimal(500),
            currency="EUR",
            on=rig.clock().date(),
            recorded_at=rig.clock(),
            recorded_by="owner",
        ),
    )
    eur, rate = rig.broker.cash["EUR"]
    rig.broker.cash["EUR"] = (eur + Decimal(800), rate)
    rig.clock.advance(1)
    run = await rig.reconciler.run()
    assert run.status is ReconcileStatus.DRIFT
    assert "cash_unexplained" in kinds(run)
    (event,) = await rig.store(capital_repo.cash_events, pending_only=True)
    assert (event.currency, event.amount) == ("EUR", Decimal(800))
