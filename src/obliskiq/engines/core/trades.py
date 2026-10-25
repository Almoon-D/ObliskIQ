"""Delta execution: the units to sell and to buy to bring holdings to target weights.

Only the change to each target is traded, in whole units unless the account trades fractions, and
only when it exceeds the no-trade band. The simulator (``research.simulate``) and the live
rebalancer both ask this function, so the trades a test priced are the trades the account makes.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from ...util.decimals import whole

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class Deltas:
    """The units to sell and to buy, by key in key order."""

    sells: tuple[tuple[str, Decimal], ...]
    buys: tuple[tuple[str, Decimal], ...]


def plan_deltas(
    weights: Mapping[str, Decimal],
    units: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
    investable: Decimal,
    *,
    band: Decimal,
    whole_units: bool,
) -> Deltas:
    """The trades from ``units`` to ``weights`` of ``investable`` at ``prices`` (base currency).

    A key held and not named has a target of zero. A change smaller than ``band`` of the target
    value is skipped unless the target is zero, so a position is always closed in full."""
    sells: list[tuple[str, Decimal]] = []
    buys: list[tuple[str, Decimal]] = []
    for key in sorted(set(weights) | set(units)):
        target_value = investable * weights.get(key, ZERO)
        wanted = target_value / prices[key]
        target = whole(wanted) if whole_units else wanted
        delta = target - units.get(key, ZERO)
        if delta == ZERO or (target > ZERO and abs(delta) * prices[key] < band * target_value):
            continue
        (sells if delta < ZERO else buys).append((key, abs(delta)))
    return Deltas(tuple(sells), tuple(buys))
