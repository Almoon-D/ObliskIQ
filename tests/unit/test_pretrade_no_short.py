"""No short, ever (I2): the gate on the fake broker, and the counting it rests on.

Every sell that is working or could still reach the broker — cleared but not listed, in any
process — never exceeds the long position, the lower of the broker's read and the store's after
every reported fill. A modify counts its order once; a conversion is bounded by cash, not here.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from order_rig import STOCK, build_rig, terms

from obliskiq.domain.enums import AssetClass, MarketDataStatus
from obliskiq.domain.orders import (
    BrokerOrder,
    ClearanceAction,
    Fill,
    IntentRole,
    IntentState,
    OrderSide,
    Position,
    PretradeClearance,
    QuoteEvidence,
    RiskEffect,
)
from obliskiq.execution.exposure import (
    SellRequest,
    UnreadableOrdersError,
    count_sells,
    store_position,
)
from obliskiq.execution.pretrade import PretradeRefusal
from obliskiq.ibkr.orders import PlacementStatus
from obliskiq.store.orders import AccountRead, OpenClearance


@pytest.fixture
def rig(order_rig: Any) -> Any:
    order_rig.ready()
    return order_rig


async def clear(rig: Any, stored: Any, **kwargs: Any) -> PretradeClearance | PretradeRefusal:
    answer: PretradeClearance | PretradeRefusal = await rig.gate.clear(stored.intent, **kwargs)
    return answer


async def placed_sell(rig: Any, **kwargs: Any) -> tuple[Any, str]:
    stored = await rig.staged(side=OrderSide.SELL, **kwargs)
    clearance = await clear(rig, stored)
    assert isinstance(clearance, PretradeClearance), clearance
    result = await rig.boundary.place(stored.intent.intent_id, clearance)
    assert result.status is PlacementStatus.PLACED
    assert result.order_id is not None
    return await rig.book.get(stored.intent.intent_id), result.order_id


# ── on the gate ──


async def test_a_sell_above_the_position_is_refused(rig: Any) -> None:
    await rig.hold(STOCK, "10")
    answer = await clear(rig, await rig.staged(side=OrderSide.SELL, quantity="10.5"))
    assert isinstance(answer, PretradeRefusal)
    assert answer.codes == ("short",)
    assert "position of 10" in answer.reason


async def test_an_exit_beside_a_resting_stop_that_together_oversell_is_refused(rig: Any) -> None:
    await rig.hold(STOCK, "10")
    await placed_sell(rig, stop="95", limit="94.5", role=IntentRole.PROTECTIVE)
    assert not await rig.kill.is_on()
    answer = await clear(rig, await rig.staged(side=OrderSide.SELL, quantity="4"))
    assert isinstance(answer, PretradeRefusal)
    assert answer.codes == ("short",)
    assert "working sell" in answer.reason


async def test_a_sell_cleared_but_listed_late_still_counts(rig: Any) -> None:
    await rig.hold(STOCK, "10")
    rig.broker.listing_delay = 5  # the broker takes it, then leaves it out of its lists
    await placed_sell(rig, quantity="6")
    answer = await clear(rig, await rig.staged(side=OrderSide.SELL, quantity="5"))
    assert isinstance(answer, PretradeRefusal)
    assert answer.codes == ("short",)
    assert "cleared, not listed" in answer.reason


async def test_a_sell_cleared_and_not_yet_sent_counts_until_it_expires(rig: Any) -> None:
    await rig.hold(STOCK, "10")
    first = await clear(rig, await rig.staged(side=OrderSide.SELL, quantity="6"))
    assert isinstance(first, PretradeClearance)
    second = await rig.staged(side=OrderSide.SELL, quantity="5")
    assert isinstance(await clear(rig, second), PretradeRefusal)
    rig.clock.advance(61)  # the first clearance can no longer be spent
    assert isinstance(await clear(rig, second), PretradeClearance)


async def test_clearing_the_same_intent_again_counts_it_once(rig: Any) -> None:
    await rig.hold(STOCK, "10")
    stored = await rig.staged(side=OrderSide.SELL, quantity="10")
    for _ in range(2):
        assert isinstance(await clear(rig, stored), PretradeClearance)


async def test_two_processes_clearing_one_instrument_never_together_exceed_it(
    rig: Any, make_session: Callable[..., Any], tmp_path: Path
) -> None:
    other = await build_rig(
        settings=rig.settings,
        session=make_session(),
        broker=rig.broker,
        tmp_path=tmp_path,
        calendars=rig.calendars,
    )
    try:
        other.clock.now = rig.clock.now
        await rig.hold(STOCK, "10")
        mine = await rig.staged(side=OrderSide.SELL, quantity="6")
        theirs = await rig.staged(side=OrderSide.SELL, quantity="6")
        answers = await asyncio.gather(rig.gate.clear(mine.intent), other.gate.clear(theirs.intent))
        assert sum(isinstance(a, PretradeClearance) for a in answers) == 1
        assert sum(isinstance(a, PretradeRefusal) and a.codes == ("short",) for a in answers) == 1
    finally:
        await other.aclose()


async def test_a_modify_counts_its_order_once_at_its_new_quantity(rig: Any) -> None:
    await rig.hold(STOCK, "10")
    stored, order_id = await placed_sell(rig, quantity="10")
    smaller = terms(side=OrderSide.SELL, quantity="8")
    assert isinstance(
        await clear(rig, stored, action=ClearanceAction.MODIFY, order_id=order_id, terms=smaller),
        PretradeClearance,
    )
    larger = terms(side=OrderSide.SELL, quantity="11")
    answer = await clear(
        rig, stored, action=ClearanceAction.MODIFY, order_id=order_id, terms=larger
    )
    assert isinstance(answer, PretradeRefusal)
    assert answer.codes == ("short",)


async def test_a_modify_not_yet_applied_keeps_the_order_at_its_larger_size(rig: Any) -> None:
    await rig.hold(STOCK, "10")
    stored, order_id = await placed_sell(rig, quantity="6")
    bigger = terms(side=OrderSide.SELL, quantity="8")
    cleared = await clear(
        rig, stored, action=ClearanceAction.MODIFY, order_id=order_id, terms=bigger
    )
    assert isinstance(cleared, PretradeClearance)  # unspent: it could still be sent
    answer = await clear(rig, await rig.staged(side=OrderSide.SELL, quantity="3"))
    assert isinstance(answer, PretradeRefusal)
    assert answer.codes == ("short",)


async def test_a_stale_broker_position_after_a_fill_does_not_rearm_a_stop(rig: Any) -> None:
    await rig.hold(STOCK, "10")
    _, order_id = await placed_sell(rig, quantity="10")
    rig.broker.stale_positions = True  # the portfolio still reads 10 after the fill
    rig.clock.advance(1)
    rig.broker.fill(order_id)
    rig.clock.advance(1)
    stop = await rig.staged(
        side=OrderSide.SELL, stop="95", limit="94.5", role=IntentRole.PROTECTIVE
    )
    answer = await clear(rig, stop)
    assert isinstance(answer, PretradeRefusal)
    assert answer.codes == ("short",)
    assert "broker 10" in answer.reason
    assert "store 0" in answer.reason


async def test_a_sell_of_a_working_order_with_no_size_is_refused(rig: Any) -> None:
    await rig.hold(STOCK, "10")

    def no_size(rows: list[Any]) -> list[Any]:
        for row in rows:
            row.pop("totalSize", None)
            row.pop("remainingQuantity", None)
        return rows

    rig.broker.add_order(side="SELL", conid=STOCK, quantity="2", coid="manual")
    rig.broker.rewrite_live_rows = no_size
    answer = await clear(rig, await rig.staged(side=OrderSide.SELL, quantity="1"))
    assert isinstance(answer, PretradeRefusal)
    assert answer.codes == ("account_unreadable",)


async def test_a_sell_fits_beside_sells_of_other_instruments(rig: Any) -> None:
    await rig.hold(STOCK, "10")
    rig.broker.add_order(side="SELL", conid=999, quantity="50", coid="other-instrument")
    rig.broker.add_order(side="BUY", conid=STOCK, quantity="50", coid="a-buy")
    answer = await clear(rig, await rig.staged(side=OrderSide.SELL, quantity="10"))
    assert isinstance(answer, PretradeClearance)
    assert answer.effect is RiskEffect.REDUCES


# ── the counting, as functions ──

AT = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)


def read(quantity: str, at: datetime) -> AccountRead[Position]:
    rows = (
        ()
        if quantity == "0"
        else (
            Position(
                conid=STOCK,
                quantity=Decimal(quantity),
                average_cost=None,
                currency="USD",
                observed_at=at,
            ),
        )
    )
    return AccountRead(observed_at=at, rows=rows)


def fill(side: OrderSide, quantity: str, at: datetime, conid: int = STOCK) -> Fill:
    return Fill(
        execution_id=f"e-{uuid.uuid4()}",
        coid="c",
        conid=conid,
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(100),
        commission=None,
        executed_at=at,
    )


def test_the_store_position_is_the_latest_read_without_fills() -> None:
    reads = [read("0", AT), read("10", AT + timedelta(minutes=1))]
    assert store_position(reads, [], STOCK) == Decimal(10)  # a transfer in is no fill
    assert store_position([], [], STOCK) is None


def test_the_store_position_takes_the_lowest_read_from_before_the_first_fill() -> None:
    before = read("10", AT)
    stale = read("10", AT + timedelta(seconds=2))  # taken after the sell, not showing it
    sold = fill(OrderSide.SELL, "10", AT + timedelta(seconds=1))
    assert store_position([before, stale], [sold], STOCK) == Decimal(0)
    assert store_position([stale], [sold], STOCK) == Decimal(10)  # no earlier read to anchor


def test_a_sell_in_the_same_second_as_a_read_counts_and_a_buy_does_not() -> None:
    assert store_position([read("10", AT)], [fill(OrderSide.SELL, "4", AT)], STOCK) == Decimal(6)
    assert store_position([read("10", AT)], [fill(OrderSide.BUY, "4", AT)], STOCK) == Decimal(10)
    later = AT + timedelta(seconds=1)
    assert store_position([read("10", AT)], [fill(OrderSide.BUY, "4", later)], STOCK) == Decimal(14)


def test_fills_of_other_instruments_do_not_move_the_position() -> None:
    other = fill(OrderSide.SELL, "10", AT, conid=999)
    assert store_position([read("10", AT)], [other], STOCK) == Decimal(10)


def order(
    order_id: str, *, side: OrderSide = OrderSide.SELL, status: str = "Submitted", **kw: Any
) -> BrokerOrder:
    fields: dict[str, Any] = {
        "order_id": order_id,
        "coid": kw.pop("coid", f"coid-{order_id}"),
        "conid": kw.pop("conid", STOCK),
        "side": side,
        "sec_type": kw.pop("sec_type", "STK"),
        "order_type": "LMT",
        "status": status,
        "total_quantity": kw.pop("total", Decimal(5)),
        "filled_quantity": kw.pop("filled", Decimal(0)),
        "remaining_quantity": kw.pop("remaining", None),
        "average_price": None,
        "tif": "DAY",
        "observed_at": AT,
    }
    return BrokerOrder(**fields)


def request(quantity: str = "1", **kw: Any) -> SellRequest:
    return SellRequest(
        intent_id=kw.pop("intent_id", uuid.uuid7()),
        coid=kw.pop("coid", "mine"),
        conid=STOCK,
        quantity=Decimal(quantity),
        order_id=kw.pop("order_id", None),
    )


def open_clearance(
    quantity: str,
    *,
    action: ClearanceAction = ClearanceAction.PLACE,
    order_id: str | None = None,
    used: bool = False,
    used_at: datetime = AT,
    coid: str = "theirs",
    intent_id: uuid.UUID | None = None,
    expires: datetime = AT + timedelta(seconds=30),
) -> OpenClearance:
    clearance = PretradeClearance(
        clearance_id=uuid.uuid4(),
        intent_id=intent_id or uuid.uuid7(),
        action=action,
        order_id=order_id,
        terms=terms(side=OrderSide.SELL, quantity=quantity),
        effect=RiskEffect.REDUCES,
        asset_class=AssetClass.STOCK,
        quote=QuoteEvidence(
            conid=STOCK, status=MarketDataStatus.REALTIME, delay_seconds=0, observed_at=AT
        ),
        issued_at=AT - timedelta(seconds=10),
        expires_at=expires,
    )
    return OpenClearance(
        clearance=clearance,
        used_at=used_at if used else None,
        coid=coid,
        intent_state=IntentState.SUBMITTED if used else IntentState.STAGED,
    )


def count(
    req: SellRequest,
    live: list[BrokerOrder],
    clearances: list[OpenClearance],
    filled: dict[str, Decimal] | None = None,
) -> Decimal:
    return count_sells(
        req, live=live, clearances=clearances, filled_by_coid=filled or {}, now=AT
    ).total


def test_working_sells_count_at_what_remains_and_finished_ones_not_at_all() -> None:
    live = [
        order("1", filled=Decimal(2)),
        order("2", remaining=Decimal(1)),
        order("3", status="Filled"),
        order("4", side=OrderSide.BUY),
        order("5", sec_type="CASH"),
        order("6", conid=999),
    ]
    assert count(request(), live, []) == Decimal(3 + 1 + 1)


def test_an_order_of_unreadable_side_or_size_cannot_be_judged() -> None:
    with pytest.raises(UnreadableOrdersError):
        count(request(), [order("1", conid=None)], [])
    with pytest.raises(UnreadableOrdersError):
        count(request(), [order("1", total=None)], [])


def test_a_pending_modify_counts_the_order_at_its_larger_size() -> None:
    live = [order("1", total=Decimal(5))]
    pending = open_clearance("8", action=ClearanceAction.MODIFY, order_id="1")
    assert count(request(), live, [pending]) == Decimal(8 + 1)
    expired = open_clearance(
        "8", action=ClearanceAction.MODIFY, order_id="1", expires=AT - timedelta(seconds=1)
    )
    assert count(request(), live, [expired]) == Decimal(5 + 1)
    spent = open_clearance("3", action=ClearanceAction.MODIFY, order_id="1", used=True)
    assert count(request(), live, [spent]) == Decimal(5 + 1)  # applied or not: the larger


def test_cleared_sells_not_listed_count_once_per_intent_less_their_fills() -> None:
    intent = uuid.uuid7()
    unspent = open_clearance("4", intent_id=intent)
    spent = open_clearance("6", intent_id=intent, used=True)
    assert count(request(), [], [unspent, spent], {"theirs": Decimal(2)}) == Decimal(6 - 2 + 1)
    assert count(request(), [], [spent, unspent], {"theirs": Decimal(2)}) == Decimal(6 - 2 + 1)
    listed = order("9", coid="theirs", total=Decimal(6))
    assert count(request(), [listed], [unspent, spent]) == Decimal(6 + 1)


def test_the_requests_own_clearances_and_order_count_once() -> None:
    mine = uuid.uuid7()
    previous = open_clearance("10", intent_id=mine, used=True, coid="mine")
    listed = order("7", coid="mine", total=Decimal(10))
    assert count(request("10", intent_id=mine), [listed], [previous]) == Decimal(10)


def test_a_modify_request_replaces_its_order() -> None:
    live = [order("1", total=Decimal(10), filled=Decimal(2))]
    assert count(request("6", order_id="1"), live, []) == Decimal(6 - 2)
    other = open_clearance("9", action=ClearanceAction.MODIFY, order_id="1")
    assert count(request("6", order_id="1"), live, [other]) == Decimal(9 - 2)


def test_only_the_latest_spent_modify_of_an_order_counts() -> None:
    live = [order("1", total=Decimal(5))]
    newer = open_clearance("7", action=ClearanceAction.MODIFY, order_id="1", used=True)
    older = open_clearance(
        "9",
        action=ClearanceAction.MODIFY,
        order_id="1",
        used=True,
        used_at=AT - timedelta(minutes=1),
    )
    assert count(request(), live, [newer, older]) == Decimal(7 + 1)


def test_modifies_spent_in_the_same_second_count_at_the_larger_size() -> None:
    live = [order("1", total=Decimal(5))]
    first = open_clearance("7", action=ClearanceAction.MODIFY, order_id="1", used=True)
    second = open_clearance("3", action=ClearanceAction.MODIFY, order_id="1", used=True)
    # Second-resolution stamps cannot order them: either may be the one the broker applied.
    assert count(request(), live, [first, second]) == Decimal(7 + 1)
    assert count(request(), live, [second, first]) == Decimal(7 + 1)
