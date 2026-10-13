"""Decoding IBKR's order answers: shapes as IBKR documents them (docs/knowledge/ibkr-orders.md),
and reads that are incomplete raise instead of coming back short.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from obliskiq.domain.orders import OrderSide
from obliskiq.ibkr.errors import DecodeError
from obliskiq.ibkr.order_decode import (
    Placed,
    Question,
    Refused,
    UnexpectedAnswer,
    decode_cancel,
    decode_live_orders,
    decode_order_status,
    decode_place_answer,
    decode_trades,
    decode_whatif,
    side,
)

NOW = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)


def test_place_answers() -> None:
    placed = decode_place_answer([{"order_id": 1234567890, "order_status": "Submitted"}])
    assert placed == Placed(order_id="1234567890", order_status="Submitted")
    question = decode_place_answer(
        [
            {
                "id": "07a13a5a",
                "message": ["The following order exceeds the Percentage constraint of 3%."],
                "isSuppressed": False,
                "messageIds": ["o163"],
            }
        ]
    )
    assert question == Question(
        reply_id="07a13a5a",
        message_ids=("o163",),
        messages=("The following order exceeds the Percentage constraint of 3%.",),
    )
    refused = decode_place_answer({"error": "We cannot accept an order at the limit price."})
    assert isinstance(refused, Refused)
    assert not refused.duplicate
    duplicate = decode_place_answer(
        {"error": "Order couldn't be submitted: Local order ID=abc is already registered."}
    )
    assert isinstance(duplicate, Refused)
    assert duplicate.duplicate


@pytest.mark.parametrize(
    "body",
    [None, "text", {}, {"orders": []}, [], [1], [{"x": 1}], [{"id": "r"}], [{}, {}]],
)
def test_answers_of_no_known_shape(body: Any) -> None:
    assert isinstance(decode_place_answer(body), UnexpectedAnswer)


def test_a_question_without_ids_keeps_none() -> None:
    answer = decode_place_answer([{"id": "r", "message": ["?"], "messageIds": "o1"}])
    assert answer == Question(reply_id="r", message_ids=(), messages=("?",))


LIVE_ROW = {
    "acct": "DU1",
    "conid": 265598,
    "orderId": 1234568790,
    "orderDesc": "Sold 5 Market, GTC",
    "secType": "STK",
    "remainingQuantity": 0.0,
    "filledQuantity": 5.0,
    "totalSize": 5.0,
    "status": "Filled",
    "avgPrice": "192.26",
    "orderType": "Market",
    "order_ref": "Order123",
    "timeInForce": "GTC",
    "side": "SELL",
}


def test_live_orders() -> None:
    live = decode_live_orders({"orders": [LIVE_ROW], "snapshot": True}, observed_at=NOW)
    assert live.complete
    (order,) = live.orders
    assert order.order_id == "1234568790"
    assert order.coid == "Order123"
    assert order.side is OrderSide.SELL
    assert order.filled_quantity == Decimal(5)
    assert order.average_price == Decimal("192.26")
    assert not decode_live_orders({"orders": [], "snapshot": False}, observed_at=NOW).complete
    assert decode_live_orders({}, observed_at=NOW).orders == ()


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {"orders": "x"},
        {"orders": [1]},
        {"orders": [{**LIVE_ROW, "orderId": None}]},
        {"orders": [{**LIVE_ROW, "status": ""}]},
        {"orders": [{**LIVE_ROW, "conid": -5}]},
    ],
)
def test_an_incomplete_live_orders_read_raises(body: Any) -> None:
    with pytest.raises(DecodeError):
        decode_live_orders(body, observed_at=NOW)


def test_an_unreadable_side_or_conid_is_absent() -> None:
    row = {**LIVE_ROW, "side": "SHORT", "conid": "x"}
    (order,) = decode_live_orders({"orders": [row]}, observed_at=NOW).orders
    assert order.side is None
    assert order.conid is None


TRADE = {
    "execution_id": "0000e0d5.6576fd38.01.01",
    "side": "S",
    "size": 5.0,
    "price": "192.26",
    "order_ref": "Order123",
    "commission": "1.01",
    "conid": 265598,
    "trade_time_r": 1702317649000,
}


def test_trades() -> None:
    (fill,) = decode_trades([TRADE])
    assert fill.side is OrderSide.SELL
    assert fill.quantity == Decimal(5)
    assert fill.price == Decimal("192.26")
    assert fill.commission == Decimal("1.01")
    assert fill.coid == "Order123"
    assert fill.executed_at == datetime(2023, 12, 11, 18, 0, 49, tzinfo=UTC)
    assert decode_trades([{**TRADE, "side": "Buy"}])[0].side is OrderSide.BUY


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        [1],
        [{**TRADE, "execution_id": None}],
        [{**TRADE, "side": "X"}],
        [{**TRADE, "size": None}],
        [{**TRADE, "price": "n/a"}],
        [{**TRADE, "conid": True}],
        [{**TRADE, "trade_time_r": None}],
        [{**TRADE, "size": -1}],
    ],
)
def test_an_incomplete_trades_read_raises(body: Any) -> None:
    with pytest.raises(DecodeError):
        decode_trades(body)


def test_order_status() -> None:
    status = decode_order_status(
        {
            "order_id": 1799796559,
            "conid": 265598,
            "side": "S",
            "order_status": "Filled",
            "total_size": "5.0",
            "cum_fill": "5.0",
            "size": "0.0",
            "average_price": "192.26",
            "tif": "DAY",
            "order_type": "MARKET",
        },
        observed_at=NOW,
    )
    assert (status.order_id, status.status, status.side) == ("1799796559", "Filled", OrderSide.SELL)
    assert status.coid is None
    assert status.remaining_quantity == Decimal(0)
    with pytest.raises(DecodeError):
        decode_order_status([], observed_at=NOW)


def test_whatif() -> None:
    preview = decode_whatif(
        {
            "amount": {
                "amount": "1,977.60 USD (10 Shares)",
                "commission": "1 USD",
                "total": "1,978.60 USD",
            },
            "equity": {"current": "215,415,594", "change": "-1", "after": "215,415,593"},
            "initial": {"current": "116,965", "change": "652", "after": "117,617"},
            "maintenance": {"current": "106,332", "change": "592", "after": "106,924"},
            "position": {"current": "0", "change": "10", "after": "10"},
            "warn": "21/You are trying to submit an order without having market data.",
            "error": None,
        }
    )
    assert preview.amount == Decimal("1977.60")
    assert preview.initial_margin.change == Decimal(652)
    assert preview.equity.after == Decimal(215415593)
    assert preview.position.after == Decimal(10)
    assert (preview.warning_code, preview.error) == ("21", None)
    plain = decode_whatif({"warn": "no code here", "error": "Insufficient funds", "amount": "x"})
    assert (plain.warning_code, plain.warning, plain.error) == (
        None,
        "no code here",
        "Insufficient funds",
    )
    assert plain.amount is None
    assert plain.equity.current is None
    assert decode_whatif({"warn": "x/not a number"}).warning_code is None
    with pytest.raises(DecodeError):
        decode_whatif([])


def test_cancel_answers() -> None:
    ok = decode_cancel({"msg": "Request was submitted", "order_id": 123456789, "conid": 265598})
    assert (ok.order_id, ok.message, ok.error) == ("123456789", "Request was submitted", None)
    refused = decode_cancel({"error": "OrderID 1 doesn't exist"})
    assert refused.error == "OrderID 1 doesn't exist"
    assert decode_cancel([]).error is not None


def test_sides() -> None:
    assert side("b") is OrderSide.BUY
    assert side(" SELL ") is OrderSide.SELL
    assert side(None) is None
    assert side(True) is None
    assert side(["S"]) is None
