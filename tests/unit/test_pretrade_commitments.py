"""What the gate adds up across clearances: the cash open buys commit in a currency, and the
notional and the orders of the last 24 hours.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from decimal import Decimal
from typing import Any

import pytest
from order_rig import STOCK, terms, with_caps

from obliskiq.domain.orders import ClearanceAction, PretradeClearance
from obliskiq.execution.pretrade import PretradeRefusal
from obliskiq.ibkr.orders import PlacementStatus


@pytest.fixture
def rig(order_rig: Any) -> Any:
    order_rig.ready()
    return order_rig


async def place(rig: Any, stored: Any) -> str:
    clearance = await rig.gate.clear(stored.intent)
    assert isinstance(clearance, PretradeClearance), clearance
    result = await rig.boundary.place(stored.intent.intent_id, clearance)
    assert result.status is PlacementStatus.PLACED
    assert result.order_id is not None
    return result.order_id


async def test_a_buy_sized_in_cash_commits_its_cash_less_what_it_spent(rig: Any) -> None:
    sized = await rig.intent(terms(cash="1000"))
    clearance = await rig.gate.clear(sized.intent)
    assert isinstance(clearance, PretradeClearance)
    assert clearance.notional == Decimal(1000)
    order_id = await place(rig, sized)
    rig.broker.fill(order_id, quantity="5", price="100")  # 500 of the 1000 spent
    await rig.assign(STOCK)  # what Session 5 does with a first fill
    rig.broker.cash["USD"] = (Decimal(1600), Decimal("0.86"))
    second = await rig.staged()
    assert isinstance(await rig.gate.clear(second.intent), PretradeClearance)
    rig.broker.cash["USD"] = (Decimal(1500), Decimal("0.86"))
    refused = await rig.gate.clear(second.intent)
    assert isinstance(refused, PretradeRefusal)
    assert refused.codes == ("insufficient_cash",)
    assert "500.00 committed" in refused.reason


async def test_a_partly_filled_buy_commits_the_rest_of_its_notional(rig: Any) -> None:
    order_id = await place(rig, await rig.staged())
    rig.broker.fill(order_id, quantity="4")
    await rig.assign(STOCK)
    await rig.gate.clear((await rig.staged()).intent)  # reads the trades into the store
    rig.broker.cash["USD"] = (Decimal(1500), Decimal("0.86"))
    refused = await rig.gate.clear((await rig.staged(quantity="1")).intent)
    assert isinstance(refused, PretradeRefusal)
    assert (
        "1600.00 committed" in refused.reason
    )  # 600 open of the first, 1000 of the second's clearance


async def test_a_clearance_that_names_no_currency_counts_against_every_currency(rig: Any) -> None:
    other = await rig.staged()
    await rig.clear(other.intent)  # made outside the gate: no currency, no notional
    rig.broker.cash["USD"] = (Decimal(1500), Decimal("0.86"))
    refused = await rig.gate.clear((await rig.staged()).intent)
    assert isinstance(refused, PretradeRefusal)
    assert "insufficient_cash" in refused.codes
    assert "carries no notional" in refused.reason  # nor can the day's total be known


async def test_an_unspent_clearance_that_expired_no_longer_counts(rig: Any) -> None:
    rig.make_gate(settings=with_caps(rig.settings, max_orders_per_day=1))
    assert isinstance(await rig.gate.clear((await rig.staged()).intent), PretradeClearance)
    rig.clock.advance(61)
    assert isinstance(await rig.gate.clear((await rig.staged()).intent), PretradeClearance)


async def test_a_modify_is_not_another_order(rig: Any) -> None:
    rig.make_gate(settings=with_caps(rig.settings, max_orders_per_day=1))
    stored = await rig.staged()
    order_id = await place(rig, stored)
    stored = await rig.book.get(stored.intent.intent_id)
    reprice = await rig.gate.clear(
        stored.intent, action=ClearanceAction.MODIFY, order_id=order_id, terms=terms(limit="99.5")
    )
    assert isinstance(reprice, PretradeClearance)
    refused = await rig.gate.clear((await rig.staged()).intent)
    assert isinstance(refused, PretradeRefusal)
    assert refused.codes == ("daily_order_cap",)
