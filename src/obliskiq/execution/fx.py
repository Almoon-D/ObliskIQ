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


def preview_terms(leg: ConversionLeg, terms: OrderTerms) -> OrderTerms:
    """The plain order on the pair that a whatif of ``terms`` may use.

    IBKR's whatif answers HTTP 503 for an order carrying ``isCcyConv``/``fxQty`` (T1, 2026-10-20),
    while the same pair as an ordinary limit order in units previews with its cost and commission.
    The preview is never sent as an order. A buy of the pair is sized in the base currency it
    brings in at the limit, a sell in the base currency it sells. Placement is unchanged: the
    conversion itself is still sent with ``isCcyConv``.
    """
    amount = terms.cash_quantity
    if not terms.conversion or amount is None:
        raise ValueError("only a conversion has a preview order")
    units = amount / terms.price.limit if leg.side is OrderSide.BUY else amount
    units = units.to_integral_value()
    if units <= 0:
        raise ValueError(f"{amount} {leg.source} is less than one {leg.pair.base} at the limit")
    return terms.model_copy(update={"conversion": False, "cash_quantity": None, "quantity": units})
