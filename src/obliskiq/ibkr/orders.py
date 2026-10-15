"""The IBKR order client: whatif, placement with its warnings, modify, cancel, and the order reads.

What it guarantees on its own, whoever calls it:

* **Limit-capped only.** An order body is built from :class:`~obliskiq.domain.orders.OrderTerms`,
  whose every price policy carries a limit; the only order types it can send are ``LMT`` (a
  patient limit, or an IBKR algo capped by its limit) and ``STOP_LIMIT``. There is no way to
  express a plain market order.
* **No write without a clearance (I1).** :meth:`OrderClient.place_idempotent` and
  :meth:`OrderClient.modify` take a :class:`~obliskiq.domain.orders.PretradeClearance` and spend
  it in the ledger — bound to the intent (through the cOID), the action, the broker order and the
  exact terms — before anything is sent. ``cancel`` needs none.
* **Never twice.** The cOID is the intent id (or its deterministic short form when
  ``execution.orders.coid_max_length`` is shorter). Before any placement the cOID is looked up in
  the live orders, which must be a complete snapshot, and in the trades; a found order is not
  placed again. A write is sent once (``retry=False``): one that may have reached the broker is
  reported as ``unknown``, and the next attempt looks the cOID up first.
* **Warnings are answered by policy.** Each warning (``/iserver/reply``) is put to the caller's
  decider; anything it does not confirm is declined, the cOID is looked up and any listed order
  cancelled. IBKR requires the answer before any other request, so one placement runs at a time.
* **The paper guard before every write.** Under profile ``paper`` the account must carry a paper
  prefix, re-checked before each write.

Endpoints, fields and message ids: docs/knowledge/ibkr-orders.md (from IBKR's reference and
ibind; VERIFY-LIVE until a paper login places one).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Protocol
from uuid import UUID

from ..config.schema import ObliskConfig
from ..domain.orders import (
    BrokerOrder,
    ClearanceAction,
    Fill,
    OrderTerms,
    PretradeClearance,
)
from ..errors import WriteRefusedError
from ..logging_setup import get_logger
from ..util.timeutil import utc_now
from .errors import (
    AuthError,
    CircuitOpenError,
    IbkrError,
    LookupIncompleteError,
    RateLimitedError,
    RequestRejectedError,
    SessionExpiredError,
    TransportUnavailableError,
)
from .guard import AccountGuard
from .order_decode import (
    CancelAnswer,
    LiveOrderRows,
    LiveOrders,
    PlaceAnswer,
    Placed,
    Question,
    Refused,
    UnexpectedAnswer,
    WhatIf,
    decode_cancel,
    decode_live_order_rows,
    decode_live_orders,
    decode_order_status,
    decode_place_answer,
    decode_trades,
    decode_whatif,
)
from .session import BrokerSession

log = get_logger("ibkr.orders")

PLACE_PATH = "iserver/account/{account}/orders"
WHATIF_PATH = "iserver/account/{account}/orders/whatif"
ORDER_PATH = "iserver/account/{account}/order/{order_id}"
REPLY_PATH = "iserver/reply/{reply_id}"
LIVE_ORDERS_PATH = "iserver/account/orders"
TRADES_PATH = "iserver/account/trades"
STATUS_PATH = "iserver/account/order/status/{order_id}"
SUPPRESS_RESET_PATH = "iserver/questions/suppress/reset"

#: The order types an order body can carry. Each is capped by a limit.
LIMIT_CAPPED_ORDER_TYPES: Final = frozenset({"LMT", "STOP_LIMIT"})

#: The router that needs no ``conidex``: an order for any other venue is routed with one.
SMART_ROUTER = "SMART"

#: Broker statuses after which an order can no longer trade (IBKR, "Order Status Value"),
#: compared in lower case.
FILLED_STATUS: Final = "filled"
CANCELLED_STATUS: Final = "cancelled"
FINAL_BROKER_STATUSES: Final = frozenset({FILLED_STATUS, CANCELLED_STATUS})


def coid_for(intent_id: UUID, max_length: int) -> str:
    """The cOID IBKR sees for an intent: its id, or a deterministic short form of it.

    The short form is the id's 16 bytes in lower-case base 32 (26 characters), cut to
    ``max_length`` when that is shorter; the store keeps the mapping and refuses a collision.
    """
    text = str(intent_id)
    if len(text) <= max_length:
        return text
    return base64.b32encode(intent_id.bytes).decode("ascii").rstrip("=").lower()[:max_length]


def is_live(order: BrokerOrder) -> bool:
    """Whether an order can still trade: anything not filled or cancelled, unknown ones too."""
    return order.status.strip().lower() not in FINAL_BROKER_STATUSES


def _number(value: Decimal) -> int | float:
    """A JSON number for an amount: whole amounts as integers."""
    return int(value) if value == value.to_integral_value() else float(value)


def order_body(terms: OrderTerms, *, coid: str, account_id: str) -> dict[str, Any]:
    """The order as IBKR's ``orders`` endpoint takes it. Every order type it emits is capped."""
    price = terms.price
    body: dict[str, Any] = {"acctId": account_id, "cOID": coid, "side": terms.side.value}
    if terms.venue == SMART_ROUTER:
        body["conid"] = terms.conid
    else:
        body["conidex"] = f"{terms.conid}@{terms.venue}"
    body["tif"] = terms.tif.value
    body["outsideRTH"] = False
    if price.kind == "stop_limit":
        body["orderType"] = "STOP_LIMIT"
        body["price"] = _number(price.limit)
        body["auxPrice"] = _number(price.stop)
    else:
        body["orderType"] = "LMT"
        body["price"] = _number(price.limit)
        if price.kind == "algo":
            body["strategy"] = price.strategy
            body["strategyParameters"] = {p.name: p.value for p in price.parameters}
    if terms.conversion:
        if terms.cash_quantity is None:  # pragma: no cover - OrderTerms refuses it
            raise ValueError("a conversion is sized in cash")
        body["isCcyConv"] = True
        body["fxQty"] = _number(terms.cash_quantity)
    elif terms.quantity is not None:
        body["quantity"] = _number(terms.quantity)
    elif terms.cash_quantity is not None:
        body["cashQty"] = _number(terms.cash_quantity)
    if body["orderType"] not in LIMIT_CAPPED_ORDER_TYPES:  # pragma: no cover - by construction
        raise ValueError(f"order type {body['orderType']} is not limit-capped")
    return body


@dataclass(frozen=True, slots=True)
class ReplyVerdict:
    """The decider's answer to one warning."""

    confirm: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ReplyRecord:
    """One warning and how it was answered."""

    reply_id: str
    message_ids: tuple[str, ...]
    confirmed: bool
    reason: str


type ReplyDecider = Callable[[Question], ReplyVerdict]
type LastCheck = Callable[[], Awaitable[None]]


class ClearanceLedger(Protocol):
    """Where clearances are spent: once, for exactly the write they were issued for."""

    async def spend(
        self,
        clearance: PretradeClearance,
        *,
        action: ClearanceAction,
        order_id: str | None,
        terms: OrderTerms,
    ) -> None:
        """Spend ``clearance`` for this write, or raise :class:`WriteRefusedError`."""
        ...


class PlacementStatus(StrEnum):
    """How a placement or a modify ended."""

    PLACED = "placed"  # the broker took it; the order id is known
    ALREADY_PLACED = "already_placed"  # the cOID lookup found it; nothing was sent
    DUPLICATE = "duplicate"  # the broker refused the cOID as already used: the order exists
    REJECTED = "rejected"  # the broker refused the order
    DECLINED = "declined"  # a warning the policy does not confirm: declined, then cancelled
    NOT_SENT = "not_sent"  # refused before the broker processed it
    UNKNOWN = "unknown"  # it may have reached the broker: look the cOID up before anything else


@dataclass(frozen=True, slots=True)
class Lookup:
    """What the broker lists under one cOID."""

    orders: tuple[BrokerOrder, ...]
    fills: tuple[Fill, ...]

    @property
    def found(self) -> bool:
        return bool(self.orders or self.fills)


@dataclass(frozen=True, slots=True)
class PlacementResult:
    status: PlacementStatus
    coid: str
    order_id: str | None = None
    order_status: str | None = None
    replies: tuple[ReplyRecord, ...] = ()
    detail: str = ""
    lookup: Lookup | None = None
    #: For a declined placement: whether the broker was seen to list nothing working under the
    #: cOID afterwards (nothing listed, or every listed order cancelled).
    cleanup_verified: bool = True


@dataclass(frozen=True, slots=True)
class _NotSent:
    detail: str


class OrderClient:
    """The order endpoints for the guarded account."""

    def __init__(
        self,
        session: BrokerSession,
        settings: ObliskConfig,
        guard: AccountGuard,
        ledger: ClearanceLedger,
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._orders = settings.execution.orders
        self._guard = guard
        self._ledger = ledger
        self._now = now
        # IBKR answers a warning only before any other request: one placement at a time.
        self._submitting = asyncio.Lock()
        session.add_known_ids([guard.account_id])

    def coid_for(self, intent_id: UUID) -> str:
        return coid_for(intent_id, self._orders.coid_max_length)

    @staticmethod
    def _order_id(order_id: str) -> str:
        """An IBKR order id: digits only. ``-1`` (cancel every open order) is never sent."""
        if not order_id.isdigit():
            raise WriteRefusedError("order_id_invalid", f"{order_id!r} is not an order id")
        return order_id

    def _account(self) -> str:
        """The account for a write, after the paper guard (profile paper ⇒ paper prefix)."""
        self._guard.check_prefix()
        return self._guard.account_id

    async def _write(self, method: str, path: str, label: str, body: Any = None) -> Any:
        self._account()
        return await self._session.request(method, path, json_body=body, label=label, retry=False)

    # ── reads ──

    async def whatif(self, terms: OrderTerms, *, coid: str) -> WhatIf:
        """Preview an order: cost, equity and margin effect, and IBKR's warning, if any."""
        account = self._account()
        body = {"orders": [order_body(terms, coid=coid, account_id=account)]}
        answer = await self._session.request(
            "POST",
            WHATIF_PATH.format(account=account),
            json_body=body,
            label=WHATIF_PATH,
        )
        return decode_whatif(answer)

    async def live_orders(self) -> LiveOrders:
        body = await self._session.request("GET", LIVE_ORDERS_PATH)
        return decode_live_orders(body, observed_at=self._now())

    async def trades(self) -> tuple[Fill, ...]:
        body = await self._session.request(
            "GET", TRADES_PATH, params={"days": self._orders.trades_lookup_days}
        )
        return decode_trades(body)

    async def live_order_rows(self) -> LiveOrderRows:
        """The live orders row by row, for the kill switch's sweep."""
        body = await self._session.request("GET", LIVE_ORDERS_PATH)
        return decode_live_order_rows(body, observed_at=self._now())

    async def status(self, order_id: str) -> BrokerOrder:
        body = await self._session.request(
            "GET", STATUS_PATH.format(order_id=self._order_id(order_id)), label=STATUS_PATH
        )
        return decode_order_status(body, observed_at=self._now())

    async def find(self, coid: str) -> Lookup:
        """Every order and fill the broker lists under ``coid``.

        Raises :class:`LookupIncompleteError` when the live orders are not a complete snapshot.
        """
        live = await self.live_orders()
        if not live.complete:
            raise LookupIncompleteError(
                "the live orders are not a complete snapshot; the cOID lookup proves nothing"
            )
        fills = await self.trades()
        return Lookup(
            orders=tuple(order for order in live.orders if order.coid == coid),
            fills=tuple(fill for fill in fills if fill.coid == coid),
        )

    # ── writes ──

    async def reset_suppressed(self) -> None:
        """Re-enable every warning a user suppressed, so none is accepted without the policy."""
        await self._write("POST", SUPPRESS_RESET_PATH, SUPPRESS_RESET_PATH)

    async def cancel(self, order_id: str) -> CancelAnswer:
        """Cancel one order. Needs no clearance; the paper guard still applies."""
        order_id = self._order_id(order_id)
        account = self._account()
        body = await self._write(
            "DELETE", ORDER_PATH.format(account=account, order_id=order_id), ORDER_PATH
        )
        return decode_cancel(body)

    async def reply(self, reply_id: str, *, confirmed: bool) -> PlaceAnswer:
        body = await self._write(
            "POST",
            REPLY_PATH.format(reply_id=reply_id),
            REPLY_PATH,
            {"confirmed": confirmed},
        )
        return decode_place_answer(body)

    def _check_binding(
        self, clearance: PretradeClearance, *, coid: str, action: ClearanceAction
    ) -> None:
        """The clearance is for this intent (by its cOID) and this action, before any read."""
        if self.coid_for(clearance.intent_id) != coid:
            raise WriteRefusedError("clearance_mismatch", "the clearance is for another intent")
        if clearance.action is not action:
            raise WriteRefusedError(
                "clearance_mismatch", f"the clearance is for a {clearance.action.value}"
            )

    async def place_idempotent(
        self,
        *,
        coid: str,
        terms: OrderTerms,
        clearance: PretradeClearance,
        decide: ReplyDecider,
        last_check: LastCheck,
    ) -> PlacementResult:
        """Place an order unless the broker already lists its cOID.

        Before anything is sent: the paper guard, the clearance's binding, the cOID lookup (a
        found order is reported, not placed, and the clearance kept), ``last_check`` (the caller's
        final gate, run immediately before the send), and the clearance spent for exactly these
        terms. A raise means nothing was sent.
        """
        account = self._account()
        self._check_binding(clearance, coid=coid, action=ClearanceAction.PLACE)
        async with self._submitting:
            lookup = await self.find(coid)
            if lookup.found:
                listed = lookup.orders[0] if lookup.orders else None
                return PlacementResult(
                    status=PlacementStatus.ALREADY_PLACED,
                    coid=coid,
                    order_id=None if listed is None else listed.order_id,
                    order_status=None if listed is None else listed.status,
                    detail="the broker already lists this cOID; nothing was sent",
                    lookup=lookup,
                )
            await last_check()
            await self._ledger.spend(
                clearance, action=ClearanceAction.PLACE, order_id=None, terms=terms
            )
            body = {"orders": [order_body(terms, coid=coid, account_id=account)]}
            answer = await self._submit(
                "POST", PLACE_PATH.format(account=account), PLACE_PATH, body
            )
            return await self._resolve(answer, coid=coid, decide=decide, cancel_on_decline=True)

    async def modify(
        self,
        *,
        order_id: str,
        coid: str,
        terms: OrderTerms,
        clearance: PretradeClearance,
        decide: ReplyDecider,
        last_check: LastCheck,
    ) -> PlacementResult:
        """Change a working order to ``terms``; the clearance must name this order and terms."""
        order_id = self._order_id(order_id)
        account = self._account()
        self._check_binding(clearance, coid=coid, action=ClearanceAction.MODIFY)
        async with self._submitting:
            await last_check()
            await self._ledger.spend(
                clearance, action=ClearanceAction.MODIFY, order_id=order_id, terms=terms
            )
            body = order_body(terms, coid=coid, account_id=account)
            answer = await self._submit(
                "POST", ORDER_PATH.format(account=account, order_id=order_id), ORDER_PATH, body
            )
            # A declined modify leaves the order as it was: cancelling it could strip a
            # position of its protective stop.
            return await self._resolve(answer, coid=coid, decide=decide, cancel_on_decline=False)

    async def _submit(
        self, method: str, path: str, label: str, body: Any
    ) -> PlaceAnswer | _NotSent:
        """Send one write once and classify what came back."""
        try:
            answer = await self._write(method, path, label, body)
        except (
            CircuitOpenError,
            TransportUnavailableError,
            SessionExpiredError,
            RateLimitedError,
        ) as exc:
            # Refused before the order was processed: nothing exists at the broker.
            return _NotSent(self._session.redact(str(exc)))
        except (RequestRejectedError, AuthError) as exc:
            return Refused(error=self._session.redact(str(exc)), duplicate=False)
        except IbkrError as exc:
            # Any 5xx (a no-data body included), a transport failure mid-flight or an
            # unreadable body: it may exist.
            return UnexpectedAnswer(self._session.redact(str(exc)))
        return decode_place_answer(answer)

    async def _resolve(
        self,
        answer: PlaceAnswer | _NotSent,
        *,
        coid: str,
        decide: ReplyDecider,
        cancel_on_decline: bool,
    ) -> PlacementResult:
        """Follow a placement through its warnings to an outcome."""
        replies: list[ReplyRecord] = []
        while True:
            match answer:
                case Placed(order_id=order_id, order_status=order_status):
                    return PlacementResult(
                        status=PlacementStatus.PLACED,
                        coid=coid,
                        order_id=order_id,
                        order_status=order_status,
                        replies=tuple(replies),
                    )
                case Refused(error=error, duplicate=True):
                    return PlacementResult(
                        status=PlacementStatus.DUPLICATE,
                        coid=coid,
                        replies=tuple(replies),
                        detail=error,
                    )
                case Refused(error=error):
                    return PlacementResult(
                        status=PlacementStatus.REJECTED,
                        coid=coid,
                        replies=tuple(replies),
                        detail=error,
                    )
                case _NotSent(detail=detail):
                    return PlacementResult(
                        status=PlacementStatus.NOT_SENT,
                        coid=coid,
                        replies=tuple(replies),
                        detail=detail,
                    )
                case UnexpectedAnswer(detail=detail):
                    return PlacementResult(
                        status=PlacementStatus.UNKNOWN,
                        coid=coid,
                        replies=tuple(replies),
                        detail=detail,
                    )
                case _:  # a warning
                    if len(replies) >= self._orders.max_reply_rounds:
                        verdict = ReplyVerdict(False, "more warning rounds than allowed")
                    else:
                        verdict = decide(answer)
                    replies.append(
                        ReplyRecord(
                            reply_id=answer.reply_id,
                            message_ids=answer.message_ids,
                            confirmed=verdict.confirm,
                            reason=verdict.reason,
                        )
                    )
                    if not verdict.confirm:
                        detail, verified = await self._decline(
                            answer.reply_id, coid, cancel_listed=cancel_on_decline
                        )
                        return PlacementResult(
                            status=PlacementStatus.DECLINED,
                            coid=coid,
                            replies=tuple(replies),
                            detail=f"{verdict.reason}; {detail}",
                            cleanup_verified=verified,
                        )
                    answer = await self._submit(
                        "POST",
                        REPLY_PATH.format(reply_id=answer.reply_id),
                        REPLY_PATH,
                        {"confirmed": True},
                    )

    async def _decline(self, reply_id: str, coid: str, *, cancel_listed: bool) -> tuple[str, bool]:
        """Decline a warning; after a placement, cancel anything listed under the cOID.

        Returns what happened and whether nothing working is known to remain at the broker.
        """
        try:
            await self.reply(reply_id, confirmed=False)
        except IbkrError as exc:
            log.warning(
                "declining a warning failed", extra={"error": self._session.redact(str(exc))}
            )
        if not cancel_listed:
            return "declined; the order stands as it was before the modify", True
        try:
            lookup = await self.find(coid)
        except IbkrError as exc:
            return (
                f"declined; the cOID could not be looked up afterwards ({type(exc).__name__})",
                False,
            )
        live = [order for order in lookup.orders if is_live(order)]
        if not live:
            return "declined; nothing is listed under the cOID", True
        failures = []
        for order in live:
            try:
                answer = await self.cancel(order.order_id)
            except (IbkrError, WriteRefusedError) as exc:
                failures.append(f"{order.order_id} ({type(exc).__name__})")
                continue
            if answer.error is not None:
                failures.append(f"{order.order_id} ({answer.error})")
        if failures:
            return f"declined; cancelling the listed order failed: {', '.join(failures)}", False
        return f"declined; cancelled {len(live)} listed order(s)", True
