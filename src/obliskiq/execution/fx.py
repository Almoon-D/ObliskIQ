"""A currency conversion's two legs, as IBKR trades them (docs/knowledge/ibkr-orders.md).

A conversion is an order on one of IBKR's spot pairs (``execution.currency.pairs``, secType
``CASH``) with ``isCcyConv``. Buying the pair sells its quote currency for its base; selling it
sells the base for the quote. The order is sized by ``fxQty``, the amount of the currency it
converts FROM (IBKR's tutorial on currency conversion orders [DOC 2026-10-20]), and capped by a
limit on the pair's rate (quote per base): a buy pays at most that rate, a sell takes at least it.
VERIFY-LIVE(conversion sides and fxQty): IBKR's example shows only a buy.

Pure functions; the currency manager plans with them and the pre-trade gate checks with them.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from ..config.schema_execution import CurrencyPair
from ..domain.orders import Fill, OrderSide, OrderTerms


@dataclass(frozen=True, slots=True)
class ConversionLeg:
    """Which currency a conversion sells (``source``) and which it buys (``target``)."""

    pair: CurrencyPair
    side: OrderSide

    @property
    def source(self) -> str:
        return self.pair.quote if self.side is OrderSide.BUY else self.pair.base

    @property
    def target(self) -> str:
        return self.pair.base if self.side is OrderSide.BUY else self.pair.quote

    def received(self, amount: Decimal, rate: Decimal) -> Decimal:
        """What converting ``amount`` of the source at ``rate`` (quote per base) brings in."""
        return amount / rate if self.side is OrderSide.BUY else amount * rate

    def needed(self, target_amount: Decimal, rate: Decimal) -> Decimal:
        """The source amount that brings in ``target_amount`` at ``rate``."""
        return target_amount * rate if self.side is OrderSide.BUY else target_amount / rate

    def spent(self, fills: Iterable[Fill]) -> Decimal:
        """The source amount the conversion's fills used: a fill's size is in the pair's base
        currency and its price is the rate. VERIFY-LIVE(the trades of a conversion)."""
        if self.side is OrderSide.BUY:
            return sum((fill.quantity * fill.price for fill in fills), Decimal(0))
        return sum((fill.quantity for fill in fills), Decimal(0))


def leg_for(pair: CurrencyPair, *, source: str, target: str) -> ConversionLeg | None:
    """The leg that converts ``source`` into ``target`` on ``pair``; ``None`` when the pair does
    not join them."""
    if (source, target) == (pair.quote, pair.base):
        return ConversionLeg(pair, OrderSide.BUY)
    if (source, target) == (pair.base, pair.quote):
        return ConversionLeg(pair, OrderSide.SELL)
    return None


def conversion_leg(pairs: Iterable[CurrencyPair], terms: OrderTerms) -> ConversionLeg | None:
    """The leg a conversion's terms trade; ``None`` when its conid is no configured pair."""
    for pair in pairs:
        if pair.conid == terms.conid:
            return ConversionLeg(pair, terms.side)
    return None
