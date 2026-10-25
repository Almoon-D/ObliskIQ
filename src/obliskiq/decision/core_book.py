"""The account's core book as the core engine reads it (MASTER_PLAN §5.4).

What the engine may know of the account: the core's units by vehicle, each vehicle's price in the
base currency, the value to invest, and the tax lots' answers to a trade. The lot engine's answers
come from :class:`~obliskiq.tax.book.TaxHooks`, which answers only for a security whose lots
account for every unit held. Where they cannot, the book says so by refusing to realise a gain:
a sale's tax unknown is a tax that is not paid, so a reduction is held, never made blind.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal

from ..tax.book import TaxHooks
from ..util.decimals import whole

ZERO = Decimal(0)
#: The tax of a sale the lots cannot price: no benefit exceeds it, so the reduction is held.
UNPRICED_TAX = Decimal("Infinity")


class LiveBook:
    """The core's units and prices on one day, keyed by contract id as a string (the name the tax
    lots give a security)."""

    def __init__(
        self,
        units: Mapping[str, Decimal],
        prices: Mapping[str, Decimal],
        investable: Decimal,
        *,
        hooks: TaxHooks | None,
        on: date,
    ) -> None:
        self._units = {key: quantity for key, quantity in units.items() if quantity > ZERO}
        self._prices = dict(prices)
        self._investable = investable
        self._hooks = hooks
        self._on = on

    @property
    def units(self) -> Mapping[str, Decimal]:
        return self._units

    @property
    def investable(self) -> Decimal:
        return self._investable

    def price(self, key: str) -> Decimal:
        return self._prices[key]

    def value(self, key: str) -> Decimal:
        quantity = self._units.get(key, ZERO)
        return quantity * self._prices[key] if quantity > ZERO else ZERO

    def weight(self, key: str) -> Decimal:
        return self.value(key) / self._investable if self._investable > ZERO else ZERO

    def basis(self, key: str) -> Decimal:
        if self._hooks is None or not self._known(key):
            return self.value(key)  # no gain is known: sales order by key
        return self._hooks.basis(int(key))

    def units_for(self, key: str, value: Decimal) -> Decimal:
        return whole(value / self._prices[key])

    def sales_tax(self, sales: Sequence[tuple[str, Decimal]]) -> Decimal:
        sold = [(key, units) for key, units in sales if units > ZERO]
        if not sold:
            return ZERO
        if self._hooks is None or not all(self._known(key) for key, _ in sold):
            return UNPRICED_TAX
        return self._hooks.trial_tax(
            [(int(key), units, self._prices[key]) for key, units in sold], on=self._on
        )

    def purchase_defers_loss(self, key: str) -> bool:
        return self._hooks is not None and self._hooks.purchase_defers_loss(int(key), self._on)

    def _known(self, key: str) -> bool:
        """Whether the lots account for every unit of ``key`` held."""
        return self._hooks is not None and self._hooks.complete(
            int(key), self._units.get(key, ZERO)
        )
