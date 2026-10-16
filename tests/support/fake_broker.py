"""A stateful fake of IBKR's order endpoints, mocked with respx.

It keeps orders, warnings, trades and cancels the way the Client Portal Web API describes them
(docs/knowledge/ibkr-orders.md) and lets a test script the awkward cases: warnings to answer, a
refusal, a reused cOID, an answer lost after the order was taken, an order listed late, a
live-orders list that is not a complete snapshot, a fill that races a cancel. Sessions 4a and 5
extend it. Like IBKR, any request other than the reply invalidates an open warning. Shapes
are IBKR's documented ones, not live captures (VERIFY-LIVE).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx
import respx

BASE = "https://gw.test/v1/api"
ACCOUNT = "DUTEST1"

_ID = r"(?P<id>[^/?]+)"


@dataclass
class FakeOrder:
    """One order at the fake broker, in IBKR's live-orders vocabulary."""

    order_id: str
    coid: str | None
    conid: int
    side: str
    quantity: Decimal
    order_type: str
    price: Decimal | None
    aux_price: Decimal | None
    tif: str
    sec_type: str = "STK"
    status: str = "Submitted"
    filled: Decimal = Decimal(0)
    hidden_reads: int = 0  # live-orders reads that do not list it yet
    body: dict[str, Any] = field(default_factory=dict)

    def row(self) -> dict[str, Any]:
        return {
            "acct": ACCOUNT,
            "conid": self.conid,
            "orderId": int(self.order_id),
            "orderType": self.order_type,
            "side": self.side,
            "secType": self.sec_type,
            "status": self.status,
            "totalSize": float(self.quantity),
            "filledQuantity": float(self.filled),
            "remainingQuantity": float(self.quantity - self.filled),
            "timeInForce": self.tif,
            "order_ref": self.coid,
        }


@dataclass
class Pending:
    """A placement or modify waiting on its warnings."""

    order_id: str | None  # the order a modify changes; None for a placement
    body: dict[str, Any]
    questions: deque[tuple[list[str], str]]


class FakeBroker:
    """The order endpoints of one paper account."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.orders: dict[str, FakeOrder] = {}
        self.trades: list[dict[str, Any]] = []
        self.placements: list[dict[str, Any]] = []
        self.modifies: list[tuple[str, dict[str, Any]]] = []
        self.replies: list[tuple[str, bool]] = []
        self.cancels: list[str] = []
        self.whatifs: list[dict[str, Any]] = []
        self.suppress_resets = 0
        self.live_order_reads = 0
        self.trade_reads = 0
        #: Warnings the next placement or modify asks, in order: (message ids, text).
        self.questions: deque[tuple[list[str], str]] = deque()
        #: A refusal (``{"error": ...}``) for the next placement.
        self.reject_next: str | None = None
        #: The next placement is taken, then its answer is lost (a read timeout).
        self.lose_next_answer = False
        #: The next placement is answered with this response instead.
        self.respond_next: httpx.Response | None = None
        #: A declined placement stays listed anyway (the broker kept it).
        self.keep_declined = False
        #: How many live-orders reads the next new order is missing from.
        self.listing_delay = 0
        #: Whether the live-orders answer is a complete snapshot.
        self.snapshot = True
        #: Rewrites the live-orders rows before they are answered (malformed rows, for tests).
        self.rewrite_live_rows: Callable[[list[Any]], list[Any]] | None = None
        #: The next cancel arrives after the order has filled.
        self.fill_before_cancel = False
        #: Cancels are refused with this error, or fail with a 503 when ``"503"``.
        self.cancel_failure: str | None = None
        #: The status endpoint answers 503 (no cached order).
        self.status_unavailable = False
        #: The whatif answer.
        self.whatif_answer: dict[str, Any] = {
            "amount": {
                "amount": "1,977.60 USD (10 Shares)",
                "commission": "1 USD",
                "total": "1,978.60 USD",
            },
            "equity": {"current": "215,415", "change": "-1", "after": "215,414"},
            "initial": {"current": "0", "change": "0", "after": "0"},
            "maintenance": {"current": "0", "change": "0", "after": "0"},
            "position": {"current": "0", "change": "10", "after": "10"},
            "warn": None,
            "error": None,
        }
        self._pending: dict[str, Pending] = {}
        self._used_coids: set[str] = set()
        self._next_id = 1000
        self._mount(router)

    # ── wiring ──

    def _invalidating(
        self, handler: Callable[..., httpx.Response]
    ) -> Callable[..., httpx.Response]:
        """IBKR invalidates an open warning as soon as any other request arrives."""

        def handle(request: httpx.Request, **params: str) -> httpx.Response:
            self._pending.clear()
            return handler(request, **params)

        return handle

    def _mount(self, router: respx.MockRouter) -> None:
        account = rf"{re.escape(BASE)}/iserver/account/(?P<account>[^/]+)"
        other = self._invalidating
        router.post(url__regex=rf"{account}/orders/whatif$").mock(side_effect=other(self._whatif))
        router.post(url__regex=rf"{account}/orders$").mock(side_effect=other(self._place))
        router.post(url__regex=rf"{account}/order/{_ID}$").mock(side_effect=other(self._modify))
        router.delete(url__regex=rf"{account}/order/{_ID}$").mock(side_effect=other(self._cancel))
        router.post(url__regex=rf"{re.escape(BASE)}/iserver/reply/{_ID}$").mock(
            side_effect=self._reply
        )
        router.get(url__regex=rf"{re.escape(BASE)}/iserver/account/orders(\?.*)?$").mock(
            side_effect=other(self._live_orders)
        )
        router.get(url__regex=rf"{re.escape(BASE)}/iserver/account/trades(\?.*)?$").mock(
            side_effect=other(self._trades)
        )
        router.get(url__regex=rf"{re.escape(BASE)}/iserver/account/order/status/{_ID}$").mock(
            side_effect=other(self._status)
        )
        router.post(url=f"{BASE}/iserver/questions/suppress/reset").mock(
            side_effect=other(self._suppress_reset)
        )

    # ── state a test arranges ──

    def add_order(
        self,
        *,
        side: str,
        conid: int = 265598,
        quantity: str = "10",
        coid: str | None = None,
        sec_type: str = "STK",
        status: str = "Submitted",
        order_type: str = "LMT",
    ) -> FakeOrder:
        """An order already working at the broker (placed earlier, or by someone else)."""
        order = FakeOrder(
            order_id=self._new_id(),
            coid=coid,
            conid=conid,
            side=side,
            quantity=Decimal(quantity),
            order_type=order_type,
            price=Decimal("100"),
            aux_price=None,
            tif="GTC",
            sec_type=sec_type,
            status=status,
        )
        self.orders[order.order_id] = order
        if coid is not None:
            self._used_coids.add(coid)
        return order

    def fill(self, order_id: str, quantity: str | None = None, price: str = "100") -> None:
        """Fill (part of) an order and record the trade."""
        order = self.orders[order_id]
        amount = Decimal(quantity) if quantity is not None else order.quantity - order.filled
        order.filled += amount
        order.status = "Filled" if order.filled >= order.quantity else "Submitted"
        self.trades.append(
            {
                "execution_id": f"exec-{order_id}-{len(self.trades)}",
                "side": "B" if order.side == "BUY" else "S",
                "size": float(amount),
                "price": price,
                "order_ref": order.coid,
                "commission": "1.00",
                "conid": order.conid,
                "trade_time_r": 1_760_000_000_000 + len(self.trades),
            }
        )

    def live(self) -> list[FakeOrder]:
        return [o for o in self.orders.values() if o.status not in ("Filled", "Cancelled")]

    # ── handlers ──

    def _new_id(self) -> str:
        self._next_id += 1
        return str(self._next_id)

    @staticmethod
    def _json(request: httpx.Request) -> Any:
        return json.loads(request.content) if request.content else None

    def _question(self, pending: Pending) -> httpx.Response:
        ids, text = pending.questions.popleft()
        reply_id = f"reply-{len(self._pending) + len(self.replies) + 1}"
        self._pending[reply_id] = pending
        return httpx.Response(
            200,
            json=[{"id": reply_id, "message": [text], "isSuppressed": False, "messageIds": ids}],
        )

    def _accept(self, pending: Pending) -> httpx.Response:
        body = pending.body
        if pending.order_id is not None:  # a modify
            order = self.orders[pending.order_id]
            order.quantity = Decimal(str(body.get("quantity", order.quantity)))
            order.price = Decimal(str(body["price"]))
            if "auxPrice" in body:
                order.aux_price = Decimal(str(body["auxPrice"]))
            return httpx.Response(
                200, json=[{"order_id": order.order_id, "order_status": order.status}]
            )
        coid = body.get("cOID")
        conidex = body.get("conidex")
        conid = body.get("conid") or int(str(conidex).split("@")[0])
        order = FakeOrder(
            order_id=self._new_id(),
            coid=coid,
            conid=conid,
            side=body["side"],
            quantity=Decimal(str(body.get("quantity", body.get("cashQty", body.get("fxQty", 0))))),
            order_type=body["orderType"],
            price=Decimal(str(body["price"])),
            aux_price=Decimal(str(body["auxPrice"])) if "auxPrice" in body else None,
            tif=body["tif"],
            sec_type="CASH" if body.get("isCcyConv") else "STK",
            hidden_reads=self.listing_delay,
            body=body,
        )
        self.listing_delay = 0
        self.orders[order.order_id] = order
        if coid is not None:
            self._used_coids.add(coid)
        if self.lose_next_answer:
            self.lose_next_answer = False
            raise httpx.ReadTimeout("the answer was lost after the order was taken")
        return httpx.Response(
            200,
            json=[
                {"order_id": order.order_id, "order_status": "Submitted", "encrypt_message": "1"}
            ],
        )

    def _place(self, request: httpx.Request, account: str) -> httpx.Response:
        body = self._json(request)
        order = body["orders"][0]
        self.placements.append(order)
        if self.respond_next is not None:
            response, self.respond_next = self.respond_next, None
            return response
        if self.reject_next is not None:
            error, self.reject_next = self.reject_next, None
            return httpx.Response(200, json={"error": error})
        if order.get("cOID") in self._used_coids:
            return httpx.Response(
                200,
                json={
                    "error": f"Order couldn't be submitted: Local order ID={order['cOID']} is "
                    "already registered."
                },
            )
        pending = Pending(order_id=None, body=order, questions=self.questions)
        self.questions = deque()
        if pending.questions:
            return self._question(pending)
        return self._accept(pending)

    def _modify(self, request: httpx.Request, account: str, id: str) -> httpx.Response:
        body = self._json(request)
        self.modifies.append((id, body))
        if id not in self.orders:
            return httpx.Response(200, json={"error": f"OrderID {id} doesn't exist"})
        pending = Pending(order_id=id, body=body, questions=self.questions)
        self.questions = deque()
        if pending.questions:
            return self._question(pending)
        return self._accept(pending)

    def _reply(self, request: httpx.Request, id: str) -> httpx.Response:
        confirmed = bool(self._json(request)["confirmed"])
        self.replies.append((id, confirmed))
        pending = self._pending.pop(id, None)
        if pending is None:
            return httpx.Response(503, json={"error": "the reply is no longer valid"})
        if not confirmed:
            if self.keep_declined and pending.order_id is None:
                self.add_order(side=pending.body["side"], coid=pending.body.get("cOID"))
            return httpx.Response(200, json={"msg": "the order was discarded"})
        if pending.questions:
            return self._question(pending)
        return self._accept(pending)

    def _whatif(self, request: httpx.Request, account: str) -> httpx.Response:
        self.whatifs.append(self._json(request)["orders"][0])
        return httpx.Response(200, json=self.whatif_answer)

    def _live_orders(self, request: httpx.Request) -> httpx.Response:
        self.live_order_reads += 1
        rows = []
        for order in self.orders.values():
            if order.hidden_reads > 0:
                order.hidden_reads -= 1
                continue
            rows.append(order.row())
        answer: list[Any] = rows
        if self.rewrite_live_rows is not None:
            answer = self.rewrite_live_rows(rows)
        return httpx.Response(200, json={"orders": answer, "snapshot": self.snapshot})

    def _trades(self, request: httpx.Request) -> httpx.Response:
        self.trade_reads += 1
        return httpx.Response(200, json=self.trades)

    def _status(self, request: httpx.Request, id: str) -> httpx.Response:
        order = self.orders.get(id)
        if order is None or self.status_unavailable:
            return httpx.Response(503, json={"error": "no such order"})
        return httpx.Response(
            200,
            json={
                "order_id": int(order.order_id),
                "conid": order.conid,
                "side": "B" if order.side == "BUY" else "S",
                "order_status": order.status,
                "total_size": str(order.quantity),
                "cum_fill": str(order.filled),
                "size": str(order.quantity - order.filled),
                "order_type": order.order_type,
                "tif": order.tif,
            },
        )

    def _cancel(self, request: httpx.Request, account: str, id: str) -> httpx.Response:
        self.cancels.append(id)
        if self.cancel_failure == "503":
            return httpx.Response(503, json={"error": "service unavailable"})
        if self.cancel_failure is not None:
            return httpx.Response(200, json={"error": self.cancel_failure})
        order = self.orders.get(id)
        if order is None:
            return httpx.Response(200, json={"error": f"OrderID {id} doesn't exist"})
        if self.fill_before_cancel:
            self.fill_before_cancel = False
            self.fill(id)
        if order.status == "Filled":
            return httpx.Response(200, json={"error": f"OrderID {id} is already filled"})
        order.status = "Cancelled"
        return httpx.Response(
            200,
            json={
                "msg": "Request was submitted",
                "order_id": int(id),
                "conid": order.conid,
                "account": account,
            },
        )

    def _suppress_reset(self, request: httpx.Request) -> httpx.Response:
        self.suppress_resets += 1
        return httpx.Response(200, json={"status": "submitted"})
