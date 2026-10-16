"""No short, ever (I2): the long position and every sell that could still reach the broker.

Pure functions over what the gate read; the gate runs them inside the store transaction that
records the clearance, so two processes clearing one instrument see each other's clearances.

* **The position** is the lower of the broker's fresh read and the store's. The store's is its
  latest positions read when the instrument has no fill in the trades window; with fills, every
  read from the last one before the first of them onwards, each plus the fills reported after it
  (a sell in the same second counts, a buy does not), and the lowest of those. A stale broker read
  after a fill therefore cannot raise the position: the read before the fill, less the fill,
  brings it down.
* **The sells** counted against it, for the instrument:
  - every working sell the broker lists (its remaining quantity), at the largest size a modify
    could still leave it: an unspent modify clearance, or the latest spent one, may not have
    reached the broker yet;
  - every sell cleared but not listed by the broker, in any process: an unspent clearance until
    it expires, and a spent one until the broker lists its cOID or its intent ends — one per
    intent, less what it filled, since a cOID exists at the broker once;
  - the order being cleared, once: a placement at its quantity, a modify at its new quantity
    less what the order has filled (the modified order is not counted again).
* A conversion is bounded by the cash check, not this one.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from ..domain.orders import BrokerOrder, ClearanceAction, Fill, OrderSide, Position
from ..ibkr.orders import is_live
from ..store.orders import AccountRead, OpenClearance
from .killswitch import CONVERSION_SEC_TYPE

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class SellRequest:
    """The sell being cleared: a placement for ``intent_id``, or a modify of ``order_id``."""

    intent_id: UUID
    coid: str
    conid: int
    quantity: Decimal
    order_id: str | None = None


@dataclass(frozen=True, slots=True)
class SellCount:
    """Every sell counted against the position, and how."""

    total: Decimal
    parts: tuple[str, ...] = field(default=())


class UnreadableOrdersError(ValueError):
    """A live order the count needs cannot be read: nothing proves the sells fit."""


def store_position(
    reads: Sequence[AccountRead[Position]], fills: Sequence[Fill], conid: int
) -> Decimal | None:
    """The position the store's reads and the fills reported after them imply; ``None`` without
    a read. ``reads`` are oldest first.

    Without a fill of the instrument in the window, the latest read is the store's position.
    With fills, every read from the last one before the first of those fills onwards counts,
    each with the fills reported after it, and the lowest wins: a read taken after a fill that
    does not show it yet cannot raise the position.
    """
    if not reads:
        return None
    own = [fill for fill in fills if fill.conid == conid]
    if not own:
        anchors: Sequence[AccountRead[Position]] = reads[-1:]
    else:
        first = min(fill.executed_at for fill in own)
        before = [index for index, read in enumerate(reads) if read.observed_at <= first]
        anchors = reads[before[-1] :] if before else reads
    lowest: Decimal | None = None
    for read in anchors:
        quantity = sum((row.quantity for row in read.rows if row.conid == conid), ZERO)
        for fill in own:
            if fill.side is OrderSide.SELL and fill.executed_at >= read.observed_at:
                quantity -= fill.quantity
            elif fill.side is OrderSide.BUY and fill.executed_at > read.observed_at:
                quantity += fill.quantity
        lowest = quantity if lowest is None else min(lowest, quantity)
    return lowest


def _remaining(order: BrokerOrder) -> Decimal:
    if order.remaining_quantity is not None:
        return order.remaining_quantity
    if order.total_quantity is None:
        raise UnreadableOrdersError(f"order {order.order_id} reports no size")
    return order.total_quantity - (order.filled_quantity or ZERO)


def _modify_sizes(
    order_id: str, clearances: Sequence[OpenClearance], now: datetime
) -> list[Decimal]:
    """The sizes the order's modify clearances could still leave it at: each unspent one before
    it expires, and the latest spent one."""
    sizes: list[Decimal] = []
    latest: tuple[datetime, Decimal | None] | None = None
    for entry in clearances:
        clearance = entry.clearance
        if clearance.action is not ClearanceAction.MODIFY or clearance.order_id != order_id:
            continue
        quantity = clearance.terms.quantity
        if entry.used_at is None:
            if clearance.expires_at > now and quantity is not None:
                sizes.append(quantity)
        elif latest is None or entry.used_at >= latest[0]:
            latest = (entry.used_at, quantity)
    if latest is not None and latest[1] is not None:
        sizes.append(latest[1])
    return sizes


def count_sells(
    request: SellRequest,
    *,
    live: Sequence[BrokerOrder],
    clearances: Sequence[OpenClearance],
    filled_by_coid: Mapping[str, Decimal],
    now: datetime,
) -> SellCount:
    """Every sell of the instrument that is working or could still reach the broker, with the
    request. Raises :class:`UnreadableOrdersError` when a live order cannot be judged."""
    conid = request.conid
    parts: list[str] = []
    total = ZERO
    listed = {order.coid for order in live if order.coid is not None}
    sells = [c for c in clearances if c.clearance.terms.conid == conid]
    sells = [c for c in sells if c.clearance.terms.side is OrderSide.SELL]
    target_filled = ZERO
    for order in live:
        if not is_live(order):
            continue
        if order.conid is None or order.side is None:
            raise UnreadableOrdersError(f"order {order.order_id} reports no instrument or side")
        if (
            order.conid != conid
            or order.side is not OrderSide.SELL
            or (order.sec_type or "").upper() == CONVERSION_SEC_TYPE
        ):
            continue
        filled = order.filled_quantity or ZERO
        if order.order_id == request.order_id:
            target_filled = filled
            continue
        if order.coid is not None and order.coid == request.coid:
            continue  # the request's own order: counted once, below
        sizes = [_remaining(order)]
        sizes += [size - filled for size in _modify_sizes(order.order_id, sells, now)]
        largest = max(sizes)
        total += largest
        parts.append(f"working sell {order.order_id}: {largest}")
    in_flight: dict[UUID, tuple[str, Decimal]] = {}
    for entry in sells:
        clearance = entry.clearance
        if (
            clearance.action is not ClearanceAction.PLACE
            or clearance.intent_id == request.intent_id
        ):
            continue
        if entry.coid in listed or clearance.terms.quantity is None:
            continue  # listed: the broker's row counts it
        if entry.used_at is None and clearance.expires_at <= now:
            continue  # expired unspent: it can no longer be sent
        held = in_flight.get(clearance.intent_id)
        quantity = clearance.terms.quantity
        if held is None or quantity > held[1]:
            in_flight[clearance.intent_id] = (entry.coid, quantity)
    for coid, quantity in in_flight.values():
        open_quantity = max(quantity - filled_by_coid.get(coid, ZERO), ZERO)
        total += open_quantity
        parts.append(f"cleared, not listed ({coid}): {open_quantity}")
    own = request.quantity - target_filled
    if request.order_id is not None:
        pending = _modify_sizes(request.order_id, sells, now)
        own = max([own, *(size - target_filled for size in pending)])
    total += own
    parts.append(f"this order: {own}")
    return SellCount(total=total, parts=tuple(parts))
