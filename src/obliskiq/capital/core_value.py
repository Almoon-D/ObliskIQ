"""The core's StrategyValue (MASTER_PLAN §5.4, §5.15; DECISIONS D108).

The reservation planner ranks a sale's strategy cost by what the sleeve knows of the position.
For a core position that is its target in the core engine's last target portfolio (each survey
notes it, ``decision/rebalance_core.py``):

* its **target weight** of the core's budget, so the part held above it is an overweight, the
  cheapest sale after an exit the strategy makes anyway;
* **an exit it makes anyway**: a vehicle the portfolio holds at no target (one no role names, a
  role the variant does not hold);
* **no protection**: core lines are not theses (``capital.planner.protected_sleeves``);
* **no expected return**: no adaptation of the core is sufficient (DECISIONS D95), and an
  estimate counts only from a sufficient layer.

Until a portfolio is noted (the first cycle after a start, before its survey) the core states no
target and every core position ranks as an ordinary holding. Every other sleeve's position is
answered by the fallback (the default ranking).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from decimal import Decimal

from ..domain.core import TargetPortfolio
from ..domain.orders import Sleeve
from .strategy import ExpectedReturn, PositionFacts, Protected, StrategyValue

ZERO = Decimal(0)


class CoreStrategyValue:
    """What the core's last target portfolio says of each core position."""

    def __init__(self, fallback: StrategyValue) -> None:
        self._fallback = fallback
        self._portfolio: TargetPortfolio | None = None

    @property
    def portfolio(self) -> TargetPortfolio | None:
        return self._portfolio

    def note(self, portfolio: TargetPortfolio) -> None:
        """The portfolio the core engine decided last."""
        self._portfolio = portfolio

    def would_exit(self, position: PositionFacts, *, today: date) -> bool:
        if position.sleeve is not Sleeve.CORE:
            return self._fallback.would_exit(position, today=today)
        weight = self.target_weight(position, today=today)
        return weight is not None and weight == ZERO

    def protection(self, position: PositionFacts, *, today: date) -> Protected | None:
        if position.sleeve is not Sleeve.CORE:
            return self._fallback.protection(position, today=today)
        return None

    def expected_return(self, position: PositionFacts, *, today: date) -> ExpectedReturn | None:
        if position.sleeve is not Sleeve.CORE:
            return self._fallback.expected_return(position, today=today)
        return None

    def target_weight(self, position: PositionFacts, *, today: date) -> Decimal | None:
        if position.sleeve is not Sleeve.CORE:
            return self._fallback.target_weight(position, today=today)
        if self._portfolio is None:
            return None
        return self._portfolio.weight(position.conid)
