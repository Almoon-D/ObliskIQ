"""The currency manager on the fake broker: buys funded in their currency by one limit-capped
conversion per currency, raised to the minimum where there is room, waiting on its fill; a
conversion that fails leaves the trades unplaced; foreign cash beyond the buffer brought home.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import pytest
from fake_broker import EURUSD
from order_rig import EUR_ETF, STOCK, whatif_answer

from obliskiq.config import ObliskConfig
from obliskiq.domain.orders import ExecutionMode, IntentRole, IntentState, OrderSide
from obliskiq.execution.boundary import OrderAlertKind
from obliskiq.execution.currency import CurrencyManager
from obliskiq.execution.pretrade import PretradeClearance, PretradeRefusal, Reason
from obliskiq.store import orders as repo

type Sleep = Callable[[float], Awaitable[None]]


def filling(rig: Any, rate: str = "1.1645") -> Sleep:
    """A wait during which the broker fills every working conversion at ``rate``."""

    async def sleep(seconds: float) -> None:
        rig.clock.advance(seconds)
        for order in rig.broker.live():
            if order.sec_type == "CASH":
                rig.broker.fill(order.order_id, price=rate)

    return sleep


def idle(rig: Any) -> Sleep:
    async def sleep(seconds: float) -> None:
        rig.clock.advance(seconds)

    return sleep


def manager(rig: Any, *, sleep: Sleep | None = None, settings: ObliskConfig | None = None) -> Any:
    return CurrencyManager(
        settings if settings is not None else rig.settings,
        gate=rig.gate,
        boundary=rig.boundary,
        book=rig.book,
        worker=rig.worker,
        reader=rig.reader,
        directory=rig.directory,
        rules=rig.ibkr.contracts,
        modes=rig.modes,
        alert=rig.alerts.append,
        now=rig.clock,
        sleep=sleep if sleep is not None else filling(rig),
    )


@pytest.fixture
def rig(order_rig: Any) -> Any:
    """Ten thousand euros and a hundred dollars; EUR.USD at 1.165."""
    order_rig.ready()
    order_rig.broker.cash = {"EUR": (Decimal(10000), None), "USD": (Decimal(100), Decimal("0.86"))}
    order_rig.broker.quote(EURUSD, bid="1.1649", ask="1.1651", last="1.165", sigma="8%")
    return order_rig


def conversions(rig: Any) -> list[dict[str, Any]]:
    return [body for body in rig.broker.placements if body.get("isCcyConv")]


async def test_two_buys_in_dollars_are_funded_by_one_conversion(rig: Any) -> None:
    first = await rig.staged(quantity="10")  # 1,000 dollars at the limit
    second = await rig.staged(quantity="10")
    report = await manager(rig).fund([first.intent, second.intent])
    assert report.ready == {first.intent.intent_id, second.intent.intent_id}
    assert report.waiting == {}
    (body,) = conversions(rig)
    # 2,000 dollars and a buffer of 100 euros (116.28 dollars) less the 100 held: 2,016.28 dollars,
    # bought with euros at no less than 1.16265 (0.2 % under 1.165, down to the half pip).
    assert (body["conid"], body["side"], body["orderType"]) == (EURUSD, "SELL", "LMT")
    assert body["price"] == pytest.approx(1.16265)
    assert body["fxQty"] == 1735
    assert "quantity" not in body
    (outcome,) = report.conversions
    assert outcome.status == "filled"
    usd, _ = rig.broker.cash["USD"]
    assert usd > Decimal(2000) + Decimal(100) / Decimal("0.86")  # both buys and the buffer
    stored = await rig.book.get(outcome.intent_id)
    assert stored.intent.role is IntentRole.CONVERSION
    assert stored.intent.state is IntentState.FILLED
    fills = await rig.store(repo.fills_for_intent, outcome.intent_id)
    assert [fill.quantity for fill in fills] == [Decimal(1735)]  # recorded for the cash check
    # The trades it funded now clear.
    rig.broker.whatif_answer = whatif_answer("1001.00", "USD")
    assert isinstance(await rig.gate.clear(first.intent), PretradeClearance)


async def test_a_small_shortfall_is_raised_to_the_minimum_conversion(rig: Any) -> None:
    buy = await rig.staged(quantity="2")  # 200 dollars
    report = await manager(rig).fund([buy.intent])
    assert report.ready == {buy.intent.intent_id}
    (body,) = conversions(rig)
    assert body["fxQty"] == 1000  # the minimum conversion, 1,000 euros


async def test_the_minimum_gives_way_to_the_cash_the_base_currency_has(rig: Any) -> None:
    rig.broker.cash["EUR"] = (Decimal(600), None)
    buy = await rig.staged(quantity="2")
    report = await manager(rig).fund([buy.intent])
    assert report.ready == {buy.intent.intent_id}
    (body,) = conversions(rig)
    assert body["fxQty"] == 575  # what the euros allow above the floor of 25


async def test_a_currency_already_funded_converts_nothing(rig: Any) -> None:
    rig.broker.cash["USD"] = (Decimal(20000), Decimal("0.86"))
    buy = await rig.staged()
    euro = await rig.staged(EUR_ETF)  # base-currency buys need no funding
    report = await manager(rig).fund([buy.intent, euro.intent])
    assert report.ready == {buy.intent.intent_id, euro.intent.intent_id}
    assert conversions(rig) == []


async def test_a_conversion_that_fails_leaves_the_trade_unplaced_and_alerts(rig: Any) -> None:
    buy = await rig.staged()
    rig.broker.reject_next = "Order rejected: no currency permission"
    report = await manager(rig).fund([buy.intent])
    assert report.ready == set()
    assert "failed" in report.waiting[buy.intent.intent_id]
    (alert,) = rig.alerts
    assert alert.kind is OrderAlertKind.CONVERSION_FAILED
    assert "no currency permission" in alert.detail
    placed_buys = [b for b in rig.broker.placements if not b.get("isCcyConv")]
    assert placed_buys == []
    # Refused by the gate (the kill switch is on): no conversion is sent at all.
    await rig.kill.turn_on(actor="owner", reason="drill")
    report = await manager(rig).fund([buy.intent])
    assert "failed" in report.waiting[buy.intent.intent_id]
    assert "kill_switch_on" in rig.alerts[-1].detail
    rejected = await rig.store(repo.intents_in_states, [IntentState.REJECTED])
    assert {s.intent.role for s in rejected} == {IntentRole.CONVERSION}


async def test_a_conversion_cancelled_at_the_broker_leaves_the_trade_waiting(rig: Any) -> None:
    buy = await rig.staged()

    async def cancelled(seconds: float) -> None:
        rig.clock.advance(seconds)
        for order in rig.broker.live():
            order.status = "Cancelled"

    report = await manager(rig, sleep=cancelled).fund([buy.intent])
    assert "failed" in report.waiting[buy.intent.intent_id]
    assert "ended Cancelled" in rig.alerts[-1].detail


async def test_a_conversion_not_filled_in_time_keeps_the_trades_waiting_on_it(rig: Any) -> None:
    buy = await rig.staged()
    slow = manager(rig, sleep=idle(rig))
    report = await slow.fund([buy.intent])
    assert report.waiting[buy.intent.intent_id] == "the conversion into USD is working"
    again = await slow.fund([buy.intent])
    assert again.waiting[buy.intent.intent_id] == "a conversion into USD is still working"
    assert len(conversions(rig)) == 1  # never a second conversion beside a working one


async def test_a_buy_that_would_leave_dollars_negative_is_refused(rig: Any) -> None:
    buy = await rig.staged()  # 1,001 dollars against the 100 held
    answer = await rig.gate.clear(buy.intent)
    assert isinstance(answer, PretradeRefusal)
    assert Reason.MARGIN_USE.value in answer.codes
    assert Reason.INSUFFICIENT_CASH.value in answer.codes


async def test_a_conversion_the_euros_cannot_cover_is_refused_and_the_trade_waits(
    rig: Any,
) -> None:
    rig.broker.cash["EUR"] = (Decimal(500), None)
    buy = await rig.staged(quantity="50")  # 5,000 dollars
    report = await manager(rig).fund([buy.intent])
    assert "failed" in report.waiting[buy.intent.intent_id]
    assert "margin_use" in rig.alerts[-1].detail


async def test_shadow_mode_converts_nothing(rig: Any) -> None:
    buy = await rig.intent(mode=ExecutionMode.SHADOW)
    report = await manager(rig).fund([buy.intent])
    assert "shadow" in report.waiting[buy.intent.intent_id]
    assert conversions(rig) == []


async def test_without_a_pair_a_quote_or_the_cash_nothing_is_converted(rig: Any) -> None:
    buy = await rig.staged()
    currency = rig.settings.execution.currency
    execution = rig.settings.execution.model_copy(
        update={"currency": currency.model_copy(update={"pairs": []})}
    )
    no_pairs = rig.settings.model_copy(update={"execution": execution})
    report = await manager(rig, settings=no_pairs).fund([buy.intent])
    assert "no currency pair" in report.waiting[buy.intent.intent_id]
    del rig.broker.quotes[EURUSD]
    report = await manager(rig).fund([buy.intent])
    assert "no quote" in report.waiting[buy.intent.intent_id]
    rig.broker.unavailable.add("ledger")
    report = await manager(rig).fund([buy.intent])
    assert "cannot be read" in report.waiting[buy.intent.intent_id]
    assert conversions(rig) == []


async def test_dollars_beyond_the_buffer_are_brought_home(rig: Any) -> None:
    rig.broker.cash["USD"] = (Decimal(5000), Decimal("0.86"))
    (outcome,) = await manager(rig).repatriate()
    assert outcome.status == "working"
    (body,) = conversions(rig)
    # 5,000 dollars less the 116.28 the buffer keeps, bought into euros at no more than 1.16735.
    assert (body["side"], body["fxQty"]) == ("BUY", 4883)
    assert body["price"] == pytest.approx(1.16735)


async def test_nothing_comes_home_below_the_threshold_or_while_a_buy_needs_it(rig: Any) -> None:
    rig.broker.cash["USD"] = (Decimal(1000), Decimal("0.86"))  # 760 euros beyond the buffer
    assert await manager(rig).repatriate() == []
    rig.broker.cash["USD"] = (Decimal(5000), Decimal("0.86"))
    await rig.staged(quantity="1")  # a dollar buy still pending
    assert await manager(rig).repatriate() == []
    assert conversions(rig) == []


async def test_repatriation_reads_fail_quietly(rig: Any) -> None:
    rig.broker.cash["USD"] = (Decimal(5000), Decimal("0.86"))
    del rig.broker.quotes[EURUSD]
    assert await manager(rig).repatriate() == []
    rig.broker.unavailable.add("ledger")
    assert await manager(rig).repatriate() == []
    assert conversions(rig) == []


async def test_repatriation_waits_in_shadow(rig: Any) -> None:
    rig.broker.cash["USD"] = (Decimal(5000), Decimal("0.86"))
    await rig.modes.downgrade(ExecutionMode.SHADOW, actor="test", reason="test")
    assert await manager(rig).repatriate() == []


async def test_a_sell_or_a_conversion_needs_no_funding(rig: Any) -> None:
    await rig.hold(STOCK, "10")
    sell = await rig.staged(side=OrderSide.SELL, limit="99.9")
    report = await manager(rig).fund([sell.intent])
    assert report.ready == {sell.intent.intent_id}


# ── failure branches ──


def test_a_legs_amounts_follow_its_side(shipped_settings: ObliskConfig) -> None:
    from datetime import UTC, datetime

    from obliskiq.domain.orders import Fill
    from obliskiq.execution.fx import ConversionLeg, conversion_leg, leg_for

    (pair,) = shipped_settings.execution.currency.pairs
    home = leg_for(pair, source="USD", target="EUR")
    away = leg_for(pair, source="EUR", target="USD")
    assert (home, away) == (ConversionLeg(pair, OrderSide.BUY), ConversionLeg(pair, OrderSide.SELL))
    assert leg_for(pair, source="EUR", target="GBP") is None
    rate = Decimal("1.25")
    assert (home.received(Decimal(125), rate), home.needed(Decimal(100), rate)) == (100, 125)
    assert (away.received(Decimal(100), rate), away.needed(Decimal(125), rate)) == (125, 100)
    fill = Fill(
        execution_id="e1",
        coid=None,
        conid=pair.conid,
        side=OrderSide.BUY,
        quantity=Decimal(100),
        price=rate,
        commission=None,
        executed_at=datetime(2026, 10, 20, tzinfo=UTC),
    )
    assert (home.spent([fill]), away.spent([fill])) == (125, 100)
    from order_rig import terms

    assert conversion_leg([pair], terms(conid=999, cash="10", conversion=True)) is None


async def test_without_a_ledger_rate_the_pairs_quote_values_the_currency(rig: Any) -> None:
    rig.broker.cash["USD"] = (Decimal(100), None)
    buy = await rig.staged()
    report = await manager(rig).fund([buy.intent])
    assert report.ready == {buy.intent.intent_id}
    assert len(conversions(rig)) == 1
    del rig.broker.quotes[EURUSD]
    report = await manager(rig).fund([buy.intent])
    assert report.waiting[buy.intent.intent_id] == "no rate for USD: no quote for EUR.USD"
    rig.broker.unavailable.add("snapshot")
    report = await manager(rig).fund([buy.intent])
    assert report.waiting[buy.intent.intent_id] == "no rate for USD: no quote for EUR.USD"


async def test_a_buy_sized_in_cash_is_funded_by_its_cash(rig: Any) -> None:
    from order_rig import terms

    buy = await rig.intent(terms(cash="3000"))
    report = await manager(rig).fund([buy.intent])
    assert report.ready == {buy.intent.intent_id}
    (body,) = conversions(rig)
    assert body["fxQty"] == 2595  # (3,000 + 116.28 buffer - 100 held) / 1.16265


async def test_the_open_orders_and_conversions_of_a_currency_are_counted_once(rig: Any) -> None:
    from order_rig import terms

    rig.broker.cash["USD"] = (Decimal(5000), Decimal("0.86"))
    (home,) = await manager(rig).repatriate()  # 4,883 dollars on their way home, not filled
    assert home.status == "working"
    await rig.intent(
        terms(conid=999, cash="10", conversion=True),  # a pair no longer configured
        role=IntentRole.CONVERSION,
        reason="old",
        state=IntentState.SUBMITTED,
    )
    await rig.staged(EUR_ETF, state=IntentState.SUBMITTED)  # a euro buy: another currency
    await rig.staged(quantity="5", state=IntentState.SUBMITTED)  # 500 dollars committed
    await rig.intent(terms(cash="1000"), state=IntentState.SUBMITTED)  # and 1,000 more
    buy = await rig.staged(quantity="2", state=IntentState.SUBMITTED)  # already sent: its own
    report = await manager(rig).fund([buy.intent])
    assert report.ready == {buy.intent.intent_id}
    (body,) = [b for b in conversions(rig) if b["side"] == "SELL"]
    # (200 + 116.28 buffer - (5,000 held - 4,883 - 500 - 1,000 committed)) / 1.16265
    assert body["fxQty"] == 1462


async def test_a_conversion_needs_the_rules_and_a_nav_to_reach_the_minimum(rig: Any) -> None:
    buy = await rig.staged(quantity="2")
    rig.broker.unavailable.add("rules")
    report = await manager(rig).fund([buy.intent])
    assert report.waiting[buy.intent.intent_id].startswith("no limit for EUR.USD")
    rig.broker.unavailable.clear()
    rig.broker.nav = None
    report = await manager(rig).fund([buy.intent])
    assert report.conversions[0].amount == 187  # no room is known: the shortfall alone


async def test_a_pending_conversion_or_missing_rules_or_mode_stop_repatriation(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from order_rig import terms

    rig.broker.cash["USD"] = (Decimal(5000), Decimal("0.86"))
    pending = await rig.intent(
        terms(side=OrderSide.SELL, cash="1000", limit="1.162", conversion=True, conid=EURUSD),
        role=IntentRole.CONVERSION,
        reason="funding",
    )
    assert await manager(rig).repatriate() == []
    await rig.book.advance(pending.intent.intent_id, IntentState.CANCELLED, "test")
    rig.broker.unavailable.add("rules")
    assert await manager(rig).repatriate() == []
    rig.broker.unavailable.clear()

    async def unreadable() -> Any:
        raise RuntimeError("the store is gone")

    monkeypatch.setattr(rig.modes, "current", unreadable)
    assert await manager(rig).repatriate() == []
    assert conversions(rig) == []


async def test_a_direct_conversion_needs_a_pair_a_quote_and_rules(rig: Any) -> None:
    currencies = manager(rig)
    terms = await currencies.conversion_terms("EUR", "USD", Decimal(10))
    assert not isinstance(terms, str)
    assert (terms.side, terms.cash_quantity, terms.conversion) == (OrderSide.SELL, 10, True)
    assert await currencies.conversion_terms("USD", "GBP", Decimal(10)) == (
        "no currency pair converts USD into GBP"
    )
    outcome = await currencies.convert(
        "USD", "GBP", Decimal(10), mode=ExecutionMode.PAPER, reason="test"
    )
    assert outcome.status == "failed"
    assert rig.alerts[-1].kind is OrderAlertKind.CONVERSION_FAILED
    rig.broker.unavailable.add("rules")
    assert await currencies.conversion_terms("EUR", "USD", Decimal(10)) == (
        "the rules of EUR.USD give no tick"
    )
    del rig.broker.quotes[EURUSD]
    assert await currencies.conversion_terms("EUR", "USD", Decimal(10)) == "no quote for EUR.USD"


async def test_a_conversion_whose_status_cannot_be_read_stays_working(rig: Any) -> None:
    rig.broker.status_unavailable = True
    outcome = await manager(rig, sleep=idle(rig)).convert(
        "EUR", "USD", Decimal(1000), mode=ExecutionMode.PAPER, reason="test"
    )
    assert outcome.status == "working"


async def test_a_filled_conversion_whose_trades_cannot_be_read_is_filled(rig: Any) -> None:
    async def fills_then_trades_fail(seconds: float) -> None:
        await filling(rig)(seconds)
        rig.broker.unavailable.add("trades")

    outcome = await manager(rig, sleep=fills_then_trades_fail).convert(
        "EUR", "USD", Decimal(1000), mode=ExecutionMode.PAPER, reason="test"
    )
    assert outcome.status == "filled"  # the order's status says so; the trades come later


async def test_a_conversion_the_boundary_refuses_fails(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obliskiq.errors import ObliskError

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise ObliskError("the boundary refused")

    monkeypatch.setattr(rig.boundary, "place", broken)
    outcome = await manager(rig).convert(
        "EUR", "USD", Decimal(1000), mode=ExecutionMode.PAPER, reason="test"
    )
    assert (outcome.status, outcome.detail) == ("failed", "not placed: the boundary refused")


def test_a_conversions_preview_is_the_plain_order_on_its_pair(
    shipped_settings: ObliskConfig,
) -> None:
    from order_rig import terms

    from obliskiq.execution.fx import ConversionLeg, preview_terms

    (pair,) = shipped_settings.execution.currency.pairs
    sell = terms(conid=pair.conid, side=OrderSide.SELL, cash="1000", limit="1.25", conversion=True)
    plain = preview_terms(ConversionLeg(pair, OrderSide.SELL), sell)
    assert (plain.conversion, plain.cash_quantity, plain.quantity) == (False, None, Decimal(1000))
    assert (plain.side, plain.conid, plain.price) == (sell.side, sell.conid, sell.price)
    buy = terms(conid=pair.conid, side=OrderSide.BUY, cash="1250", limit="1.25", conversion=True)
    assert preview_terms(ConversionLeg(pair, OrderSide.BUY), buy).quantity == Decimal(1000)
    tiny = terms(conid=pair.conid, side=OrderSide.BUY, cash="0.5", limit="1.25", conversion=True)
    with pytest.raises(ValueError, match="less than one"):
        preview_terms(ConversionLeg(pair, OrderSide.BUY), tiny)
    with pytest.raises(ValueError, match="only a conversion"):
        preview_terms(ConversionLeg(pair, OrderSide.SELL), terms(quantity="1"))
