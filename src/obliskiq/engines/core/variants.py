"""The variants Session 4c registered, built from ``core.variants`` (MASTER_PLAN §5.4, DECISIONS
D60, D80, D91).

A variant's static weights come from ``engines.core.weights`` exactly as the research harness
computes them, and its rules follow Session 4c's build (``research/strategies.py``): a variant
that tilts trades tax-aware through its roles' swap partners; one that does not holds each role
through the vehicles the book already holds and its primary. The equity reference buys once and
holds. ``core.variant`` names the variant the core runs; the default is the owner's point (A).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from ...config.schema import ObliskConfig
from ...config.schema_core import (
    BandedWeightsVariant,
    BuyAndHoldVariant,
    CoreConfig,
    EquityOverlayVariant,
    RiskBalanceVariant,
    VariantConfig,
)
from ...config.schema_numerics import RiskBudgetConfig
from ...errors import CoreError
from ...quant.trend import TrendRule
from .rules import Bands, CoreRules, RoleVehicles, TaxAware
from .weights import equity_overlay, risk_balance


@dataclass(frozen=True, slots=True)
class Variant:
    """A registered variant: its name, its static weights by role (before the tilt), whether it
    tilts (and so trades tax-aware), whether it buys and holds, and the constants it shares."""

    name: str
    weights: dict[str, Decimal]
    tilts: bool
    buy_and_hold: bool
    cash_role: str
    bands: Bands
    tilt: TrendRule
    tax_aware: TaxAware

    @property
    def roles(self) -> tuple[str, ...]:
        """The roles the variant weights, and the cash role when it tilts (it receives the freed
        weight)."""
        named = [role for role, weight in self.weights.items() if weight > 0]
        if self.tilts and self.cash_role not in named:
            named.append(self.cash_role)
        return tuple(named)

    def rules(
        self, vehicles: Mapping[str, RoleVehicles], *, untilted: frozenset[str] = frozenset()
    ) -> CoreRules:
        """The variant's rules over ``vehicles`` (role to its vehicles' keys). The cash role
        takes part when the book holds it or the variant names it (a tilt's freed weight, an
        overlay's rest)."""
        return CoreRules(
            name=self.name,
            weights=self.weights,
            vehicles=vehicles,
            bands=None if self.buy_and_hold else self.bands,
            cash_role=self.cash_role if self.cash_role in vehicles else None,
            tilt=self.tilt if self.tilts else None,
            tax_aware=self.tax_aware if self.tilts else None,
            untilted=untilted,
        )


def static_weights(
    core: CoreConfig, name: str, config: VariantConfig, solver: RiskBudgetConfig
) -> dict[str, Decimal]:
    """A variant's static weights by role, as Session 4c computed them."""
    if isinstance(config, BuyAndHoldVariant | BandedWeightsVariant):
        return dict(config.weights)
    if isinstance(config, EquityOverlayVariant):
        return equity_overlay(config.equity, config.fraction, core.cash_role)
    vols, corr = core.risk.matrix(core.risk_roles())
    try:
        solution = risk_balance(
            core.environments,
            config.budgets,
            vols,
            corr,
            tolerance=solver.tolerance,
            max_iterations=solver.max_iterations,
        )
    except ValueError as exc:  # a declared risk that is no covariance
        raise CoreError(f"variant {name}: {exc}") from exc
    if not solution.converged:
        raise CoreError(
            f"variant {name}: the risk budget did not converge in {solution.iterations} sweeps"
        )
    return solution.weights


def build_variant(settings: ObliskConfig, name: str | None = None) -> Variant:
    """The variant ``name`` (by default ``core.variant``, the one the core runs)."""
    core = settings.core
    chosen = core.variant if name is None else name
    config = core.variants.get(chosen)
    if config is None:
        raise CoreError(f"{chosen!r} is not a variant of core.variants")
    tilts = isinstance(config, RiskBalanceVariant | EquityOverlayVariant) and config.tilt
    tilt = core.tilt
    return Variant(
        name=chosen,
        weights=static_weights(core, chosen, config, settings.numerics.risk_budget),
        tilts=tilts,
        buy_and_hold=isinstance(config, BuyAndHoldVariant),
        cash_role=core.cash_role,
        bands=Bands(core.bands.absolute, core.bands.relative),
        tilt=TrendRule(
            momentum_months=tilt.momentum_months,
            average_months=tilt.average_months,
            hysteresis=float(tilt.hysteresis),
            floor=float(tilt.floor),
        ),
        tax_aware=TaxAware(core.tax_aware.benefit_fraction),
    )
