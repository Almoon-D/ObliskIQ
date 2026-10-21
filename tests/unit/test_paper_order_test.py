"""``obliskiq preflight --paper-order-test`` on the fake broker: whatif, place one order that
cannot fill per instrument, read its status and replies, cancel; a US stock without dollars is
refused by the cash check, and that refusal is the expected outcome.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections import deque
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from order_rig import EUR_ETF, STOCK, whatif_answer

from obliskiq.domain.orders import ExecutionMode
from obliskiq.errors import StoreError
from obliskiq.execution.paper_test import PaperOrderTest, passive_limit
from obliskiq.execution.pricing import tick_for
from obliskiq.execution.runtime import OrderPath
from obliskiq.ibkr.models import ContractRules, IncrementRule
from obliskiq.jurisdiction import load_profile

REPO = Path(__file__).resolve().parents[2]


def path_of(rig: Any) -> OrderPath:
    """The order path the command assembles, over the rig's parts."""
    paper_order = rig.settings.ibkr.preflight.paper_order.model_copy(
        update={"conids": [EUR_ETF, STOCK]}
    )
    preflight = rig.settings.ibkr.preflight.model_copy(update={"paper_order": paper_order})
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


@pytest.fixture
def rig(order_rig: Any) -> Any:
    order_rig.ready()
    broker = order_rig.broker
    broker.contracts = {EUR_ETF: ("EXV1", "IBIS", "EUR"), STOCK: ("STK1", "NYSE", "USD")}

    def priced(order: dict[str, Any]) -> dict[str, Any]:
        currency = "EUR" if order.get("conid") == EUR_ETF else "USD"
        return whatif_answer("95.90", currency)

    broker.whatif_for = priced
    return order_rig


def steps(report: Any) -> dict[str, Any]:
    return {step.name: step for step in report.steps}


async def test_each_instrument_is_placed_out_of_reach_and_cancelled(rig: Any) -> None:
    rig.broker.questions = deque([(["o10153"], "a cap price is mandatory")])
    report = await PaperOrderTest(path_of(rig), now=rig.clock).run()
    found = steps(report)
    assert report.ok, report.render()
    assert found["EXV1 identity"].ok
    assert found["STK1 identity"].ok
    assert found["EXV1 algos"].detail == "Adaptive"
    assert "limit 94.9" in found["EXV1 quote"].detail  # 5 % below the 99.9 bid, on the tick
    assert "o10153" in found["EXV1 place"].detail
    assert found["EXV1 cOID"].ok
    assert "36 characters" in found["EXV1 cOID"].detail
    assert "Cancelled" in found["EXV1 cancel"].detail
    placed = rig.broker.placements
    assert [p["side"] for p in placed] == ["BUY", "BUY"]
    assert all(p["price"] < 99.9 for p in placed)
    assert all(o.status == "Cancelled" for o in rig.broker.orders.values())
    rendered = report.render(("DUTEST1",))
    assert "DUTEST1" not in rendered
    assert "Every step passed." in rendered


async def test_a_stock_in_a_currency_the_account_does_not_hold_is_refused_by_the_cash_check(
    rig: Any,
) -> None:
    del rig.broker.cash["USD"]
    report = await PaperOrderTest(path_of(rig), now=rig.clock).run()
    found = steps(report)
    assert report.ok, report.render()
    assert found["STK1 pre-trade"].detail.startswith("refused by the cash check, as expected")
    assert "currency_not_held" in found["STK1 pre-trade"].detail
    assert "STK1 place" not in found
    assert len(rig.broker.placements) == 1  # the ETF alone


async def test_the_test_needs_the_paper_mode(rig: Any) -> None:
    await rig.modes.downgrade(ExecutionMode.SHADOW, actor="test", reason="test")
    report = await PaperOrderTest(path_of(rig), now=rig.clock).run()
    assert not report.ok
    assert "obliskiq mode set paper" in report.steps[0].detail
    assert rig.broker.placements == []


async def test_an_unreadable_mode_stops_the_test(rig: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def unreadable() -> Any:
        raise StoreError("the mode audit cannot be read")

    monkeypatch.setattr(rig.modes, "current", unreadable)
    report = await PaperOrderTest(path_of(rig), now=rig.clock).run()
    assert not report.ok
    assert report.steps[0].name == "mode"


async def test_a_conid_ibkr_resolves_otherwise_stops_that_instrument(rig: Any) -> None:
    rig.broker.contracts[777] = ("EXV1", "IBIS", "EUR")
    del rig.broker.contracts[EUR_ETF]
    report = await PaperOrderTest(path_of(rig), now=rig.clock).run()
    found = steps(report)
    assert not found["EXV1 identity"].ok
    assert "conid 777" in found["EXV1 identity"].detail
    assert found["STK1 place"].ok  # the other instrument still runs


async def test_a_search_that_finds_nothing_stops_that_instrument(rig: Any) -> None:
    del rig.broker.contracts[EUR_ETF]
    report = await PaperOrderTest(path_of(rig), now=rig.clock).run()
    assert not steps(report)["EXV1 stopped"].ok


async def test_no_tick_rule_leaves_no_passive_limit(rig: Any) -> None:
    rig.broker.rules_answer = {"orderTypes": ["limit"], "sizeIncrement": 1}
    report = await PaperOrderTest(path_of(rig), now=rig.clock).run()
    assert not steps(report)["EXV1 pricing"].ok
    assert rig.broker.placements == []


async def test_an_unexpected_refusal_fails_the_step(rig: Any) -> None:
    await rig.kill.turn_on(actor="test", reason="drill")
    report = await PaperOrderTest(path_of(rig), now=rig.clock).run()
    found = steps(report)
    assert not found["EXV1 pre-trade"].ok
    assert "kill_switch_on" in found["EXV1 pre-trade"].detail
    assert rig.broker.placements == []


async def test_a_placement_the_broker_refuses_fails_the_step(rig: Any) -> None:
    rig.broker.reject_next = "no trading permission"
    report = await PaperOrderTest(path_of(rig), now=rig.clock).run()
    found = steps(report)
    assert not found["EXV1 place"].ok
    assert "rejected" in found["EXV1 place"].detail


async def test_a_whatif_error_is_reported(rig: Any) -> None:
    rig.broker.whatif_for = lambda _order: whatif_answer("95.90", "EUR", error="no permission")
    report = await PaperOrderTest(path_of(rig), now=rig.clock).run()
    assert not steps(report)["EXV1 whatif"].ok


def rules(*edges: tuple[float, float]) -> ContractRules:
    return ContractRules(
        conid=1,
        is_buy=True,
        order_types=("limit",),
        order_types_outside_rth=(),
        cash_quantity_order_types=(),
        tif_types=(),
        algo_eligible=None,
        size_increment=1.0,
        increment_rules=tuple(IncrementRule(lower_edge=e, increment=i) for e, i in edges),
        price_magnifier=None,
        cash_currency=None,
        error=None,
    )


def test_the_tick_is_the_rule_of_the_highest_edge_below_the_price() -> None:
    graded = rules((0.0, 0.001), (1.0, 0.005), (10.0, 0.01))
    assert tick_for(graded, Decimal("0.5")) == Decimal("0.001")
    assert tick_for(graded, Decimal("12")) == Decimal("0.01")
    assert tick_for(rules((5.0, 0.01)), Decimal("1")) is None


def test_the_passive_limit_rounds_down_to_the_tick() -> None:
    assert passive_limit(Decimal("99.9"), Decimal("0.05"), Decimal("0.01")) == Decimal("94.90")
    assert passive_limit(Decimal("0.01"), Decimal("0.5"), Decimal("0.01")) is None
