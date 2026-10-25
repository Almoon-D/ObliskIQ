"""The strategies a pre-registration may name, built from its specification.

``fixed_weights`` rebalances to the same weights at every date (the band decides what trades);
``buy_and_hold`` buys its weights on the first date and never trades again, so its tax waits for
the horizon.

The core's variants (MASTER_PLAN §5.4) are one mechanism with different static weights:

- **Static weights by role**: ``banded_weights`` states them; ``risk_balance`` computes them once
  from risk budgets by environment (25 % to each of growth up, growth down, inflation up and
  inflation down in A and B; a growth-tilted budget in C), equal risk contribution inside an
  environment's basket, on the pre-registered long-run covariance (``quant.risk_budget``);
  ``equity_overlay`` (D) holds one equity role at a stated fraction and the rest in cash.
- **Wide bands**: nothing trades while every role sits inside its outer band around its target,
  the smaller of an absolute width and a fraction of the target; when one leaves it, every role
  is traded back to its target.
- **The trend tilt** (B, C, D): each risky role's target is scaled by its trend
  (``quant.trend``: 12-month momentum over cash and a 10-month moving average, hysteresis, a
  floor above zero); the weight freed goes to the cash role.
- **Tax-aware execution** with the tilt: a trend reduction is made only while the tax it would
  cost (its sales together, so a loss on one vehicle offsets a gain on another) is at most a
  pre-registered fraction of the value sold (its assumed benefit), else the
  role keeps its weight; losses are taken freely; a purchase that would defer a loss under the
  wash-sale rule goes to the role's swap partner, which differs in issuer and index.

Each role is held through its primary vehicle and, with tax-aware execution, its swap partner;
sales come first from the vehicle with the smaller gain for its value.

**Scaling total risk** (``total_risk``, DECISIONS D92) is the same mechanism with one risky share
instead of the tilt: the risky roles keep fixed relative weights (the base), and their share of
the account is the target volatility over the base's forecast volatility, capped at one (no
leverage), at least the floor and moved by at most the largest change from the share held; the
rest goes to the cash role. The forecast is the base's volatility under the estimator's
covariance as of the date (``quant.covariance`` on the panel's daily returns, shrunk toward the
declared matrix), or under the declared matrix itself: the declared twin, whose constant share
makes it a static portfolio. Bands, the no-trade band and tax-aware execution are the core's.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from typing import cast

import numpy as np

from ..config.schema import ObliskConfig
from ..engines.core.mechanism import (
    Bands,
    BuyAndHold,
    CoreBook,
    CoreStrategy,
    RoleVehicles,
    TaxAware,
    Weights,
)
from ..engines.core.view import CoreView
from ..engines.core.weights import WEIGHT_QUANTUM, environment_budgets, risk_balanced
from ..errors import CoreError, PreregistrationError, SimulationError
from ..quant.covariance import EstimatorRule, portfolio_volatility
from ..quant.risk_budget import covariance_from
from ..quant.trend import TrendRule, TrendState
from .prereg import (
    BandedWeightsSpec,
    BuyAndHoldSpec,
    CoreSpec,
    CoreStrategySpec,
    EquityOverlaySpec,
    FixedWeightsSpec,
    StrategySpec,
    StudySpec,
    TotalRiskSpec,
)
from .simulate import Book, MarketView

ZERO = Decimal(0)
ONE = Decimal(1)


@dataclass(frozen=True, slots=True)
class FixedWeights:
    """The same target weights at every rebalance date."""

    name: str
    weights: Weights

    def targets(self, view: MarketView, book: Book) -> Weights | None:
        del view, book  # the targets never change
        return self.weights


@dataclass(frozen=True, slots=True)
class TotalRisk:
    """The risky share of a total-risk strategy (the module docstring). ``declared`` is the
    base roles' yearly covariance, in the order of ``base``; ``estimator`` ``None`` forecasts
    with it (the declared twin)."""

    base: Mapping[str, Decimal]
    target_volatility: float
    floor: Decimal
    max_change: Decimal
    estimator: EstimatorRule | None
    declared: np.ndarray
    periods_per_year: int

    def __post_init__(self) -> None:
        if sum(self.base.values(), ZERO) != ONE or any(w <= ZERO for w in self.base.values()):
            raise ValueError("a base's relative weights are positive and sum to 1")
        if self.declared.shape != (len(self.base), len(self.base)):
            raise ValueError("the declared covariance covers the base's roles")
        if not self.target_volatility > 0 or not ZERO <= self.floor < ONE:
            raise ValueError("a target volatility is positive and a floor lies in [0, 1)")
        if not ZERO < self.max_change <= ONE or self.periods_per_year < 1:
            raise ValueError("the largest change lies in (0, 1] and a year has periods")

    @property
    def weights(self) -> np.ndarray:
        return np.array([float(w) for w in self.base.values()])

    def declared_volatility(self) -> float:
        return portfolio_volatility(self.declared, self.weights, 1)

    def forecast(self, view: MarketView, keys: Sequence[str]) -> float:
        """The base's yearly volatility as of the view's date; ``keys`` are its roles' primary
        vehicles, in the order of ``base``."""
        if self.estimator is None:
            return self.declared_volatility()
        target = self.declared / self.periods_per_year
        found = view.covariance(keys, self.estimator, target)
        if found is None:
            raise SimulationError(f"no covariance of {', '.join(keys)} on {view.as_of}")
        return portfolio_volatility(found.covariance, self.weights, self.periods_per_year)

    def share(self, forecast: float, held: Decimal | None) -> Decimal:
        """The risky share for a forecast volatility, from the share ``held`` (``None`` on the
        first date: no largest change applies)."""
        wanted = Decimal(repr(min(1.0, self.target_volatility / forecast))).quantize(
            WEIGHT_QUANTUM, rounding=ROUND_FLOOR
        )
        wanted = max(self.floor, wanted)
        if held is not None:
            wanted = min(max(wanted, held - self.max_change), held + self.max_change)
        return min(max(wanted, ZERO), ONE)


@dataclass(frozen=True, slots=True)
class ScaledCoreStrategy(CoreStrategy):
    """The core's mechanism with one risky share instead of the tilt (the module docstring):
    the engine's strategy, whose role targets are scaled to a target volatility. It is research
    only (DECISIONS D95): the account does not scale total risk."""

    scaling: TotalRisk = field(kw_only=True)

    def _check(self) -> None:
        self._check_common()
        scaling = self.scaling
        if self.tilt is not None:
            raise ValueError(f"strategy {self.name} both tilts and scales total risk (D92)")
        if self.cash_role is None:
            raise ValueError(f"strategy {self.name} scales total risk without a cash role")
        unknown = sorted(set(scaling.base) - set(self.vehicles))
        if unknown or self.cash_role in scaling.base:
            raise ValueError(f"strategy {self.name}'s base names roles it cannot hold")

    def _states(
        self, view: CoreView, book: CoreBook
    ) -> tuple[dict[str, Decimal], dict[str, Decimal], dict[str, TrendState]]:
        if self.cash_role is None:  # the check refuses this already
            raise ValueError(f"strategy {self.name} scales total risk without a cash role")
        return *self._scaled_targets(self.scaling, self.cash_role, view, book), {}

    def role_targets(
        self, view: CoreView, book: CoreBook | None = None
    ) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
        """Each role's target weight and its scale: the risky share of the account (from the
        share ``book`` holds)."""
        if self.cash_role is None:  # the check refuses this already
            raise ValueError(f"strategy {self.name} scales total risk without a cash role")
        return self._scaled_targets(self.scaling, self.cash_role, view, book)

    def _scaled_targets(
        self, scaling: TotalRisk, cash_role: str, view: CoreView, book: CoreBook | None
    ) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
        keys = [self.vehicles[role].primary for role in scaling.base]
        held = (
            sum((self._role_weight(role, book) for role in scaling.base), ZERO)
            if book is not None and book.units
            else None
        )
        # A total-risk strategy is simulated only: the view is the research market view.
        share = scaling.share(scaling.forecast(cast(MarketView, view), keys), held)
        targets = {role: share * weight for role, weight in scaling.base.items()}
        targets[cash_role] = ONE - share
        scales = dict.fromkeys(scaling.base, share)
        scales[cash_role] = ONE
        return targets, scales


# ── building a strategy from its specification ──


def total_risk(spec: TotalRiskSpec, core: CoreSpec) -> TotalRisk:
    """The risky share of a total-risk strategy, from its specification and the core's."""
    if core.scaling is None:  # the specification refuses this already
        raise PreregistrationError(f"strategy {spec.name} needs study.core.scaling")
    roles = list(spec.base)
    vols, corr = core.risk.matrix(core.risk_roles())
    order = core.risk_roles()
    full = covariance_from(vols, corr)
    picked = [order.index(role) for role in roles]
    return TotalRisk(
        base=dict(spec.base),
        target_volatility=float(spec.target_volatility),
        floor=spec.floor,
        max_change=spec.max_change,
        estimator=None if spec.declared else core.scaling.estimators[spec.estimator].rule(),
        declared=full[np.ix_(picked, picked)],
        periods_per_year=core.scaling.periods_per_year,
    )


def declared_share(spec: TotalRiskSpec, core: CoreSpec) -> Decimal:
    """The risky share a total-risk strategy holds under the declared matrix (its twin's)."""
    scaling = total_risk(spec, core)
    return scaling.share(scaling.declared_volatility(), None)


def static_weights(
    spec: CoreStrategySpec, core: CoreSpec, settings: ObliskConfig
) -> dict[str, Decimal]:
    """A core strategy's static weights by role (before any tilt; a total-risk strategy's at
    its declared share)."""
    if isinstance(spec, BandedWeightsSpec):
        return dict(spec.weights)
    if isinstance(spec, EquityOverlaySpec):
        return {spec.equity: spec.fraction, core.cash_role: ONE - spec.fraction}
    if isinstance(spec, TotalRiskSpec):
        share = declared_share(spec, core)
        held = {role: share * weight for role, weight in spec.base.items()}
        held[core.cash_role] = ONE - share
        return held
    roles = core.risk_roles()
    shares = environment_budgets(core.environments, spec.budgets)
    vols, corr = core.risk.matrix(roles)
    solver = settings.numerics.risk_budget
    try:
        return risk_balanced(
            roles,
            shares,
            vols,
            corr,
            tolerance=solver.tolerance,
            max_iterations=solver.max_iterations,
            name=spec.name,
        )
    except CoreError as exc:
        raise PreregistrationError(str(exc)) from exc


def tilt_rule(core: CoreSpec) -> TrendRule:
    tilt = core.tilt
    if tilt is None:  # the specification refuses a tilt without it
        raise PreregistrationError("the tilt needs study.core.tilt")
    return TrendRule(
        momentum_months=tilt.momentum_months,
        average_months=tilt.average_months,
        hysteresis=float(tilt.hysteresis),
        floor=float(tilt.floor),
    )


def build_strategy(
    spec: StrategySpec, study: StudySpec, settings: ObliskConfig
) -> FixedWeights | BuyAndHold | CoreStrategy | ScaledCoreStrategy:
    """The strategy a pre-registration names."""
    if isinstance(spec, FixedWeightsSpec):
        return FixedWeights(spec.name, dict(spec.weights))
    if isinstance(spec, BuyAndHoldSpec):
        return BuyAndHold(spec.name, dict(spec.weights))
    if study.core is None or study.roles is None:  # the specification refuses this already
        raise PreregistrationError(f"strategy {spec.name} needs study.roles and study.core")
    core = study.core
    if isinstance(spec, TotalRiskSpec):
        tilt, aware, hold = False, spec.tax_aware, spec.hold_costly_reductions
    else:
        tilt = not isinstance(spec, BandedWeightsSpec) and spec.tilt
        aware = hold = tilt
    benefit = None
    if hold:
        if core.tax_aware is None:  # the specification refuses this already
            raise PreregistrationError(f"strategy {spec.name} needs study.core.tax_aware")
        benefit = core.tax_aware.benefit_fraction
    vehicles = {
        role: RoleVehicles(held.primary, held.partner if aware else None)
        for role, held in study.roles.items()
    }
    bands = Bands(core.bands.absolute, core.bands.relative)
    tax_aware = TaxAware(benefit) if aware else None
    weights = static_weights(spec, core, settings)
    if isinstance(spec, TotalRiskSpec):
        return ScaledCoreStrategy(
            name=spec.name,
            weights=weights,
            vehicles=vehicles,
            bands=bands,
            cash_role=core.cash_role,
            tilt=None,
            tax_aware=tax_aware,
            scaling=total_risk(spec, core),
        )
    return CoreStrategy(
        name=spec.name,
        weights=weights,
        vehicles=vehicles,
        bands=bands,
        cash_role=core.cash_role,
        tilt=tilt_rule(core) if tilt else None,
        tax_aware=tax_aware,
    )
