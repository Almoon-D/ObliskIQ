"""A core book and view for the engine's tests: units, prices in the base currency, chosen tax
answers; and month-end closes that trend one way or the other.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

D = Decimal


@dataclass
class ToyBook:
    """A book of whole units at given prices. ``taxes`` is the tax per unit sold of a key,
    ``bases`` a key's basis, ``defers`` the keys whose purchase would defer a loss."""

    units: dict[str, Decimal]
    prices: dict[str, Decimal]
    investable: Decimal
    taxes: dict[str, Decimal] = field(default_factory=dict)
    bases: dict[str, Decimal] = field(default_factory=dict)
    defers: set[str] = field(default_factory=set)

    def price(self, key: str) -> Decimal:
        return self.prices[key]

    def value(self, key: str) -> Decimal:
        quantity = self.units.get(key, D(0))
        return quantity * self.prices[key] if quantity > 0 else D(0)

    def weight(self, key: str) -> Decimal:
        return self.value(key) / self.investable if self.investable > 0 else D(0)

    def basis(self, key: str) -> Decimal:
        return self.bases.get(key, self.value(key))

    def units_for(self, key: str, value: Decimal) -> Decimal:
        return (value / self.prices[key]).to_integral_value(rounding="ROUND_FLOOR")

    def sales_tax(self, sales: list[tuple[str, Decimal]]) -> Decimal:
        return sum((self.taxes.get(key, D(0)) * units for key, units in sales), D(0))

    def purchase_defers_loss(self, key: str) -> bool:
        return key in self.defers


def month_ends(last: date, count: int) -> list[date]:
    """``count`` month ends ending with the one of ``last``'s month, oldest first."""
    out: list[date] = []
    year, month = last.year, last.month
    for _ in range(count):
        out.append(date(year, month, calendar.monthrange(year, month)[1]))
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return out[::-1]


def closes(
    last: date, count: int, monthly: float, start: float = 100.0
) -> list[tuple[date, float]]:
    """Month-end closes growing by ``monthly`` a month (negative: falling)."""
    return [(day, start * (1 + monthly) ** k) for k, day in enumerate(month_ends(last, count))]
