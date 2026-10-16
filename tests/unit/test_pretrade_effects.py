"""Class by effect, never by role (I3), with the kill switch ON.

An order that reduces risk — a sell of a managed long position, within it — waives exactly the
kill switch, the notional caps, the order count, buy-side eligibility, the cash and no-margin
check and approval. Every other check applies; every buy adds risk, whatever its role.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from decimal import Decimal
from typing import Any

import pytest
from order_rig import OPTION, STOCK, US_ETF, with_caps

from obliskiq.domain.orders import IntentRole, OrderSide, PretradeClearance, RiskEffect
from obliskiq.execution.boundary import OrderAlertKind
from obliskiq.execution.pretrade import PretradeRefusal
from obliskiq.ibkr.orders import PlacementStatus


@pytest.fixture
async def rig(order_rig: Any) -> Any:
    """Ten shares held and the kill switch on."""
    order_rig.ready()
    await order_rig.hold(STOCK, "10")
    await order_rig.kill.turn_on(actor="owner", reason="drill")
    return order_rig


async def answer(rig: Any, stored: Any) -> PretradeClearance | PretradeRefusal:
    result: PretradeClearance | PretradeRefusal = await rig.gate.clear(stored.intent)
    return result


async def test_any_buy_is_refused_whatever_its_role(rig: Any) -> None:
    for role, reason in ((IntentRole.ENTRY, "entry"), (IntentRole.EXIT, "target")):
        refused = await answer(rig, await rig.staged(role=role, reason=reason))
        assert isinstance(refused, PretradeRefusal)
        assert "kill_switch_on" in refused.codes


async def test_a_protective_role_buy_is_refused(rig: Any) -> None:
    buy = await rig.staged(stop="101", limit="101.5", role=IntentRole.PROTECTIVE)
    refused = await answer(rig, buy)
    assert isinstance(refused, PretradeRefusal)
    assert "kill_switch_on" in refused.codes


async def test_an_exit_passes_and_reaches_the_broker(rig: Any) -> None:
    exit_intent = await rig.staged(side=OrderSide.SELL, quantity="10", limit="99.6")
    clearance = await answer(rig, exit_intent)
    assert isinstance(clearance, PretradeClearance)
    assert clearance.effect is RiskEffect.REDUCES
    placed = await rig.boundary.place(exit_intent.intent.intent_id, clearance)
    assert placed.status is PlacementStatus.PLACED


async def test_a_protective_stop_is_cleared_also_past_the_daily_cap(rig: Any) -> None:
    rig.make_gate(settings=with_caps(rig.settings, max_orders_per_day=1))
    await rig.kill.turn_off(actor="owner", reason="before the drill")
    assert isinstance(await answer(rig, await rig.staged()), PretradeClearance)
    assert isinstance(await answer(rig, await rig.staged()), PretradeRefusal)  # the cap is used
    await rig.kill.turn_on(actor="owner", reason="drill")
    stop = await rig.staged(
        side=OrderSide.SELL, stop="95", limit="94.5", role=IntentRole.PROTECTIVE
    )
    assert isinstance(await answer(rig, stop), PretradeClearance)


async def test_an_exit_above_the_per_order_cap_passes(rig: Any) -> None:
    rig.broker.nav = Decimal(3000)  # 25 % is 750 EUR; the exit is about 856 EUR
    exit_intent = await rig.staged(side=OrderSide.SELL, quantity="10", limit="99.6")
    assert isinstance(await answer(rig, exit_intent), PretradeClearance)


async def test_the_cash_check_and_the_nav_are_waived_for_a_sell(rig: Any) -> None:
    rig.broker.cash = {"EUR": (Decimal(0), None)}  # no dollars at all
    rig.broker.nav = None
    exit_intent = await rig.staged(side=OrderSide.SELL, quantity="10", limit="99.6")
    assert isinstance(await answer(rig, exit_intent), PretradeClearance)
    assert rig.broker.whatifs == []  # no whatif: nothing to fund


async def test_buy_side_eligibility_and_approval_are_waived_for_a_sell(rig: Any) -> None:
    await rig.hold(US_ETF, "3")  # an ETF the policy does not allow, assigned to the core
    sell = await rig.staged(
        US_ETF, side=OrderSide.SELL, quantity="3", limit="99.6", approval_required=True
    )
    assert isinstance(await answer(rig, sell), PretradeClearance)


async def test_flatten_clears_its_closing_orders(rig: Any) -> None:
    report = await rig.flattener.flatten(actor="owner")
    (line,) = report.lines
    assert line.closing
    assert report.ok
    exits = [o for o in rig.broker.live() if o.side == "SELL"]
    assert [o.quantity for o in exits] == [Decimal(10)]


async def test_a_sell_of_an_unmanaged_holding_is_refused(rig: Any) -> None:
    await rig.assign(STOCK, "unmanaged")
    refused = await answer(rig, await rig.staged(side=OrderSide.SELL, quantity="10", limit="99.6"))
    assert isinstance(refused, PretradeRefusal)
    assert refused.codes == ("holding_unmanaged",)


async def test_an_option_close_without_a_real_time_quote_is_refused(rig: Any) -> None:
    await rig.hold(OPTION, "1")
    rig.broker.quote(OPTION, bid="4.9", ask="5.1", last="5", option=True, availability="DP")
    close = await rig.staged(OPTION, side=OrderSide.SELL, quantity="1", limit="4.98")
    refused = await answer(rig, close)
    assert isinstance(refused, PretradeRefusal)
    assert refused.codes == ("quote_policy",)
    assert [a.kind for a in rig.alerts] == [OrderAlertKind.OPTION_CLOSE_BLOCKED]


async def test_a_closing_limit_outside_the_collar_is_refused(rig: Any) -> None:
    refused = await answer(rig, await rig.staged(side=OrderSide.SELL, quantity="10", limit="99"))
    assert isinstance(refused, PretradeRefusal)
    assert refused.codes == ("collar",)


async def test_a_sell_beyond_the_position_is_not_waived(rig: Any) -> None:
    refused = await answer(rig, await rig.staged(side=OrderSide.SELL, quantity="11", limit="99.6"))
    assert isinstance(refused, PretradeRefusal)
    assert refused.codes == ("short",)
