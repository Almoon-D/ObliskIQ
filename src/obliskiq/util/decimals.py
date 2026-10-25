"""Exact decimal helpers for weights and whole units, shared by the simulator and the core engine
so that a target weight and a unit count are rounded the same way in a test and in the account.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from decimal import ROUND_FLOOR, Decimal, localcontext
from typing import Final

#: A target weight's last decimal place. Weights at this quantum add up exactly in Decimal's 28
#: significant digits, and rounded down they never sum above the values they came from. It is
#: fine enough that one unit of a position worth a millionth of a percent of the account comes
#: back as one unit (a quantum of 1e-20 lost it in a billion-euro account).
WEIGHT_QUANTUM: Final = Decimal("1e-24")
#: A unit count this close to a whole number is that number: what Decimal rounding leaves of a
#: round trip from units to a weight and back, far below any quantity a venue trades.
UNIT_RESIDUE: Final = Decimal("1e-9")


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
