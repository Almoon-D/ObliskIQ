"""``flatten``: closing orders for the managed positions, separate from the kill switch.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from decimal import Decimal
from typing import Any

import pytest
from order_rig import EUR_ETF, OPTION, STOCK, US_ETF

from obliskiq.domain.orders import IntentRole, IntentState, OrderSide, PretradeClearance
from obliskiq.execution.flatten import FLATTEN_REASON, FlattenLine, FlattenReport
from obliskiq.ibkr.orders import PlacementStatus


@pytest.fixture
def rig(order_rig: Any) -> Any:
    order_rig.ready()
    return order_rig


async def test_flatten_closes_each_managed_position_and_reports_the_rest(rig: Any) -> None:
    await rig.hold(STOCK, "10")
    await rig.hold(EUR_ETF, "5", owner="unmanaged")
    await rig.hold(US_ETF, "3", owner=None)
    await rig.hold(999, "2")  # assigned, but nothing describes it
    await rig.hold(OPTION, "1")
    del rig.broker.quotes[OPTION]  # no bid to price its close
    await rig.hold(LEVERAGED_SHORT, "-4")
    report = await rig.flattener.flatten(actor="owner")
    outcomes = {line.conid: line for line in report.lines}
    assert outcomes[STOCK].closing
    assert outcomes[STOCK].intent_id is not None
    assert outcomes[EUR_ETF].skipped
    assert outcomes[EUR_ETF].outcome == "skipped: unmanaged"
    assert outcomes[US_ETF].skipped
    assert outcomes[US_ETF].outcome == "skipped: held by no sleeve"
    assert outcomes[999].outcome == "the instrument is not described"
    assert outcomes[OPTION].outcome.startswith("no bid")
    assert "a buy would add risk" in outcomes[LEVERAGED_SHORT].outcome
    assert not report.ok
    rendered = report.render()
    assert f"conid {STOCK} (10.0): closing" in rendered
    stored = await rig.book.get(outcomes[STOCK].intent_id)
    intent = stored.intent
    assert (intent.role, intent.reason, intent.side) == (
        IntentRole.EXIT,
        FLATTEN_REASON,
        OrderSide.SELL,
    )
    assert intent.terms.price.limit == Decimal("99.9")  # at the bid
    assert intent.terms.tif.value == rig.settings.execution.flatten.tif.value
    assert intent.state is IntentState.WORKING


async def test_flatten_cuts_the_protective_stop_first(rig: Any) -> None:
    await rig.hold(STOCK, "10")
    stop = await rig.staged(
        side=OrderSide.SELL, stop="95", limit="94.5", role=IntentRole.PROTECTIVE
    )
    clearance = await rig.gate.clear(stop.intent)
    assert isinstance(clearance, PretradeClearance)
    placed = await rig.boundary.place(stop.intent.intent_id, clearance)
    assert placed.status is PlacementStatus.PLACED
    assert placed.order_id is not None
    report = await rig.flattener.flatten(actor="owner")
    assert report.ok
    assert rig.broker.orders[placed.order_id].status == "Cancelled"
    sells = [o for o in rig.broker.live() if o.side == "SELL"]
    assert [(o.order_type, o.quantity) for o in sells] == [("LMT", Decimal(10))]


async def test_a_refused_close_is_reported(rig: Any) -> None:
    await rig.hold(STOCK, "10")
    rig.broker.quote(STOCK, bid="99", ask="101")  # the bid is outside the collar of the mid
    report = await rig.flattener.flatten(actor="owner")
    (line,) = report.lines
    assert not line.closing
    assert line.outcome.startswith("refused (collar")
    assert not report.ok


async def test_flatten_with_nothing_held(rig: Any) -> None:
    report = await rig.flattener.flatten(actor="owner")
    assert report.lines == ()
    assert report.ok
    assert report.render() == "flatten: no positions"


async def test_flatten_stops_when_the_positions_cannot_be_read(rig: Any) -> None:
    rig.broker.unavailable.add("positions")
    report = await rig.flattener.flatten(actor="owner")
    assert report.error is not None
    assert not report.ok
    assert report.render().startswith("flatten stopped: the positions cannot be read")


async def test_a_quote_that_cannot_be_read_is_reported(rig: Any) -> None:
    await rig.hold(STOCK, "10")
    rig.broker.unavailable.add("snapshot")
    (line,) = (await rig.flattener.flatten(actor="owner")).lines
    assert line.outcome.startswith("no quote")


def test_a_report_with_only_skipped_lines_is_ok() -> None:
    skipped = FlattenLine(1, Decimal(1), False, "skipped: unmanaged", skipped=True)
    assert FlattenReport(lines=(skipped,)).ok


#: A position the broker reports short (it should never exist).
LEVERAGED_SHORT = 600003


async def test_a_position_the_broker_lists_at_zero_is_left_out(rig: Any) -> None:
    await rig.hold(STOCK, "0")
    report = await rig.flattener.flatten(actor="owner")
    assert report.lines == ()


async def test_one_position_that_fails_does_not_stop_the_others(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obliskiq.ibkr.errors import IbkrUpstreamError

    await rig.hold(STOCK, "10")
    await rig.hold(EUR_ETF, "5")
    place_exit = rig.coordinator.place_exit

    async def failing(intent_id: Any) -> Any:
        stored = await rig.book.get(intent_id)
        if stored.intent.conid == STOCK:
            raise IbkrUpstreamError("the stop's status could not be read")
        return await place_exit(intent_id)

    monkeypatch.setattr(rig.coordinator, "place_exit", failing)
    report = await rig.flattener.flatten(actor="owner")
    outcomes = {line.conid: line for line in report.lines}
    assert outcomes[STOCK].outcome.startswith("stopped:")
    assert outcomes[EUR_ETF].closing
