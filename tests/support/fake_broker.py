"""A stateful fake of IBKR's order endpoints, mocked with respx.

It keeps orders, warnings, trades and cancels the way the Client Portal Web API describes them
(docs/knowledge/ibkr-orders.md) and lets a test script the awkward cases: warnings to answer, a
refusal, a reused cOID, an answer lost after the order was taken, an order listed late, a
live-orders list that is not a complete snapshot, a fill that races a cancel. Session 4a adds
what the pre-trade gate reads: the session and account endpoints, positions (which a fill moves,
unless a test keeps them stale), the ledger, snapshots, contract rules, algos and the contract
search. Session 5 extends it. Like IBKR, any request other than the reply invalidates an open
warning. Shapes are IBKR's documented ones, not live captures (VERIFY-LIVE).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
import respx

BASE = "https://gw.test/v1/api"
ACCOUNT = "DUTEST1"

#: Snapshot field ids (config/defaults/ibkr.yaml): last, bid, ask, availability, the
#: underlying's 30-day historical volatility, an option's own implied volatility.
LAST, BID, ASK, AVAILABILITY, HISTORICAL_VOL, OPTION_IV = "31", "84", "86", "6509", "7087", "7633"

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
        #: Answers each whatif from its order body instead of ``whatif_answer``.
        self.whatif_for: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        #: The clock the broker stamps executions with (the test's wall clock, when set).
        self.trade_clock: Callable[[], datetime] | None = None
        #: Positions by conid (units), with their currency.
        self.positions: dict[int, tuple[Decimal, str]] = {}
        #: Fills leave the positions as they were (a stale portfolio cache).
        self.stale_positions = False
        #: Cash per currency: (cash, rate to the base currency). The base currency is EUR.
        self.cash: dict[str, tuple[Decimal, Decimal | None]] = {"EUR": (Decimal(10000), None)}
        self.nav: Decimal | None = Decimal(100000)
        #: Currencies the ledger lists without a readable cash balance.
        self.unreadable_cash: set[str] = set()
        #: Snapshot rows by conid, keyed by field id.
        self.quotes: dict[int, dict[str, Any]] = {}
        #: The positions, ledger or snapshot endpoint answers 503.
        self.unavailable: set[str] = set()
        #: Contract search: (symbol, listing exchange, currency) by conid.
        self.contracts: dict[int, tuple[str, str, str]] = {}
        self.rules_answer: dict[str, Any] = {
            "orderTypes": ["limit", "stop_limit"],
            "sizeIncrement": 1,
            "incrementRules": [{"lowerEdge": 0.0, "increment": 0.01}],
        }
        self.algos_answer: dict[str, Any] = {"algos": [{"id": "Adaptive", "name": "Adaptive"}]}
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
        router.post(url=f"{BASE}/iserver/auth/ssodh/init").mock(
            return_value=httpx.Response(200, json={"authenticated": True, "competing": False})
        )
        router.get(url=f"{BASE}/iserver/accounts").mock(
            return_value=httpx.Response(200, json={"accounts": [ACCOUNT]})
        )
        router.get(url=f"{BASE}/portfolio/accounts").mock(
            return_value=httpx.Response(
                200, json=[{"id": ACCOUNT, "accountId": ACCOUNT, "currency": "EUR"}]
            )
        )
        portfolio = rf"{re.escape(BASE)}/portfolio/{re.escape(ACCOUNT)}"
        router.get(url__regex=rf"{portfolio}/positions/(?P<page>[0-9]+)$").mock(
            side_effect=other(self._positions)
        )
        router.get(url=f"{BASE}/portfolio/{ACCOUNT}/ledger").mock(side_effect=other(self._ledger))
        router.get(url__regex=rf"{re.escape(BASE)}/iserver/marketdata/snapshot(\?.*)?$").mock(
            side_effect=other(self._snapshot)
        )
        router.post(url=f"{BASE}/iserver/contract/rules").mock(side_effect=other(self._rules))
        router.get(url__regex=rf"{re.escape(BASE)}/iserver/contract/{_ID}/algos(\?.*)?$").mock(
            side_effect=other(self._algos)
        )
        router.post(url=f"{BASE}/iserver/secdef/search").mock(side_effect=other(self._search))
        router.get(url__regex=rf"{re.escape(BASE)}/iserver/contract/{_ID}/info$").mock(
            side_effect=other(self._info)
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

    def hold(self, conid: int, quantity: str, currency: str = "USD") -> None:
        """A position the account holds."""
        self.positions[conid] = (Decimal(quantity), currency)

    def quote(
        self,
        conid: int,
        *,
        bid: str = "99.9",
        ask: str = "100.1",
        last: str = "100",
        availability: str = "RP",
        sigma: str | None = "30%",
        updated_ms: int | None = None,
        option: bool = False,
    ) -> None:
        """The snapshot the broker answers for ``conid`` (``availability``: IBKR's 6509 code)."""
        row: dict[str, Any] = {"conid": conid, LAST: last, BID: bid, ASK: ask}
        row[AVAILABILITY] = availability
        if sigma is not None:
            row[OPTION_IV if option else HISTORICAL_VOL] = sigma
        if updated_ms is not None:
            row["_updated"] = updated_ms
        self.quotes[conid] = row

    def fill(self, order_id: str, quantity: str | None = None, price: str = "100") -> None:
        """Fill (part of) an order and record the trade; the position moves unless stale."""
        order = self.orders[order_id]
        amount = Decimal(quantity) if quantity is not None else order.quantity - order.filled
        order.filled += amount
        order.status = "Filled" if order.filled >= order.quantity else "Submitted"
        if not self.stale_positions and order.sec_type != "CASH":
            held, currency = self.positions.get(order.conid, (Decimal(0), "USD"))
            signed = amount if order.side == "BUY" else -amount
            self.positions[order.conid] = (held + signed, currency)
        self.trades.append(
            {
                "execution_id": f"exec-{order_id}-{len(self.trades)}",
                "side": "B" if order.side == "BUY" else "S",
                "size": float(amount),
                "price": price,
                "order_ref": order.coid,
                "commission": "1.00",
                "conid": order.conid,
                "trade_time_r": (
                    1_760_000_000_000 + len(self.trades)
                    if self.trade_clock is None
                    else int(self.trade_clock().timestamp() * 1000)
                ),
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
        order = self._json(request)["orders"][0]
        self.whatifs.append(order)
        if self.whatif_for is not None:
            return httpx.Response(200, json=self.whatif_for(order))
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

    # ── what the pre-trade gate reads ──

    def _down(self, name: str) -> httpx.Response | None:
        if name in self.unavailable:
            return httpx.Response(503, json={"error": f"{name} unavailable"})
        return None

    def _positions(self, request: httpx.Request, page: str) -> httpx.Response:
        down = self._down("positions")
        if down is not None:
            return down
        if page != "0":
            return httpx.Response(200, json=[])
        rows = [
            {
                "acctId": ACCOUNT,
                "conid": conid,
                "position": float(quantity),
                "currency": currency,
                "assetClass": "STK",
            }
            for conid, (quantity, currency) in sorted(self.positions.items())
        ]
        return httpx.Response(200, json=rows)

    def _ledger(self, request: httpx.Request) -> httpx.Response:
        down = self._down("ledger")
        if down is not None:
            return down
        body: dict[str, Any] = {}
        base: dict[str, Any] = {"currency": "BASE", "secondkey": "BASE", "exchangerate": 1}
        base["cashbalance"] = float(sum(cash for cash, _ in self.cash.values()))
        if self.nav is not None:
            base["netliquidationvalue"] = float(self.nav)
        body["BASE"] = base
        for currency, (cash, rate) in self.cash.items():
            row: dict[str, Any] = {"currency": currency, "cashbalance": str(cash)}
            row["exchangerate"] = 1 if currency == "EUR" else (None if rate is None else str(rate))
            body[currency] = row
        for currency in self.unreadable_cash:
            body[currency] = {"currency": currency, "cashbalance": None}
        return httpx.Response(200, json=body)

    def _snapshot(self, request: httpx.Request) -> httpx.Response:
        down = self._down("snapshot")
        if down is not None:
            return down
        conids = [int(c) for c in request.url.params.get("conids", "").split(",") if c]
        return httpx.Response(200, json=[self.quotes[c] for c in conids if c in self.quotes])

    def _rules(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=self.rules_answer)

    def _algos(self, request: httpx.Request, id: str) -> httpx.Response:
        return httpx.Response(200, json=self.algos_answer)

    def _search(self, request: httpx.Request) -> httpx.Response:
        symbol = self._json(request)["symbol"]
        rows = [
            {
                "conid": str(conid),
                "symbol": sym,
                "description": exchange,
                "sections": [{"secType": "STK"}],
            }
            for conid, (sym, exchange, _) in self.contracts.items()
            if sym == symbol
        ]
        return httpx.Response(200, json=rows)

    def _info(self, request: httpx.Request, id: str) -> httpx.Response:
        found = self.contracts.get(int(id))
        if found is None:
            return httpx.Response(200, json={"error": "no such contract"})
        symbol, exchange, currency = found
        return httpx.Response(
            200,
            json={
                "con_id": int(id),
                "symbol": symbol,
                "currency": currency,
                "valid_exchanges": f"SMART,{exchange}",
            },
        )
