"""The variants Session 4c registered, as configuration states them (MASTER_PLAN §5.4,
DECISIONS D80, D91, D140).

Each variant is static weights by role and whether the trend tilt (and with it tax-aware execution)
applies. A risk balance is solved once on the declared covariance (``weights.py``), an equity
overlay holds one role at a fraction and the rest in cash. The weights are the same ones the
research harness computed from the pre-registration; ``tests/unit/test_core_config.py`` holds the
configuration equal to it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from ...config.schema import ObliskConfig
from ...config.schema_core import (
    BandedWeightsVariant,
    BuyAndHoldVariant,
    CoreVariant,
    EquityOverlayVariant,
)
from ...errors import CoreError
from .weights import environment_budgets, risk_balanced, risk_matrix

ONE = Decimal(1)


@dataclass(frozen=True, slots=True)
class Variant:
    """One registered variant: its kind, its static weights by role, and whether it tilts."""

    name: str
    kind: str
    weights: Mapping[str, Decimal]
    tilts: bool

    @property
    def holds(self) -> bool:
        """A buy-and-hold variant is bought once and never sold for."""
        return self.kind == "buy_and_hold"

    @property
    def tax_aware(self) -> bool:
        """Tax-aware execution goes with the tilt (the pre-registration's rule)."""
        return self.tilts


class CoreVariants:
    """The registered variants, their weights worked out once."""

    def __init__(self, settings: ObliskConfig) -> None:
        core = settings.core
        self._cash = core.cash_role
        self._environments = core.environments
        self._volatilities = core.risk.volatilities
        self._correlations = core.risk.correlations
        self._chosen = core.variant
        self._solver = settings.numerics.risk_budget
        self._variants = {item.name: self._resolve(item) for item in core.variants}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._variants)

    @property
    def chosen(self) -> str:
        """The variant that holds the account (``core.variant``)."""
        return self._chosen

    def get(self, name: str) -> Variant:
        try:
            return self._variants[name]
        except KeyError:
            raise CoreError(
                f"core variant {name} is not registered; the registered ones are "
                f"{', '.join(self._variants)}"
            ) from None

    def _resolve(self, item: CoreVariant) -> Variant:
        if isinstance(item, BuyAndHoldVariant | BandedWeightsVariant):
            return Variant(item.name, item.kind, dict(item.weights), tilts=False)
        if isinstance(item, EquityOverlayVariant):
            weights = {item.equity: item.fraction, self._cash: ONE - item.fraction}
            return Variant(item.name, item.kind, weights, tilts=item.tilt)
        roles = list(dict.fromkeys(r for basket in self._environments.values() for r in basket))
        vols, corr = risk_matrix(self._volatilities, self._correlations, roles)
        weights = risk_balanced(
            roles,
            environment_budgets(self._environments, item.budgets),
            vols,
            corr,
            tolerance=self._solver.tolerance,
            max_iterations=self._solver.max_iterations,
            name=item.name,
        )
        return Variant(item.name, item.kind, weights, tilts=item.tilt)
