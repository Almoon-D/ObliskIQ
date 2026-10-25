"""What the core knows of its positions, for the reservation planner (MASTER_PLAN §5.15,
DECISIONS D108, D140).

The planner asks each sleeve what selling a position costs the strategy. For the core the answer
comes from its latest target portfolio: a held vehicle the target no longer names is one the core
would sell anyway, and the part of a vehicle above its target is what rebalancing would trim.
Nothing else is the core's to claim: its positions carry no protection, and an expected return
counts only from a layer whose research outcome is sufficient (MASTER_PLAN §5.9), which the core's
is not (every candidate inconclusive, DECISIONS D91, D95). Before the first target of a run, or
when the engine could not form one, the core claims nothing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping
from datetime import date
from decimal import Decimal

from ..capital.strategy import ExpectedReturn, PositionFacts, Protected, StrategyValue
from ..domain.orders import Sleeve
from ..domain.portfolio import TargetPortfolio, TargetStatus


class LatestTarget:
    """The core's latest target portfolio, kept for the run (the rebalancer sets it each cycle)."""

    def __init__(self) -> None:
        self.portfolio: TargetPortfolio | None = None

    def weights(self) -> Mapping[int, Decimal] | None:
        """The vehicles' target weights by contract id, or ``None`` when no target is formed."""
        portfolio = self.portfolio
        if portfolio is None or portfolio.status is not TargetStatus.READY:
            return None
        return {int(v.key): v.weight for v in portfolio.vehicles}


class CoreStrategyValue:
    """The core's side of :class:`~obliskiq.capital.strategy.StrategyValue`; every other sleeve
    is the default's."""

    def __init__(self, latest: LatestTarget, others: StrategyValue) -> None:
        self._latest = latest
        self._others = others

    def would_exit(self, position: PositionFacts, *, today: date) -> bool:
        if position.sleeve is not Sleeve.CORE:
            return self._others.would_exit(position, today=today)
        weights = self._latest.weights()
        return weights is not None and weights.get(position.conid) is None

    def protection(self, position: PositionFacts, *, today: date) -> Protected | None:
        return self._others.protection(position, today=today)

    def expected_return(self, position: PositionFacts, *, today: date) -> ExpectedReturn | None:
        return self._others.expected_return(position, today=today)
