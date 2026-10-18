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
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Final

import numpy as np

from ..config.schema import ObliskConfig
from ..errors import PreregistrationError, SimulationError
from ..quant.covariance import EstimatorRule, portfolio_volatility
from ..quant.risk_budget import covariance_from, risk_budget_weights
from ..quant.trend import TrendRule
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
from .simulate import Book, MarketView, share

type Weights = Mapping[str, Decimal]

ZERO = Decimal(0)
ONE = Decimal(1)
#: Static weights are kept to a millionth.
WEIGHT_QUANTUM: Final = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class FixedWeights:
    """The same target weights at every rebalance date."""

    name: str
    weights: Weights

    def targets(self, view: MarketView, book: Book) -> Weights | None:
        del view, book  # the targets never change
        return self.weights


@dataclass(frozen=True, slots=True)
class BuyAndHold:
    """Its weights on the first date, then nothing."""

    name: str
    weights: Weights

    def targets(self, view: MarketView, book: Book) -> Weights | None:
        del view
        return None if book.units else self.weights


@dataclass(frozen=True, slots=True)
class RoleVehicles:
    """The instruments that hold a role: its primary vehicle and, if any, its swap partner."""

    primary: str
    partner: str | None

    @property
    def keys(self) -> tuple[str, ...]:
        return (self.primary,) if self.partner is None else (self.primary, self.partner)


@dataclass(frozen=True, slots=True)
class Bands:
    """A role's outer band: the smaller of ``absolute`` weight and ``relative`` of its target."""

    absolute: Decimal
    relative: Decimal

    def width(self, target: Decimal) -> Decimal:
        return min(self.absolute, self.relative * target)


@dataclass(frozen=True, slots=True)
class TaxAware:
    """A reduction is made only while its tax is at most ``benefit_fraction`` of the value sold
    (``None``: reductions are always made); purchases go to a swap partner when the primary
    would defer a loss."""

    benefit_fraction: Decimal | None


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
class CoreStrategy:
    """Static role weights held within wide bands, optionally tilted by trend or scaled to a
    target volatility, and executed tax-aware (the module docstring)."""

    name: str
    weights: Weights
    vehicles: Mapping[str, RoleVehicles]
    bands: Bands
    cash_role: str | None
    tilt: TrendRule | None
    tax_aware: TaxAware | None
    scaling: TotalRisk | None = None

    def __post_init__(self) -> None:
        unknown = sorted(set(self.weights) - set(self.vehicles))
        if unknown:
            raise ValueError(f"strategy {self.name} weights roles without vehicles: {unknown}")
        if any(w < ZERO for w in self.weights.values()) or sum(self.weights.values()) > ONE:
            raise ValueError(
                f"strategy {self.name} weights must be at least 0 and sum to 1 at most"
            )
        if (self.tilt is not None or self.tax_aware is not None) and self.cash_role is None:
            raise ValueError(f"strategy {self.name} tilts or trades tax-aware without a cash role")
        if self.cash_role is not None and self.cash_role not in self.vehicles:
            raise ValueError(f"strategy {self.name} names a cash role without vehicles")
        if self.tax_aware is not None and self.tilt is None and self.scaling is None:
            raise ValueError(
                f"strategy {self.name}: tax-aware execution belongs to the tilt or the scaling"
            )
        if self.scaling is not None:
            if self.tilt is not None:
                raise ValueError(f"strategy {self.name} both tilts and scales total risk (D92)")
            if self.cash_role is None:
                raise ValueError(f"strategy {self.name} scales total risk without a cash role")
            unknown = sorted(set(self.scaling.base) - set(self.vehicles))
            if unknown or self.cash_role in self.scaling.base:
                raise ValueError(f"strategy {self.name}'s base names roles it cannot hold")

    # ── targets ──

    def role_targets(
        self, view: MarketView, book: Book | None = None
    ) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
        """Each role's target weight and its scale (1 without a tilt or a scaling): the trend's
        scale, or the risky share of a total-risk strategy (from the share ``book`` holds)."""
        if self.scaling is not None and self.cash_role is not None:
            return self._scaled_targets(self.scaling, self.cash_role, view, book)
        targets = dict(self.weights)
        scales = dict.fromkeys(targets, ONE)
        if self.tilt is None or self.cash_role is None:
            return targets, scales
        targets.setdefault(self.cash_role, ZERO)
        scales.setdefault(self.cash_role, ONE)
        cash = self.vehicles[self.cash_role].primary
        for role, weight in self.weights.items():
            if role == self.cash_role or weight == ZERO:
                continue
            state = view.trend(self.vehicles[role].primary, cash, self.tilt)
            if state is None:
                raise SimulationError(
                    f"{self.name}: {role} lacks the {self.tilt.months_needed} month ends the "
                    f"tilt needs on {view.as_of}"
                )
            scale = Decimal(repr(state.scale))
            scales[role] = scale
            targets[role] = weight * scale
            targets[self.cash_role] += weight * (ONE - scale)
        return targets, scales

    def _scaled_targets(
        self, scaling: TotalRisk, cash_role: str, view: MarketView, book: Book | None
    ) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
        keys = [self.vehicles[role].primary for role in scaling.base]
        held = (
            sum((self._role_weight(role, book) for role in scaling.base), ZERO)
            if book is not None and book.units
            else None
        )
        share = scaling.share(scaling.forecast(view, keys), held)
        targets = {role: share * weight for role, weight in scaling.base.items()}
        targets[cash_role] = ONE - share
        scales = dict.fromkeys(scaling.base, share)
        scales[cash_role] = ONE
        return targets, scales

    def targets(self, view: MarketView, book: Book) -> Weights | None:
        targets, scales = self.role_targets(view, book)
        current = {role: self._role_weight(role, book) for role in self.vehicles}
        if book.units:
            if not self._outside(targets, current):
                return None
            aware = self.tax_aware
            if aware is not None and aware.benefit_fraction is not None and self.cash_role:
                targets = self._hold_costly_reductions(
                    targets, scales, current, book, self.cash_role, aware.benefit_fraction
                )
                # A held reduction alone is no reason to trade the other roles.
                if not self._outside(targets, current):
                    return None
        return self._vehicle_weights(targets, book)

    def _outside(self, targets: Mapping[str, Decimal], current: Mapping[str, Decimal]) -> bool:
        """Whether a role sits outside its outer band, or a role without a target is held."""
        return any(
            abs(current[role] - target) > self.bands.width(target)
            for role, target in targets.items()
        ) or any(current[role] > ZERO for role in set(self.vehicles) - set(targets))

    def _role_weight(self, role: str, book: Book) -> Decimal:
        return sum((book.weight(key) for key in self.vehicles[role].keys), ZERO)

    # ── tax-aware execution ──

    def _sale_order(self, role: str, book: Book) -> list[str]:
        """The role's held vehicles, the one with the smaller gain for its value first."""

        def gain_share(key: str) -> Decimal:
            value = book.value(key)
            return (value - book.basis(key)) / value

        held = [key for key in self.vehicles[role].keys if book.value(key) > ZERO]
        return sorted(held, key=lambda key: (gain_share(key), key))

    def _sale_plan(self, role: str, amount: Decimal, book: Book) -> list[tuple[str, Decimal]]:
        """The value to sell from each vehicle to take ``amount`` out of ``role``."""
        plan: list[tuple[str, Decimal]] = []
        left = amount
        for key in self._sale_order(role, book):
            if left <= ZERO:
                break
            take = min(left, book.value(key))
            plan.append((key, take))
            left -= take
        return plan

    def _hold_costly_reductions(
        self,
        targets: dict[str, Decimal],
        scales: Mapping[str, Decimal],
        current: Mapping[str, Decimal],
        book: Book,
        cash_role: str,
        benefit_fraction: Decimal,
    ) -> dict[str, Decimal]:
        """A trend reduction whose tax exceeds its assumed benefit is not made: the role keeps
        its weight and the cash role gives up what it would have received."""
        held = dict(targets)
        kept = ZERO
        for role, target in targets.items():
            if role == cash_role or scales[role] >= ONE or current[role] <= target:
                continue
            amount = (current[role] - target) * book.investable
            tax = book.sales_tax(
                [
                    (key, book.units_for(key, value))
                    for key, value in self._sale_plan(role, amount, book)
                ]
            )
            if tax > benefit_fraction * amount:
                held[role] = current[role]
                kept += current[role] - target
        if kept == ZERO:
            return held
        held[cash_role] -= min(kept, held[cash_role])
        excess = sum(held.values(), ZERO) - ONE
        if excess <= ZERO:
            return held
        # The cash role could not give it all: the roles still trading give up the rest, and
        # if they cannot (tax owed leaves less to invest than is held), every role scales down.
        free = [
            role
            for role in held
            if role != cash_role and held[role] != current[role] and held[role] > ZERO
        ]
        pool = sum((held[role] for role in free), ZERO)
        if pool >= excess:
            for role in free:
                held[role] -= excess * held[role] / pool
            return held
        for role in free:
            held[role] = ZERO
        total = sum(held.values(), ZERO)
        return {role: weight / total for role, weight in held.items()}

    def _buy_vehicle(self, role: str, book: Book) -> str:
        """Where a purchase for ``role`` goes: a vehicle already held first, then the primary;
        with tax-aware execution, the first that would not defer a loss (else the first)."""
        vehicles = self.vehicles[role]
        held = [key for key in vehicles.keys if book.value(key) > ZERO]
        order = held + [key for key in vehicles.keys if key not in held]
        if self.tax_aware is None or vehicles.partner is None:
            return order[0]
        for key in order:
            if not book.purchase_defers_loss(key):
                return key
        return order[0]

    def _vehicle_weights(self, targets: Mapping[str, Decimal], book: Book) -> dict[str, Decimal]:
        """Each vehicle's weight, rounded down to the weight quantum: a residue of the arithmetic
        above can never make them sum above 1."""
        if book.investable <= ZERO:
            return {
                self.vehicles[role].primary: share(w, ONE)
                for role, w in targets.items()
                if w > ZERO
            }
        weights: dict[str, Decimal] = {}
        for role, target in targets.items():
            vehicles = self.vehicles[role]
            values = {key: book.value(key) for key in vehicles.keys}
            wanted = target * book.investable
            change = wanted - sum(values.values(), ZERO)
            if change > ZERO:
                key = self._buy_vehicle(role, book)
                values[key] += change
            elif change < ZERO:
                for key, value in self._sale_plan(role, -change, book):
                    values[key] -= value
            for key, value in values.items():
                if value > ZERO:
                    weights[key] = share(value, book.investable)
        return weights


# ── building a strategy from its specification ──


def environment_budgets(
    environments: Mapping[str, Sequence[str]], budgets: Mapping[str, Decimal]
) -> dict[str, Decimal]:
    """Each role's share of risk: every environment's budget split equally among its roles,
    a role in two environments taking a share of each."""
    shares: dict[str, Decimal] = {}
    for environment, roles in environments.items():
        for role in roles:
            shares[role] = shares.get(role, ZERO) + budgets[environment] / len(roles)
    return shares


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
    solution = risk_budget_weights(
        covariance_from(vols, corr),
        [float(shares[role]) for role in roles],
        tolerance=solver.tolerance,
        max_iterations=solver.max_iterations,
    )
    if not solution.converged:
        raise PreregistrationError(
            f"strategy {spec.name}: the risk budget did not converge in "
            f"{solution.iterations} sweeps"
        )
    weights = [Decimal(repr(w)).quantize(WEIGHT_QUANTUM) for w in solution.weights]
    # The rounding remainder goes to the largest weight so that the weights sum to exactly 1.
    largest = max(range(len(weights)), key=lambda i: weights[i])
    weights[largest] += ONE - sum(weights, ZERO)
    return dict(zip(roles, weights, strict=True))


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
) -> FixedWeights | BuyAndHold | CoreStrategy:
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
    return CoreStrategy(
        name=spec.name,
        weights=static_weights(spec, core, settings),
        vehicles={
            role: RoleVehicles(vehicles.primary, vehicles.partner if aware else None)
            for role, vehicles in study.roles.items()
        },
        bands=Bands(core.bands.absolute, core.bands.relative),
        cash_role=core.cash_role,
        tilt=tilt_rule(core) if tilt else None,
        tax_aware=TaxAware(benefit) if aware else None,
        scaling=total_risk(spec, core) if isinstance(spec, TotalRiskSpec) else None,
    )
