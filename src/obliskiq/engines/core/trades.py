"""Delta execution: from target weights to whole-unit trades (MASTER_PLAN §5.4, DECISIONS D75).

The research simulator and the live core trade a target the same way:

1. Each instrument's **target units** are its weight of the investable value at its price, rounded
   down to whole units (a count within :data:`UNIT_RESIDUE` of a whole number is that number, so a
   position kept at its weight is not sold by one unit).
2. Only the **change** is traded, and only when it reaches the **no-trade band**: a fraction of the
   target value. A position whose target is zero is always sold whole.
3. **Sells first**; then the buys are scaled by **one common factor**, the largest whose cost (the
   notional and every leg's cost) the cash covers, found by bisection: a minimum commission makes
   a smaller order cost more than its share, so proportional scaling never converges.

Weights are shares of one total rounded down to :data:`WEIGHT_QUANTUM` (:func:`share`), so their
sum never exceeds the values they came from and a rounding residue is never read as leverage.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Mapping, Sequence
from decimal import ROUND_FLOOR, Decimal, localcontext
from typing import Final

ZERO = Decimal(0)
ONE = Decimal(1)
#: A target weight's last decimal place. Weights at this quantum add up exactly in Decimal's 28
#: significant digits, and rounded down they never sum above the values they came from. It is
#: fine enough that one unit of a position worth a millionth of a percent of the account comes
#: back as one unit (a quantum of 1e-20 lost it in a billion-euro account).
WEIGHT_QUANTUM: Final = Decimal("1e-24")
#: A unit count this close to a whole number is that number: what Decimal rounding leaves of a
#: round trip from units to a weight and back, far below any quantity a venue trades.
UNIT_RESIDUE: Final = Decimal("1e-9")
#: Halvings in the search for the share of a buy list the cash covers (2^-30 of the list).
SCALE_BISECTIONS: Final = 30

#: One trade: an instrument's key and a positive quantity.
type Leg = tuple[str, Decimal]


def share(value: Decimal, total: Decimal) -> Decimal:
    """``value / total`` rounded down to :data:`WEIGHT_QUANTUM`: shares of one total computed
    this way never sum above 1, so a rounding residue is never read as leverage."""
    with localcontext() as context:
        context.rounding = ROUND_FLOOR
        return (value / total).quantize(WEIGHT_QUANTUM)


def whole(units: Decimal, rounding: str = ROUND_FLOOR) -> Decimal:
    """``units`` as whole units, rounded by ``rounding``; a count within :data:`UNIT_RESIDUE` of
    a whole number is that number, so a position kept at its weight is not sold by one unit."""
    nearest = units.to_integral_value()
    if abs(units - nearest) <= UNIT_RESIDUE:
        return nearest
    return units.to_integral_value(rounding=rounding)


def units_of(
    value: Decimal, price: Decimal, *, whole_units: bool, rounding: str = ROUND_FLOOR
) -> Decimal:
    """The units ``value`` buys at ``price``: whole units by ``rounding``, or exact."""
    units = value / price
    return whole(units, rounding) if whole_units else units


def delta_trades(
    weights: Mapping[str, Decimal],
    held: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
    investable: Decimal,
    *,
    band: Decimal,
    whole_units: bool,
) -> tuple[list[Leg], list[Leg]]:
    """The sells and the buys that take ``held`` units to ``weights`` of ``investable``, in key
    order; a change below ``band`` of its target value is not traded. ``prices`` covers every key
    of both (one unit in the investable value's currency)."""
    sells: list[Leg] = []
    buys: list[Leg] = []
    for key in sorted(set(weights) | set(held)):
        target_value = investable * weights.get(key, ZERO)
        target = units_of(target_value, prices[key], whole_units=whole_units)
        delta = target - held.get(key, ZERO)
        if delta == ZERO or (target > ZERO and abs(delta) * prices[key] < band * target_value):
            continue
        (sells if delta < ZERO else buys).append((key, abs(delta)))
    return sells, buys


def affordable(
    buys: Sequence[Leg],
    cash: Decimal,
    prices: Mapping[str, Decimal],
    cost: Callable[[str, Decimal], Decimal],
    *,
    whole_units: bool,
) -> list[Leg]:
    """``buys`` scaled by one common factor, the largest whose cost the cash covers. ``cost``
    is what a buy takes from cash: its notional and every leg's cost."""

    def scaled(factor: Decimal) -> list[Leg]:
        plan = [
            (key, units_of(q * factor * prices[key], prices[key], whole_units=whole_units))
            for key, q in buys
        ]
        return [(key, q) for key, q in plan if q > ZERO]

    def total(plan: Sequence[Leg]) -> Decimal:
        return sum((cost(key, q) for key, q in plan), ZERO)

    if total(buys) <= cash:
        return list(buys)
    low, high = ZERO, ONE
    for _ in range(SCALE_BISECTIONS):
        middle = (low + high) / 2
        if total(scaled(middle)) <= cash:
            low = middle
        else:
            high = middle
    return scaled(low)
