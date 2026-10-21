"""``obliskiq preflight --execution-test`` on the fake broker: a conversion with its whatif and
both balances, a buy that fills at the touch, its protective stop listed, IBKR's reporting lag;
then, in a new process, the stop still listed and the position sold with the stop cancelled.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fake_broker import EURUSD
from order_rig import STOCK

from obliskiq.domain.orders import ExecutionMode
from obliskiq.execution.live_test import ExecutionTest
from obliskiq.execution.runtime import OrderPath
from obliskiq.jurisdiction import load_profile

REPO = Path(__file__).resolve().parents[2]


def path_of(rig: Any) -> OrderPath:
    """The order path the command assembles, over the rig's parts, testing STOCK."""
    execution_test = rig.settings.ibkr.preflight.execution_test.model_copy(
        update={"conid": STOCK, "fill_timeout_seconds": 30}
    )
    preflight = rig.settings.ibkr.preflight.model_copy(update={"execution_test": execution_test})
    ibkr = rig.settings.ibkr.model_copy(update={"preflight": preflight})
    return OrderPath(
        settings=rig.settings.model_copy(update={"ibkr": ibkr}),
        client=rig.ibkr,
        orders=rig.client,
        worker=rig.worker,
        book=rig.book,
        modes=rig.modes,
        kill=rig.kill,
        boundary=rig.boundary,
        gate=rig.gate,
        coordinator=rig.coordinator,
        reader=rig.reader,
        directory=rig.directory,
        calendars=rig.calendars,
        approvals=rig.approvals,
        flattener=rig.flattener,
        pricer=rig.pricer,
        currency=rig.currency,
        protective=rig.protective,
        reconciler=rig.reconciler,
        executor=rig.executor,
        data_dir=rig.data_dir,
        profile=load_profile(REPO / "config", "es"),
    )


def filling(rig: Any, *, sec_type: str, price: str) -> Any:
    """A wait during which the broker fills the working orders of ``sec_type``."""

    async def sleep(seconds: float) -> None:
        rig.clock.advance(seconds)
        for order in rig.broker.live():
            if order.sec_type == sec_type and order.order_type == "LMT":
                rig.broker.fill(order.order_id, price=price)

    return sleep


@pytest.fixture
def rig(order_rig: Any) -> Any:
    order_rig.ready()
    order_rig.broker.contracts = {STOCK: ("STK1", "NYSE", "USD")}
    order_rig.broker.quote(EURUSD, bid="1.1649", ask="1.1651", last="1.165", sigma="8%")
    order_rig.make_execution(sleep=filling(order_rig, sec_type="CASH", price="1.1645"))
    return order_rig


def steps(report: Any) -> dict[str, Any]:
    return {step.name: step for step in report.steps}


def make_tester(rig: Any, *, fills: bool = True) -> ExecutionTest:
    sleep = (
        filling(rig, sec_type="STK", price="100.1")
        if fills
        else filling(rig, sec_type="", price="1")
    )
    return ExecutionTest(path_of(rig), now=rig.clock, sleep=sleep)


async def test_phase_one_converts_buys_protects_and_measures_the_lag(rig: Any) -> None:
    report = await make_tester(rig).run()
    found = steps(report)
    assert report.ok, report.render()
    assert "SELL conid 12087792 fxQty 100" in found["(a) conversion whatif"].detail
    assert "EUR -100.00" in found["(a) both balances"].detail
    assert "USD +116.45" in found["(a) both balances"].detail
    assert "not probed" in found["(c) attached orders"].detail
    assert "placed 1.0 at 100.1, stop 90.09" in found["(b) buy"].detail
    assert "Filled" in found["(b) fill"].detail
    lag = found["(d) reporting lag (b)"].detail
    assert "live orders after" in lag
    assert "trades after" in lag
    assert "positions after" in lag
    assert found["(b) protective stop"].detail.startswith("placed")
    assert "STOP_LIMIT" in found["(b) stop at the broker"].detail
    assert "GTC" in found["(b) stop at the broker"].detail
    assert "--finish" in found["phase one done"].detail
    text = report.render(("DUTEST1",))
    assert "DUTEST1" not in text


async def test_phase_two_finds_the_stop_after_a_restart_and_sells(rig: Any) -> None:
    assert (await make_tester(rig).run()).ok
    rig.make_execution()  # a new process over the same store and broker
    report = await make_tester(rig).finish()
    found = steps(report)
    assert report.ok, report.render()
    assert found["(b) the stop after a restart"].detail.startswith("1 of 1")
    assert "1 stop(s) cancelled" in found["(b) sale"].detail
    assert "Filled" in found["(b) sale fill"].detail
    assert rig.broker.positions[STOCK][0] == Decimal(0)
    assert found["(b) nothing left working"].ok


async def test_the_test_needs_paper_mode(rig: Any) -> None:
    await rig.modes.downgrade(ExecutionMode.SHADOW, actor="test", reason="test")
    report = await make_tester(rig).run()
    assert not report.ok
    assert "shadow" in steps(report)["mode"].detail
    assert not (await make_tester(rig).finish()).ok
    assert rig.broker.placements == []


async def test_a_refused_conversion_stops_the_test_before_the_buy(rig: Any) -> None:
    await rig.kill.turn_on(actor="owner", reason="drill")
    report = await make_tester(rig).run()
    found = steps(report)
    assert not found["(a) conversion"].ok
    assert "kill_switch_on" in found["(a) conversion"].detail
    assert "(b) buy" not in found
    assert rig.broker.placements == []


async def test_an_identity_mismatch_stops_the_test(rig: Any) -> None:
    rig.broker.contracts = {999: ("STK1", "NYSE", "USD")}
    report = await make_tester(rig).run()
    assert not steps(report)["STK1 identity"].ok
    assert rig.broker.whatifs == []


async def test_a_buy_that_does_not_fill_is_reported(rig: Any) -> None:
    report = await make_tester(rig, fills=False).run()
    found = steps(report)
    assert not found["(b) fill"].ok
    assert found["(d) reporting lag (b)"].detail == "nothing seen"
    assert "(b) protective stop" not in found


async def test_phase_two_without_a_working_stop_stops(rig: Any) -> None:
    report = await make_tester(rig).finish()
    assert not report.ok
    assert steps(report)["(b) the stop after a restart"].detail.startswith("0 of 0")


async def test_a_buy_the_gate_refuses_is_reported(rig: Any) -> None:
    rig.broker.whatif_for = lambda order: (
        {"error": "no trading permission", "amount": {}} if order.get("conid") != EURUSD else {}
    )
    report = await make_tester(rig).run()
    found = steps(report)
    assert not found["(b) buy"].ok
    assert "refused" in found["(b) buy"].detail


async def test_a_stopped_test_says_why(rig: Any) -> None:
    rig.broker.unavailable.add("snapshot")
    report = await make_tester(rig).run()
    assert not report.ok
    assert any(not step.ok for step in report.steps)


# ── failure branches ──


async def test_an_unreadable_mode_or_cash_stops_the_test(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obliskiq.errors import ObliskError

    rig.broker.unavailable.add("ledger")
    report = await make_tester(rig).run()
    assert not steps(report)["stopped"].ok

    async def unreadable() -> Any:
        raise ObliskError("the store is gone")

    monkeypatch.setattr(rig.modes, "current", unreadable)
    report = await make_tester(rig).run()
    assert "cannot be read" in steps(report)["mode"].detail


@pytest.mark.parametrize("missing", ["quote", "tick", "collar"])
async def test_a_buy_needs_a_quote_a_tick_and_a_limit(rig: Any, missing: str) -> None:
    if missing == "quote":
        del rig.broker.quotes[STOCK]
    elif missing == "tick":
        rig.broker.rules_by_conid[STOCK] = {"orderTypes": ["limit"], "incrementRules": []}
    else:
        rig.broker.quote(STOCK, availability="DP", sigma=None)  # delayed, no volatility
    report = await make_tester(rig).run()
    found = steps(report)
    assert found["(a) conversion"].ok
    assert not found["(b) buy"].ok
    assert rig.broker.placements[-1].get("isCcyConv")  # the buy never reached the broker


async def test_a_rejected_buy_is_reported(rig: Any) -> None:
    async def reject_the_buy(seconds: float) -> None:
        await filling(rig, sec_type="CASH", price="1.1645")(seconds)
        rig.broker.reject_next = "no trading permission"

    rig.make_execution(sleep=reject_the_buy)  # the conversion fills; the buy is rejected
    report = await make_tester(rig).run()
    found = steps(report)
    assert not found["(b) buy"].ok
    assert found["(b) buy"].detail.startswith("rejected")


async def test_the_reporting_lag_is_measured_read_by_read(rig: Any) -> None:
    calls = 0

    async def lagging(seconds: float) -> None:
        nonlocal calls
        calls += 1
        rig.clock.advance(seconds)
        for order in rig.broker.live():
            if order.sec_type == "STK" and order.order_type == "LMT":
                rig.broker.stale_positions = True  # the position lags the fill
                rig.broker.fill(order.order_id, price="100.1")
                rig.broker.stale_positions = False
                pending.extend(rig.broker.trades)
                rig.broker.trades.clear()  # and so do the trades
                return
        if pending and calls > 1:
            rig.broker.trades.extend(pending)
            pending.clear()
            return
        if not pending and rig.broker.positions.get(STOCK, (Decimal(0), ""))[0] == 0:
            rig.broker.hold(STOCK, "1")

    pending: list[Any] = []
    report = await ExecutionTest(path_of(rig), now=rig.clock, sleep=lagging).run()
    lag = steps(report)["(d) reporting lag (b)"].detail
    assert "trades after 6 s" in lag
    assert "positions after 12 s" in lag


async def test_phase_two_stops_without_a_quote_a_position_or_a_sale(rig: Any) -> None:
    assert (await make_tester(rig).run()).ok
    rig.make_execution()
    del rig.broker.quotes[STOCK]
    assert steps(await make_tester(rig).finish())["(b) sale"].detail == "no quote"
    rig.broker.quote(STOCK)
    rig.broker.hold(STOCK, "0")
    assert "nothing to sell" in steps(await make_tester(rig).finish())["(b) sale"].detail
    rig.broker.hold(STOCK, "1")
    rig.broker.reject_next = "no trading permission"
    assert not steps(await make_tester(rig).finish())["(b) sale"].ok
    rig.broker.unavailable.add("positions")
    assert not steps(await make_tester(rig).finish())["stopped"].ok
