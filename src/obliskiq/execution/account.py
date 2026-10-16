"""What the pre-trade gate reads from the broker before it clears an order.

The gate decides on fresh reads, never on a cache: the positions (every page), the live orders (a
complete snapshot), the trades IBKR serves, the ledger (cash per currency, its rate to the base
currency and the NAV), the quotes of the instrument and, for an option, its underlying, and a
whatif of the order. :class:`AccountReader` is that surface; :class:`IbkrAccountReader` reads it
through Session 3's read client and Session 4's order client. A read that is incomplete raises;
the gate refuses on it.

Quotes become :class:`~obliskiq.domain.orders.QuoteEvidence`: stamped no later than IBKR's own
update time, with a previous close dropped as a price (it is no quote) and the volatility the
collar widens with — the option's own implied volatility, else the underlying's 30-day
historical volatility, else its implied volatility.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from ..config.schema import ObliskConfig
from ..domain.orders import Fill, OrderTerms, Position, QuoteEvidence
from ..ibkr.client import IbkrClient
from ..ibkr.errors import DecodeError
from ..ibkr.models import LastKind, Quote
from ..ibkr.order_decode import LiveOrders, WhatIf
from ..ibkr.orders import OrderClient, coid_for
from ..util.timeutil import utc_now


@dataclass(frozen=True, slots=True)
class PositionsRead:
    """Every position of the account, as read at ``observed_at``."""

    positions: tuple[Position, ...]
    observed_at: datetime

    def quantity(self, conid: int) -> Decimal:
        """The position in ``conid``: zero when the read holds none (a complete read)."""
        return sum((p.quantity for p in self.positions if p.conid == conid), Decimal(0))


@dataclass(frozen=True, slots=True)
class CashLine:
    """Cash in one currency and what one unit of it is worth in the base currency."""

    currency: str
    cash: Decimal
    rate_to_base: Decimal | None


@dataclass(frozen=True, slots=True)
class CashRead:
    """The ledger: cash per currency, and the NAV in the base currency (``None``: refused)."""

    lines: tuple[CashLine, ...]
    nav: Decimal | None
    nav_refusal: str | None
    observed_at: datetime

    def line(self, currency: str) -> CashLine | None:
        return next((line for line in self.lines if line.currency == currency), None)


@dataclass(frozen=True, slots=True)
class QuoteRead:
    """A quote as evidence, the volatility the collar may widen with, and whether IBKR reports
    the instrument halted."""

    evidence: QuoteEvidence
    sigma: float | None
    halted: bool


class AccountReader(Protocol):
    """The broker reads the gate makes."""

    async def positions(self) -> PositionsRead: ...

    async def cash(self) -> CashRead: ...

    async def live_orders(self) -> LiveOrders: ...

    async def trades(self) -> tuple[Fill, ...]: ...

    async def quote(self, conid: int, *, option: bool) -> QuoteRead | None: ...

    async def whatif(self, terms: OrderTerms) -> WhatIf: ...


def _price(value: float | None) -> Decimal | None:
    if value is None or value <= 0:
        return None
    return Decimal(repr(value))


def _sigma(*candidates: float | None) -> float | None:
    return next((value for value in candidates if value is not None and value > 0), None)


def quote_read(quote: Quote, *, option: bool, read_at: datetime) -> QuoteRead:
    """``quote`` as evidence: stamped at the earlier of IBKR's update and the read."""
    updated = quote.updated_at
    observed = read_at if updated is None else min(updated, read_at)
    last = None if quote.last_kind is LastKind.PRIOR_CLOSE else _price(quote.last)
    evidence = QuoteEvidence(
        conid=quote.conid,
        status=quote.market_data_status,
        delay_seconds=quote.delay_seconds,
        observed_at=observed,
        bid=_price(quote.bid),
        ask=_price(quote.ask),
        last=last,
    )
    sigma = (
        _sigma(quote.implied_vol) if option else _sigma(quote.historical_vol_30d, quote.implied_vol)
    )
    return QuoteRead(evidence=evidence, sigma=sigma, halted=quote.last_kind is LastKind.HALTED)


class IbkrAccountReader:
    """The gate's reads over the IBKR read client and the order client."""

    def __init__(
        self,
        client: IbkrClient,
        orders: OrderClient,
        settings: ObliskConfig,
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._client = client
        self._orders = orders
        self._coid_length = settings.execution.orders.coid_max_length
        self._now = now

    async def positions(self) -> PositionsRead:
        rows = await self._client.portfolio.positions()
        observed = self._now()
        return PositionsRead(
            positions=tuple(
                Position(
                    conid=row.conid,
                    quantity=row.quantity,
                    average_cost=row.average_cost,
                    currency=row.currency,
                    observed_at=observed,
                )
                for row in rows
            ),
            observed_at=observed,
        )

    async def cash(self) -> CashRead:
        ledger = await self._client.portfolio.ledger()
        if ledger.unreadable_currencies:
            raise DecodeError(
                "the ledger carries no readable cash balance for "
                f"{', '.join(ledger.unreadable_currencies)}: the cash read is incomplete"
            )
        nav = ledger.nav
        return CashRead(
            lines=tuple(
                CashLine(currency=b.currency, cash=b.cash, rate_to_base=b.exchange_rate)
                for b in ledger.balances
            ),
            nav=None if nav is None else nav.nav,
            nav_refusal=ledger.nav_refusal,
            observed_at=self._now(),
        )

    async def live_orders(self) -> LiveOrders:
        return await self._orders.live_orders()

    async def trades(self) -> tuple[Fill, ...]:
        return await self._orders.trades()

    async def quote(self, conid: int, *, option: bool) -> QuoteRead | None:
        quotes = await self._client.marketdata.snapshot([conid], underlying=not option)
        found = quotes.get(conid)
        if found is None:
            return None
        return quote_read(found, option=option, read_at=self._now())

    async def whatif(self, terms: OrderTerms) -> WhatIf:
        # A cOID of its own: a preview must never be the reason the order's cOID reads as used.
        return await self._orders.whatif(terms, coid=coid_for(uuid.uuid7(), self._coid_length))
