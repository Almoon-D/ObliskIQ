"""Decoding IBKR's order responses: pure functions, no I/O.

The shapes come from IBKR's Web API reference and ibind (docs/knowledge/ibkr-orders.md); none has
been seen live through ObliskIQ yet (VERIFY-LIVE). A read that is incomplete raises rather than
return a short list: an order missing from a lookup is how a placement happens twice, and a
missing fill is how a position is misread.

* A placement answers one of: the order (``order_id``, ``order_status``); a warning to confirm
  (``id``, ``message``, ``messageIds``); or a refusal (``{"error": ...}``, HTTP 200). Anything
  else is :class:`UnexpectedAnswer`: the order may or may not exist.
* Live orders carry the cOID as ``order_ref`` and a ``snapshot`` flag; a list that is not a
  complete snapshot proves nothing about an absent cOID.
* Trades give the side as ``B``/``S`` (or the word), the size as a number and prices as text.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from ..domain.orders import BrokerOrder, Fill, OrderSide
from .decode import epoch_ms_to_utc, to_decimal
from .errors import DecodeError

#: The refusal IBKR gives a cOID already used inside its uniqueness window (as ibind matches it).
DUPLICATE_COID_MARKER = "local order id="

#: The separator between a whatif warning's code and its text ("21/You are trying ...").
WARNING_CODE_SEPARATOR = "/"

_SIDES = {"B": OrderSide.BUY, "BUY": OrderSide.BUY, "S": OrderSide.SELL, "SELL": OrderSide.SELL}


@dataclass(frozen=True, slots=True)
class Placed:
    """The broker took the order."""

    order_id: str
    order_status: str | None


@dataclass(frozen=True, slots=True)
class Question:
    """A warning the order waits on: confirm or decline ``reply_id``."""

    reply_id: str
    message_ids: tuple[str, ...]
    messages: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Refused:
    """The broker refused the order; ``duplicate`` when the cOID was already used."""

    error: str
    duplicate: bool


@dataclass(frozen=True, slots=True)
class UnexpectedAnswer:
    """An answer of no known shape: the order may exist."""

    detail: str


type PlaceAnswer = Placed | Question | Refused | UnexpectedAnswer


@dataclass(frozen=True, slots=True)
class LiveOrders:
    """The live-orders list; ``complete`` only when IBKR marked it a snapshot."""

    orders: tuple[BrokerOrder, ...]
    complete: bool


@dataclass(frozen=True, slots=True)
class LiveOrderRows:
    """The live orders read row by row (the kill switch's sweep): the rows that decoded, and
    the order ids of those that did not (``None`` where even the id is unreadable)."""

    orders: tuple[BrokerOrder, ...]
    unreadable: tuple[str | None, ...]


@dataclass(frozen=True, slots=True)
class Impact:
    """One ``{current, change, after}`` block of a whatif, as amounts where they parse."""

    current: Decimal | None
    change: Decimal | None
    after: Decimal | None


@dataclass(frozen=True, slots=True)
class WhatIf:
    """A previewed order: its cost and its effect on equity, margin and the position."""

    amount: Decimal | None
    commission: Decimal | None
    total: Decimal | None
    equity: Impact
    initial_margin: Impact
    maintenance_margin: Impact
    position: Impact
    warning_code: str | None
    warning: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class CancelAnswer:
    """The answer to a cancel: accepted (``error`` is ``None``) or refused."""

    order_id: str | None
    message: str | None
    error: str | None


def _text(raw: Any) -> str | None:
    if raw is None or isinstance(raw, bool | Mapping | list):
        return None
    text = str(raw).strip()
    return text or None


def _id(raw: Any) -> str | None:
    """An IBKR id, which arrives as a number or as text."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return str(raw)
    return _text(raw)


def side(raw: Any) -> OrderSide | None:
    text = _text(raw)
    return None if text is None else _SIDES.get(text.upper())


def _strings(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(text for item in raw if (text := _text(item)) is not None)


def _leading_amount(raw: Any) -> Decimal | None:
    """The amount at the start of a display string: ``"1,977.60 USD (10 Shares)"`` → 1977.60."""
    text = _text(raw)
    if text is None:
        return None
    return to_decimal(text.split()[0])


def decode_place_answer(body: Any) -> PlaceAnswer:
    """What a placement, a modify or a reply answered."""
    if isinstance(body, Mapping):
        error = _text(body.get("error"))
        if error is not None:
            return Refused(error=error, duplicate=DUPLICATE_COID_MARKER in error.lower())
        return UnexpectedAnswer(f"an object without an error: keys {sorted(map(str, body))}")
    if not isinstance(body, list) or len(body) != 1 or not isinstance(body[0], Mapping):
        size = len(body) if isinstance(body, list) else None
        return UnexpectedAnswer(f"not a one-item list (got {type(body).__name__}, {size} items)")
    item = body[0]
    order_id = _id(item.get("order_id"))
    if order_id is not None:
        return Placed(order_id=order_id, order_status=_text(item.get("order_status")))
    reply_id = _id(item.get("id"))
    if reply_id is not None and "message" in item:
        return Question(
            reply_id=reply_id,
            message_ids=_strings(item.get("messageIds")),
            messages=_strings(item.get("message")),
        )
    error = _text(item.get("error"))
    if error is not None:
        return Refused(error=error, duplicate=DUPLICATE_COID_MARKER in error.lower())
    return UnexpectedAnswer(f"an item of unknown shape: keys {sorted(map(str, item))}")


def _broker_order(row: Mapping[str, Any], observed_at: datetime, *, coid_key: str) -> BrokerOrder:
    order_id = _id(row.get("orderId", row.get("order_id")))
    status = _text(row.get("status", row.get("order_status")))
    if order_id is None or status is None:
        raise DecodeError("an order without an id or a status: the order read is incomplete")
    conid = row.get("conid")
    try:
        return BrokerOrder(
            order_id=order_id,
            coid=_text(row.get(coid_key)),
            conid=conid if isinstance(conid, int) and not isinstance(conid, bool) else None,
            side=side(row.get("side")),
            sec_type=_text(row.get("secType", row.get("sec_type"))),
            order_type=_text(row.get("orderType", row.get("order_type"))),
            status=status,
            total_quantity=to_decimal(row.get("totalSize", row.get("total_size"))),
            filled_quantity=to_decimal(row.get("filledQuantity", row.get("cum_fill"))),
            remaining_quantity=to_decimal(row.get("remainingQuantity", row.get("size"))),
            average_price=to_decimal(row.get("avgPrice", row.get("average_price"))),
            tif=_text(row.get("timeInForce", row.get("tif"))),
            observed_at=observed_at,
        )
    except ValidationError as exc:
        raise DecodeError(
            f"order {order_id} cannot be read: {exc.error_count()} problem(s)"
        ) from None


def decode_live_orders(body: Any, *, observed_at: datetime) -> LiveOrders:
    """``GET /iserver/account/orders``."""
    if not isinstance(body, Mapping) or not isinstance(body.get("orders", []), list):
        raise DecodeError("the live-orders answer is not an object holding a list of orders")
    rows = body.get("orders", [])
    if not all(isinstance(row, Mapping) for row in rows):
        raise DecodeError("a live order is not an object: the order read is incomplete")
    orders = tuple(_broker_order(row, observed_at, coid_key="order_ref") for row in rows)
    return LiveOrders(orders=orders, complete=body.get("snapshot") is True)


def decode_live_order_rows(body: Any, *, observed_at: datetime) -> LiveOrderRows:
    """``GET /iserver/account/orders`` row by row: one unreadable row does not hide the others.

    For the kill switch, which must still cancel what it can read; the cOID lookup uses the
    all-or-nothing :func:`decode_live_orders`.
    """
    if not isinstance(body, Mapping) or not isinstance(body.get("orders", []), list):
        raise DecodeError("the live-orders answer is not an object holding a list of orders")
    orders: list[BrokerOrder] = []
    unreadable: list[str | None] = []
    for row in body.get("orders", []):
        if not isinstance(row, Mapping):
            unreadable.append(None)
            continue
        try:
            orders.append(_broker_order(row, observed_at, coid_key="order_ref"))
        except DecodeError:
            unreadable.append(_id(row.get("orderId", row.get("order_id"))))
    return LiveOrderRows(orders=tuple(orders), unreadable=tuple(unreadable))


def decode_order_status(body: Any, *, observed_at: datetime) -> BrokerOrder:
    """``GET /iserver/account/order/status/{orderId}`` (it carries no cOID)."""
    if not isinstance(body, Mapping):
        raise DecodeError("the order-status answer is not an object")
    return _broker_order(body, observed_at, coid_key="order_ref")


def _fill(row: Mapping[str, Any]) -> Fill:
    execution_id = _text(row.get("execution_id"))
    executed_at = epoch_ms_to_utc(row.get("trade_time_r"))
    conid = row.get("conid")
    fill_side = side(row.get("side"))
    quantity = to_decimal(row.get("size"))
    price = to_decimal(row.get("price"))
    if (
        execution_id is None
        or executed_at is None
        or not isinstance(conid, int)
        or isinstance(conid, bool)
        or fill_side is None
        or quantity is None
        or price is None
    ):
        raise DecodeError(
            f"trade {execution_id or '?'} lacks an id, time, conid, side, size or price: the "
            "trades read is incomplete"
        )
    try:
        return Fill(
            execution_id=execution_id,
            coid=_text(row.get("order_ref")),
            conid=conid,
            side=fill_side,
            quantity=quantity,
            price=price,
            commission=to_decimal(row.get("commission")),
            executed_at=executed_at,
        )
    except ValidationError as exc:
        raise DecodeError(
            f"trade {execution_id} cannot be read: {exc.error_count()} problem(s)"
        ) from None


def decode_trades(body: Any) -> tuple[Fill, ...]:
    """``GET /iserver/account/trades``."""
    if not isinstance(body, list) or not all(isinstance(row, Mapping) for row in body):
        raise DecodeError("the trades answer is not a list of objects")
    return tuple(_fill(row) for row in body)


def _impact(raw: Any) -> Impact:
    block = raw if isinstance(raw, Mapping) else {}
    return Impact(
        current=_leading_amount(block.get("current")),
        change=_leading_amount(block.get("change")),
        after=_leading_amount(block.get("after")),
    )


def decode_whatif(body: Any) -> WhatIf:
    """``POST /iserver/account/{accountId}/orders/whatif``. The warning keeps its code."""
    if not isinstance(body, Mapping):
        raise DecodeError("the whatif answer is not an object")
    raw_amount = body.get("amount")
    amount: Mapping[str, Any] = raw_amount if isinstance(raw_amount, Mapping) else {}
    warning = _text(body.get("warn"))
    code: str | None = None
    if warning is not None and WARNING_CODE_SEPARATOR in warning:
        head, _, rest = warning.partition(WARNING_CODE_SEPARATOR)
        if head.strip().isdigit():
            code, warning = head.strip(), rest.strip()
    return WhatIf(
        amount=_leading_amount(amount.get("amount")),
        commission=_leading_amount(amount.get("commission")),
        total=_leading_amount(amount.get("total")),
        equity=_impact(body.get("equity")),
        initial_margin=_impact(body.get("initial")),
        maintenance_margin=_impact(body.get("maintenance")),
        position=_impact(body.get("position")),
        warning_code=code,
        warning=warning,
        error=_text(body.get("error")),
    )


def decode_cancel(body: Any) -> CancelAnswer:
    """``DELETE /iserver/account/{accountId}/order/{orderId}``."""
    if not isinstance(body, Mapping):
        return CancelAnswer(order_id=None, message=None, error="the cancel answer is not an object")
    return CancelAnswer(
        order_id=_id(body.get("order_id")),
        message=_text(body.get("msg")),
        error=_text(body.get("error")),
    )
