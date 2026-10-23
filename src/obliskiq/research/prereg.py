"""A pre-registration file: its machine-readable front matter and its prose (DECISIONS D63).

A pre-registration is a Markdown file from ``research/preregistrations/TEMPLATE.md``. Its front
matter, between two ``---`` lines at the top, holds every field D63 names — the mechanism, the
universe, the data and their point-in-time status, the primary metric, the horizon, theta, the
power inputs, the adjustment, the robustness checks and the trial budget — in a form the harness
runs; the prose below explains them. Nothing has a default the harness picks silently: an
optional field left out takes the value the configuration names (D62's alpha and target power),
and the report says which.

A portfolio comparison tests several candidates, each against one reference (one test each,
Bonferroni across them). The core's variants (MASTER_PLAN §5.4) name roles rather than
instruments: ``study.roles`` gives each role its primary vehicle and swap partner, and
``study.core`` the environments, the long-run risk the static weights are computed from, the
bands, the trend tilt and the tax-aware rule. A frontier (``frontier``) and the owner's stated
aspiration (``aspiration``) are reported beside the tests; ``structure`` registers a pre-tax
check of the tilt on the longest history, which is descriptive and never an outcome.

Scaling total risk (DECISIONS D92) adds a strategy, ``total_risk``: fixed relative weights among
the risky roles (its base) and a risky share of a target volatility over the forecast one,
capped at one, with a floor and a largest change; the rest in the cash role. Its estimator is one
of ``study.core.scaling.estimators`` (``quant.covariance``) or the declared matrix itself (a
declared twin). ``study.twins`` are portfolios simulated and reported beside the candidates but
tested against the reference only through ``comparisons``, the secondary tests (each a portfolio
against another, with its own theta). The study may mark the account on daily closes
(``marks``), state the owner's point (a drawdown limit, the largest share of paths reaching it
over a horizon) and a grid of horizons, a block-length sensitivity, a long-history check, a
forecast check of each estimator (never an outcome) and a development window for exploration.
A field left out is not registered; the locked pre-registrations of earlier studies leave them
out.

A **cross-section** (``kind: cross_section``, MASTER_PLAN §5.9) tests a selection signal: a
universe of members held through one kind of vehicle, each a member over stated spans (a member
the vendor does not serve is named with no series, and counts as lost to survivorship), a
signal, a selection rule and named portfolios built from them; each test compares a candidate
portfolio with another as the Fama-MacBeth series of the candidate's monthly excess log return
after costs and tax. Exploration runs on the development sample, the confirmation run on the
later confirmation sample.

A **forward test** (``kind: forward_test``, DECISIONS D70) tests a signal whose evidence can only
be produced live, such as an LLM's reading of news: the outputs of one model and prompt
(``segment``), stamped when produced, each scored on the excess return that followed its first
close against the price signal (``controls``), up to an evaluation date fixed before any output
existed. It has no development sample and no exploratory trial.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import Field, NonNegativeInt, PositiveInt, ValidationError, model_validator

from ..config.base import FrozenModel
from ..config.loader import strict_yaml
from ..config.schema_costs import Fraction, Rate
from ..domain.base import CountryCode, CurrencyCode, NonEmptyText
from ..domain.enums import AssetClass
from ..domain.research import PointInTime
from ..domain.tax import Isin, SecurityKind
from ..errors import PreregistrationError
from ..quant.covariance import EstimatorRule
from .registry import file_sha256

#: The line that opens and closes the front matter.
FRONT_MATTER_FENCE = "---"
#: Months in a year (a reset month is one of them).
MONTHS_PER_YEAR = 12
#: The estimator a declared twin names: the declared matrix itself.
DECLARED = "declared"
#: A block-length sensitivity compares at least this many lengths (D92 rule 3).
MIN_SENSITIVITY_LENGTHS = 3
#: Twice the longest lookback must fit in this share of the sample for blocks to respect it.
LOOKBACK_SAMPLE_SHARE = Decimal(1) / 3

type Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
type Key = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+$")]
type Share = Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False)]
type Weight = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
type Correlation = Annotated[Decimal, Field(ge=-1, le=1, allow_inf_nan=False)]


class SecuritySpec(FrozenModel):
    kind: SecurityKind
    isin: Isin | None
    issuer: NonEmptyText | None
    tracked_index: NonEmptyText | None
    listed: bool


class InstrumentSpec(FrozenModel):
    """One instrument of the study and where its history is."""

    key: Key
    price_series: NonEmptyText
    income_series: NonEmptyText | None
    currency: CurrencyCode
    venue: NonEmptyText
    asset_class: AssetClass
    issuer_country: CountryCode | None
    income_country: CountryCode | None
    market_cap_usd: Positive | None
    # The vehicle's yearly cost, deducted from an index proxy day by day; null for a price
    # that already carries its fund's costs.
    expense_ratio: Fraction | None
    security: SecuritySpec


class FixedWeightsSpec(FrozenModel):
    name: NonEmptyText
    strategy: Literal["fixed_weights"]
    weights: dict[Key, Weight]
    simplicity: PositiveInt


class BuyAndHoldSpec(FrozenModel):
    name: NonEmptyText
    strategy: Literal["buy_and_hold"]
    weights: dict[Key, Weight]
    simplicity: PositiveInt


class BandedWeightsSpec(FrozenModel):
    """Static weights by role, kept within the core's bands."""

    name: NonEmptyText
    strategy: Literal["banded_weights"]
    weights: dict[Key, Weight]
    simplicity: PositiveInt


class RiskBalanceSpec(FrozenModel):
    """Static weights from risk budgets by environment (A, B, C), with or without the tilt."""

    name: NonEmptyText
    strategy: Literal["risk_balance"]
    budgets: dict[Key, Share]
    tilt: bool
    simplicity: PositiveInt


class EquityOverlaySpec(FrozenModel):
    """One equity role at ``fraction``, the rest in cash, with or without the tilt (D)."""

    name: NonEmptyText
    strategy: Literal["equity_overlay"]
    equity: Key
    fraction: Share
    tilt: bool
    simplicity: PositiveInt


class TotalRiskSpec(FrozenModel):
    """Fixed relative weights among the risky roles (``base``) held at a risky share of
    ``target_volatility`` over the forecast volatility of the base, capped at one, at least
    ``floor`` and moved by at most ``max_change`` from the share held; the rest in the cash role
    (DECISIONS D92). ``estimator`` names one of ``study.core.scaling.estimators``, or
    ``declared``: the declared matrix, a constant forecast and so a static portfolio, the
    specification's declared twin. ``tax_aware`` buys through swap partners when the primary
    would defer a loss; ``hold_costly_reductions`` also holds a reduction whose tax exceeds the
    core's benefit fraction of the value sold."""

    name: NonEmptyText
    strategy: Literal["total_risk"]
    base: dict[Key, Share]
    target_volatility: Positive
    floor: Fraction
    max_change: Share
    estimator: Key
    tax_aware: bool
    hold_costly_reductions: bool
    simplicity: PositiveInt

    @model_validator(mode="after")
    def _a_base(self) -> Self:
        if sum(self.base.values()) != 1:
            raise ValueError(f"strategy {self.name}'s base weights must sum to 1")
        if self.hold_costly_reductions and not self.tax_aware:
            raise ValueError(f"strategy {self.name} holds reductions only when tax-aware")
        return self

    @property
    def declared(self) -> bool:
        return self.estimator == DECLARED


type StrategySpec = Annotated[
    FixedWeightsSpec
    | BuyAndHoldSpec
    | BandedWeightsSpec
    | RiskBalanceSpec
    | EquityOverlaySpec
    | TotalRiskSpec,
    Field(discriminator="strategy"),
]
type CoreStrategySpec = BandedWeightsSpec | RiskBalanceSpec | EquityOverlaySpec | TotalRiskSpec


class RoleSpec(FrozenModel):
    """The instruments that hold a role: its primary vehicle and its swap partner."""

    primary: Key
    partner: Key | None


class RiskSpec(FrozenModel):
    """The long-run risk the static weights are computed from, in the base currency, declared
    before the data: each role's yearly volatility and every pair's correlation."""

    volatilities: dict[Key, Positive]
    correlations: list[tuple[Key, Key, Correlation]]

    @model_validator(mode="after")
    def _every_pair_once(self) -> Self:
        roles = list(self.volatilities)
        if any(a == b for a, b, _ in self.correlations):
            raise ValueError("a correlation pairs two different roles")
        pairs = [frozenset((a, b)) for a, b, _ in self.correlations]
        expected = {frozenset((a, b)) for i, a in enumerate(roles) for b in roles[i + 1 :]}
        if len(pairs) != len(set(pairs)) or set(pairs) != expected:
            raise ValueError("every pair of roles with a volatility needs exactly one correlation")
        return self

    def matrix(self, roles: list[str]) -> tuple[list[float], list[list[float]]]:
        """Volatilities and the correlation matrix in the order of ``roles``."""
        rho = {frozenset((a, b)): float(c) for a, b, c in self.correlations}
        vols = [float(self.volatilities[role]) for role in roles]
        corr = [[1.0 if a == b else rho[frozenset((a, b))] for b in roles] for a in roles]
        return vols, corr


class BandsSpec(FrozenModel):
    absolute: Rate
    relative: Share


class TiltSpec(FrozenModel):
    momentum_months: PositiveInt
    average_months: Annotated[int, Field(ge=2)]
    hysteresis: Fraction
    floor: Share


class TaxAwareSpec(FrozenModel):
    benefit_fraction: Rate


class EstimatorSpec(FrozenModel):
    """An estimator of daily covariance (``quant.covariance``), its lengths in trading days:
    an EWMA's half-life, a window, the EWMA's share of their blend, Ledoit-Wolf shrinkage toward
    the declared matrix or none, and the prior's weight in equivalent observations."""

    half_life_days: Positive | None
    window_days: PositiveInt | None
    ewma_share: Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
    shrinkage: Literal["ledoit_wolf", "none"]
    prior_observations: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        self.rule()
        return self

    def rule(self, scale: float = 1.0) -> EstimatorRule:
        """The rule in observations of a series with ``scale`` observations a trading day (a
        monthly series has about 12 / 261); a window is at least two observations."""
        try:
            return EstimatorRule(
                half_life=None
                if self.half_life_days is None
                else float(self.half_life_days) * scale,
                window=None
                if self.window_days is None
                else max(2, round(self.window_days * scale)),
                ewma_share=float(self.ewma_share),
                shrinkage=self.shrinkage == "ledoit_wolf",
                prior_observations=float(self.prior_observations) * scale,
            )
        except ValueError as exc:
            raise ValueError(f"estimator: {exc}") from None


class ScalingSpec(FrozenModel):
    """What the total-risk strategies share: the first day of the daily data their estimators
    read (the warm-up starts there), the trading days in a year on the daily calendar (the
    declared yearly matrix over them is its daily target) and the estimators by name."""

    estimation_start: date
    periods_per_year: PositiveInt
    estimators: dict[Key, EstimatorSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _names(self) -> Self:
        if DECLARED in self.estimators:
            raise ValueError(f"'{DECLARED}' names the declared matrix, not an estimator")
        return self

    def lookback_months(self, estimator: str) -> Decimal:
        """An estimator's lookback in months (D92 rule 3); the declared matrix reads none."""
        if estimator == DECLARED:
            return Decimal(0)
        days = Decimal(repr(self.estimators[estimator].rule().lookback))
        return days * MONTHS_PER_YEAR / self.periods_per_year


class CoreSpec(FrozenModel):
    """What the core's variants share (MASTER_PLAN §5.4). ``tilt`` and ``tax_aware`` are needed
    by the strategies that tilt or trade tax-aware, ``scaling`` by those that scale total
    risk."""

    cash_role: Key
    environments: dict[Key, list[Key]]
    risk: RiskSpec
    bands: BandsSpec
    tilt: TiltSpec | None = None
    tax_aware: TaxAwareSpec | None = None
    scaling: ScalingSpec | None = None

    @model_validator(mode="after")
    def _risk_covers_the_environments(self) -> Self:
        members = {role for basket in self.environments.values() for role in basket}
        if not members or any(not basket for basket in self.environments.values()):
            raise ValueError("every environment needs at least one role")
        if set(self.risk.volatilities) != members:
            raise ValueError("the declared risk must cover exactly the roles of the environments")
        if self.cash_role in members:
            raise ValueError("the cash role belongs to no environment")
        return self

    def risk_roles(self) -> list[str]:
        """The roles of the environments, in their first order of appearance."""
        return list(dict.fromkeys(r for basket in self.environments.values() for r in basket))


class StudySpec(FrozenModel):
    """What is simulated: the instruments, the dates, the capital, the reference and the
    candidates tested against it."""

    start: date
    end: date
    rebalance: Literal["month_end"]
    calendar: Key
    initial_capital: Positive
    fx_dataset: NonEmptyText | None
    instruments: list[InstrumentSpec] = Field(min_length=1)
    roles: dict[Key, RoleSpec] | None
    core: CoreSpec | None
    reference: StrategySpec
    candidates: list[StrategySpec] = Field(min_length=1)
    twins: list[StrategySpec] = Field(default_factory=list)

    @property
    def portfolios(self) -> list[StrategySpec]:
        """The reference, the candidates and the twins, in that order."""
        return [self.reference, *self.candidates, *self.twins]

    def lookback_months(self) -> Decimal:
        """The longest lookback any portfolio reads, in months (D92 rule 3): the tilt's window,
        a total-risk strategy's estimator."""
        longest = Decimal(0)
        for strategy in self.portfolios:
            if self.core is None:
                break
            if isinstance(strategy, TotalRiskSpec) and self.core.scaling is not None:
                longest = max(longest, self.core.scaling.lookback_months(strategy.estimator))
            elif (
                isinstance(strategy, RiskBalanceSpec | EquityOverlaySpec)
                and strategy.tilt
                and self.core.tilt is not None
            ):
                tilt = self.core.tilt
                longest = max(longest, Decimal(max(tilt.momentum_months + 1, tilt.average_months)))
        return longest

    def months(self) -> int:
        """The months from the first month end to the horizon."""
        return (
            (self.end.year - self.start.year) * MONTHS_PER_YEAR + self.end.month - self.start.month
        )

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must follow start")
        keys = [instrument.key for instrument in self.instruments]
        if len(keys) != len(set(keys)):
            raise ValueError("instrument keys must be unique")
        if self.calendar not in keys:
            raise ValueError(f"calendar {self.calendar} is not one of the instruments")
        names = [strategy.name for strategy in self.portfolios]
        if len(names) != len(set(names)):
            raise ValueError("the reference and every candidate need distinct names")
        for strategy in self.portfolios:
            if isinstance(strategy, FixedWeightsSpec | BuyAndHoldSpec):
                unknown = sorted(set(strategy.weights) - set(keys))
                if unknown:
                    raise ValueError(
                        f"strategy {strategy.name} weights unknown instruments {unknown}"
                    )
            else:
                self._check_core_strategy(strategy, keys)
        return self

    def _check_core_strategy(self, strategy: CoreStrategySpec, keys: list[str]) -> None:
        if self.roles is None or self.core is None:
            raise ValueError(f"strategy {strategy.name} needs study.roles and study.core")
        for role, vehicles in self.roles.items():
            named_keys = {vehicles.primary} | ({vehicles.partner} if vehicles.partner else set())
            unknown = sorted(named_keys - set(keys))
            if unknown:
                raise ValueError(f"role {role} names unknown instruments {unknown}")
            if vehicles.partner == vehicles.primary:
                raise ValueError(f"role {role}'s swap partner is its primary vehicle")
        core = self.core
        named = set(core.risk_roles()) | {core.cash_role}
        unknown_roles = sorted(named - set(self.roles))
        if unknown_roles:
            raise ValueError(f"the core names roles without vehicles: {unknown_roles}")
        if isinstance(strategy, BandedWeightsSpec):
            unknown_roles = sorted(set(strategy.weights) - set(self.roles))
            if unknown_roles:
                raise ValueError(f"strategy {strategy.name} weights unknown roles {unknown_roles}")
            if sum(strategy.weights.values()) > 1:
                raise ValueError(f"strategy {strategy.name} weights sum above 1")
        elif isinstance(strategy, TotalRiskSpec):
            self._check_total_risk(strategy, core)
        elif isinstance(strategy, RiskBalanceSpec):
            if set(strategy.budgets) != set(core.environments):
                raise ValueError(
                    f"strategy {strategy.name} needs one budget per environment "
                    f"({', '.join(sorted(core.environments))})"
                )
            if sum(strategy.budgets.values()) != 1:
                raise ValueError(f"strategy {strategy.name}'s budgets must sum to 1")
        elif strategy.equity not in self.roles or strategy.equity == core.cash_role:
            raise ValueError(f"strategy {strategy.name} names {strategy.equity}, not a risky role")
        tilts = isinstance(strategy, RiskBalanceSpec | EquityOverlaySpec) and strategy.tilt
        if tilts and (core.tilt is None or core.tax_aware is None):
            raise ValueError(f"strategy {strategy.name} tilts: study.core.tilt and tax_aware")

    @staticmethod
    def _check_total_risk(strategy: TotalRiskSpec, core: CoreSpec) -> None:
        risky = set(core.risk.volatilities)
        unknown = sorted(set(strategy.base) - risky)
        if unknown:
            raise ValueError(
                f"strategy {strategy.name}'s base holds roles without a declared risk: {unknown}"
            )
        if core.scaling is None:
            raise ValueError(f"strategy {strategy.name} scales total risk: study.core.scaling")
        if not strategy.declared and strategy.estimator not in core.scaling.estimators:
            raise ValueError(f"strategy {strategy.name} names no estimator {strategy.estimator}")
        if strategy.hold_costly_reductions and core.tax_aware is None:
            raise ValueError(
                f"strategy {strategy.name} holds costly reductions: study.core.tax_aware"
            )


class SensitivitySpec(FrozenModel):
    """Block lengths the bootstrap is repeated with (D92 rule 3), each with ``n_boot`` paths."""

    block_months: list[PositiveInt] = Field(min_length=MIN_SENSITIVITY_LENGTHS)
    n_boot: PositiveInt

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        if len(set(self.block_months)) != len(self.block_months):
            raise ValueError("the sensitivity's block lengths must be distinct")
        return self


class BootstrapSpec(FrozenModel):
    n_boot: PositiveInt
    block_months: PositiveInt
    seed: Annotated[int, Field(ge=0)]
    sensitivity: SensitivitySpec | None = None


class AdequacySpec(FrozenModel):
    """The checks the data must pass before anything is inferred (D62)."""

    min_coverage: Annotated[Decimal, Field(gt=0, le=1)]
    point_in_time: list[PointInTime] = Field(min_length=1)
    min_effective_observations: PositiveInt


class PowerSpec(FrozenModel):
    """What the power calculation assumes before the data are read: the standard deviation of
    one observation (one name in one period) of the primary metric, the periods, how many
    periods make a year, the names per period and their average correlation. ``by_candidate``
    states a candidate's own standard deviation where it differs."""

    sd_per_period: Positive
    by_candidate: dict[NonEmptyText, Positive] | None
    periods: PositiveInt
    periods_per_year: PositiveInt
    names_per_period: PositiveInt
    rho: Annotated[Decimal, Field(ge=-1, le=1)]


class FrontierSpec(FrozenModel):
    """The breach probabilities that, with ``drawdown_limits``, make the frontier's grid."""

    breach_probabilities: list[Share] = Field(min_length=1)


class StructureSpec(FrozenModel):
    """A pre-tax check of the tilt on the longest history: an equity market's monthly return
    (the sum of ``market_series``) against cash (``cash_series``), rebalanced monthly at
    ``cost_per_leg``. Descriptive: never an outcome and never in the Bonferroni count."""

    market_series: list[NonEmptyText] = Field(min_length=1)
    cash_series: NonEmptyText
    start: date
    end: date
    cost_per_leg: Fraction
    point_in_time: list[PointInTime] = Field(min_length=1)
    subperiods: list[tuple[date, date]]

    @model_validator(mode="after")
    def _inside(self) -> Self:
        if self.end <= self.start:
            raise ValueError("the structure check's end must follow its start")
        for first, last in self.subperiods:
            if not self.start <= first < last <= self.end:
                raise ValueError(f"structure subperiod {first} to {last} is outside its span")
        return self


class ComparisonSpec(FrozenModel):
    """A secondary test: ``candidate``'s annualised log growth of liquidation value after costs
    and tax minus ``against``'s, paired, with its theta and the standard deviation a month the
    power calculation assumes before data."""

    name: NonEmptyText
    candidate: NonEmptyText
    against: NonEmptyText
    theta: Positive
    theta_justification: NonEmptyText
    sd_per_period: Positive


class OwnerPointSpec(FrozenModel):
    """The owner's point (D91): a drawdown limit reached on at most ``probability`` of the paths
    within ``horizon_years``."""

    limit: Annotated[Decimal, Field(gt=0, lt=1)]
    probability: Share
    horizon_years: PositiveInt


class SeriesSumSpec(FrozenModel):
    """A role's history: one ``level`` series, or ``returns`` series (decimal, simple) summed
    period by period (an excess return and a bill)."""

    kind: Literal["level", "returns"]
    series: list[NonEmptyText] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_level(self) -> Self:
        if self.kind == "level" and len(self.series) != 1:
            raise ValueError("a level history is one series")
        return self


class LongHistorySpec(FrozenModel):
    """A longer history for the breach at the owner's point (D92 rule 4): each role's series
    and the cash's, before tax at a flat cost a leg, its own bootstrap. The estimators' lengths
    in trading days become this history's observations through ``periods_per_year``; the
    estimators read its data from ``data_start`` and its months are evaluated from ``start``."""

    name: NonEmptyText
    frequency: Literal["daily", "monthly"]
    periods_per_year: PositiveInt
    currency: NonEmptyText
    data_start: date
    start: date
    end: date
    roles: dict[Key, SeriesSumSpec] = Field(min_length=1)
    cash: SeriesSumSpec
    point_in_time: list[PointInTime] = Field(min_length=1)
    cost_per_leg: Fraction
    block_months: PositiveInt
    n_boot: PositiveInt
    biases: list[NonEmptyText] = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not self.data_start <= self.start < self.end:
            raise ValueError(f"long history {self.name}: data_start <= start < end")
        return self


class ForecastCheckSpec(FrozenModel):
    """Each estimator's forecast of its base's next-month realised variance, scored by a loss
    against the declared matrix's constant forecast: a check that the estimator works, never an
    outcome (D92)."""

    loss: Literal["qlike"]


class PreregSpec(FrozenModel):
    """Every field of a pre-registration (D63)."""

    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$")]
    title: NonEmptyText
    kind: Literal["portfolio_comparison"]
    mechanism: NonEmptyText
    citation: NonEmptyText
    universe: NonEmptyText
    data_status: NonEmptyText
    primary_metric: Literal["log_growth_difference"]
    theta: Positive
    theta_justification: NonEmptyText
    alpha: Rate | None
    target_power: Rate | None
    tests_in_confirmation: PositiveInt
    adjustment: NonEmptyText
    trial_budget: NonNegativeInt
    study: StudySpec
    bootstrap: BootstrapSpec
    drawdown_limits: list[Annotated[Decimal, Field(gt=0, lt=1)]] = Field(min_length=1)
    frontier: FrontierSpec | None
    aspiration: list[Positive] | None
    subperiods: list[tuple[date, date]]
    cost_uncertainty_multiplier: Positive
    robustness: NonEmptyText
    adequacy: AdequacySpec
    power: PowerSpec
    structure: StructureSpec | None
    comparisons: list[ComparisonSpec] = Field(default_factory=list)
    marks: Literal["month_end", "daily"] = "month_end"
    owner_point: OwnerPointSpec | None = None
    breach_horizons_years: list[PositiveInt] = Field(default_factory=list)
    long_history: list[LongHistorySpec] = Field(default_factory=list)
    forecast_check: ForecastCheckSpec | None = None
    development: SampleSpec | None = None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        for first, last in self.subperiods:
            if not self.study.start <= first < last <= self.study.end:
                raise ValueError(f"subperiod {first} to {last} is not inside the study")
        tests = len(self.study.candidates) + len(self.comparisons)
        if self.tests_in_confirmation != tests:
            raise ValueError(
                f"tests_in_confirmation is {self.tests_in_confirmation} but the study tests "
                f"{len(self.study.candidates)} candidate(s) and {len(self.comparisons)} "
                "comparison(s), one test each"
            )
        names = {candidate.name for candidate in self.study.candidates}
        unknown = sorted(set(self.power.by_candidate or {}) - names)
        if unknown:
            raise ValueError(f"power.by_candidate names no candidate: {unknown}")
        if self.structure is not None and (self.study.core is None or self.study.core.tilt is None):
            raise ValueError("the structure check uses the core's tilt: study.core.tilt is needed")
        self._check_comparisons()
        self._check_breach()
        self._check_blocks()
        self._check_checks()
        return self

    def _check_comparisons(self) -> None:
        portfolios = [strategy.name for strategy in self.study.portfolios]
        tests = [candidate.name for candidate in self.study.candidates]
        for comparison in self.comparisons:
            unknown = sorted({comparison.candidate, comparison.against} - set(portfolios))
            if unknown:
                raise ValueError(f"comparison {comparison.name} names unknown portfolios {unknown}")
            if comparison.candidate == comparison.against:
                raise ValueError(f"comparison {comparison.name} compares a portfolio with itself")
            tests.append(comparison.name)
        if len(tests) != len(set(tests)):
            raise ValueError("every candidate and comparison needs a distinct name")

    def _check_breach(self) -> None:
        point = self.owner_point
        if point is not None and point.limit not in self.drawdown_limits:
            raise ValueError("the owner's limit must be one of drawdown_limits")
        if self.breach_horizons_years and point is None:
            raise ValueError("breach_horizons_years needs the owner's point")

    def _check_blocks(self) -> None:
        """D92 rule 3, for a study that scales total risk: blocks of at least twice the longest
        lookback, unless twice it exceeds a third of the sample (the report then says so)."""
        if not any(isinstance(s, TotalRiskSpec) for s in self.study.portfolios):
            return
        needed = 2 * self.study.lookback_months()
        respectable = needed <= LOOKBACK_SAMPLE_SHARE * self.study.months()
        if respectable and self.bootstrap.block_months < needed:
            raise ValueError(
                f"blocks of {self.bootstrap.block_months} months are shorter than twice the "
                f"longest lookback ({needed:.1f} months; DECISIONS D92 rule 3)"
            )

    def _check_checks(self) -> None:
        core = self.study.core
        scaled = [s for s in self.study.portfolios if isinstance(s, TotalRiskSpec)]
        if self.forecast_check is not None and not any(not s.declared for s in scaled):
            raise ValueError("the forecast check needs a total-risk strategy with an estimator")
        if self.long_history and not scaled:
            raise ValueError("the long-history check runs total-risk strategies")
        if core is not None and self.long_history:
            for history in self.long_history:
                unknown = sorted(set(history.roles) - set(core.risk.volatilities))
                if unknown:
                    raise ValueError(f"long history {history.name} names roles {unknown}")
        if core is not None and core.scaling is not None:
            first = self.study.start
            if self.development is not None:
                first = min(first, self.development.start)
            if core.scaling.estimation_start > first:
                raise ValueError(
                    "the scaling's estimation_start must be on or before the study's start and "
                    "the development window's"
                )
        if self.development is not None:
            if not self.comparisons:
                raise ValueError("exploration on a development window runs the comparisons")
            if self.development.end >= self.study.start:
                raise ValueError("the development window must end before the study starts")

    def sd_per_period(self, candidate: str) -> Decimal:
        """The power calculation's standard deviation for ``candidate``."""
        return (self.power.by_candidate or {}).get(candidate, self.power.sd_per_period)


# ── a cross-section ──


class VehicleSpec(FrozenModel):
    """How every member of a cross-section is held: one kind of vehicle, its listing venue and
    currency, its yearly cost (deducted from an index proxy) and the size its friction tier
    reads (null is the illiquid tier, labelled)."""

    currency: CurrencyCode
    venue: NonEmptyText
    asset_class: AssetClass
    market_cap_usd: Positive | None
    expense_ratio: Fraction | None
    kind: SecurityKind
    issuer: NonEmptyText


class MemberSpec(FrozenModel):
    """One member: its series (null when the vendor does not serve it: lost to survivorship)
    and the spans it belongs to the universe, first and last day inclusive (null: still)."""

    key: Key
    name: NonEmptyText
    price_series: NonEmptyText | None
    spans: list[tuple[date, date | None]] = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        previous: date | None = None
        for index, (first, last) in enumerate(self.spans):
            if last is not None and last < first:
                raise ValueError(f"member {self.key}: a span ends before it starts")
            if previous is not None and first <= previous:
                raise ValueError(f"member {self.key}: spans must be ordered and disjoint")
            if last is None and index != len(self.spans) - 1:
                raise ValueError(f"member {self.key}: only the last span may be open")
            previous = last
        return self

    def member_on(self, day: date) -> bool:
        return any(first <= day and (last is None or day <= last) for first, last in self.spans)


class SignalSpec(FrozenModel):
    """Momentum: the return from ``lookback_months`` to ``skip_months`` month ends back."""

    lookback_months: PositiveInt
    skip_months: NonNegativeInt

    @model_validator(mode="after")
    def _skip_inside(self) -> Self:
        if self.skip_months >= self.lookback_months:
            raise ValueError("the skipped months must be fewer than the lookback")
        return self


class SelectionSpec(FrozenModel):
    """The top group: a name enters within the top ``enter_fraction`` of the ranked members and
    is kept while within the top ``keep_fraction``; the group holds ``enter_fraction`` of them.
    Every holding is reset to equal weights at the last rebalance date of ``reset_month``."""

    enter_fraction: Share
    keep_fraction: Share
    reset_month: Annotated[int, Field(ge=1, le=MONTHS_PER_YEAR)]

    @model_validator(mode="after")
    def _keep_wider(self) -> Self:
        if self.keep_fraction < self.enter_fraction:
            raise ValueError("a name is kept at least as long as it would enter")
        return self


class StopSpec(FrozenModel):
    """A trailing protective stop: ``multiple`` times the daily volatility of the last
    ``volatility_sessions`` sessions, scaled to ``horizon_sessions``, below the highest close
    since entry; it fills at the first daily close at or below it (a gap is borne in full)."""

    volatility_sessions: Annotated[int, Field(ge=2)]
    horizon_sessions: PositiveInt
    multiple: Positive


class PortfolioSpec(FrozenModel):
    """A portfolio of the cross-section: the whole ``universe`` or the signal's ``top_group``
    (its signal lagged ``lag_months``, a placebo past the horizon), with or without the stop."""

    rule: Literal["universe", "top_group"]
    lag_months: NonNegativeInt
    stop: bool


class SampleSpec(FrozenModel):
    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("a sample's end must follow its start")
        return self


class CrossSectionStudySpec(FrozenModel):
    """What is simulated: the samples, the members and their vehicle, the signal, the selection,
    the stop and the named portfolios."""

    development: SampleSpec
    confirmation: SampleSpec
    calendar: Key
    initial_capital: Positive
    fx_dataset: NonEmptyText | None
    vehicle: VehicleSpec
    members: list[MemberSpec] = Field(min_length=2)
    signal: SignalSpec
    selection: SelectionSpec
    stop: StopSpec | None
    portfolios: dict[Key, PortfolioSpec] = Field(min_length=2)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.development.end > self.confirmation.start:
            raise ValueError("the development sample must end before the confirmation starts")
        keys = [member.key for member in self.members]
        if len(keys) != len(set(keys)):
            raise ValueError("member keys must be unique")
        served = {m.key for m in self.members if m.price_series is not None}
        if self.calendar not in served:
            raise ValueError(f"calendar {self.calendar} is not a member with a series")
        if self.stop is None and any(p.stop for p in self.portfolios.values()):
            raise ValueError("a portfolio with a stop needs study.stop")
        return self

    def served(self) -> list[MemberSpec]:
        """The members whose series the vendor serves."""
        return [member for member in self.members if member.price_series is not None]


class CrossSectionTestSpec(FrozenModel):
    """One test: ``candidate``'s monthly excess log return over ``against``'s, with its theta
    and the standard deviation per month the power calculation assumes before data."""

    name: NonEmptyText
    candidate: Key
    against: Key
    theta: Positive
    theta_justification: NonEmptyText
    sd_per_period: Positive


class PlaceboSpec(FrozenModel):
    """A portfolio that must not show the effect: compared with ``against`` at the level of the
    confirmation run, it fails as it should while its interval stays below ``test``'s theta."""

    candidate: Key
    against: Key
    test: NonEmptyText


class CrossSectionAdequacySpec(FrozenModel):
    """The checks the data must pass (D62): coverage of the served members on the rebalance
    dates, the share of members lost to survivorship in each year, the point-in-time kinds and
    the months each sample needs."""

    min_coverage: Annotated[Decimal, Field(gt=0, le=1)]
    max_lost_share: Annotated[Decimal, Field(ge=0, lt=1)]
    point_in_time: list[PointInTime] = Field(min_length=1)
    min_effective_observations: PositiveInt
    min_development_observations: PositiveInt


class CrossSectionSpec(FrozenModel):
    """Every field of a cross-section pre-registration (D63)."""

    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$")]
    title: NonEmptyText
    kind: Literal["cross_section"]
    mechanism: NonEmptyText
    citation: NonEmptyText
    universe: NonEmptyText
    data_status: NonEmptyText
    primary_metric: Literal["top_group_excess"]
    alpha: Rate | None
    target_power: Rate | None
    tests_in_confirmation: PositiveInt
    adjustment: NonEmptyText
    trial_budget: NonNegativeInt
    study: CrossSectionStudySpec
    tests: list[CrossSectionTestSpec] = Field(min_length=1)
    placebo: PlaceboSpec | None
    bootstrap: BootstrapSpec
    newey_west_lags: PositiveInt
    periods_per_year: PositiveInt
    subperiods: list[tuple[date, date]]
    cost_uncertainty_multiplier: Positive
    robustness: NonEmptyText
    adequacy: CrossSectionAdequacySpec

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        confirmation = self.study.confirmation
        for first, last in self.subperiods:
            if not confirmation.start <= first < last <= confirmation.end:
                raise ValueError(f"subperiod {first} to {last} is not inside the confirmation")
        if self.tests_in_confirmation != len(self.tests):
            raise ValueError(
                f"tests_in_confirmation is {self.tests_in_confirmation} but {len(self.tests)} "
                "test(s) are registered"
            )
        names = [test.name for test in self.tests]
        if len(names) != len(set(names)):
            raise ValueError("every test needs a distinct name")
        portfolios = self.study.portfolios
        pairs = [(t.candidate, t.against) for t in self.tests]
        if self.placebo is not None:
            pairs.append((self.placebo.candidate, self.placebo.against))
            if self.placebo.test not in names:
                raise ValueError(f"the placebo names no test: {self.placebo.test}")
        for candidate, against in pairs:
            unknown = sorted({candidate, against} - set(portfolios))
            if unknown:
                raise ValueError(f"a comparison names unknown portfolios {unknown}")
            if candidate == against:
                raise ValueError(f"{candidate} is compared with itself")
        return self

    def compared(self) -> list[str]:
        """Every portfolio a test or the placebo compares, in the order first named."""
        pairs = [(t.candidate, t.against) for t in self.tests]
        if self.placebo is not None:
            pairs.append((self.placebo.candidate, self.placebo.against))
        return list(dict.fromkeys(name for pair in pairs for name in pair))


class SegmentSpec(FrozenModel):
    """The LLM outputs a forward test reads: one model and one prompt (DECISIONS D70)."""

    provider: Key
    model_id: NonEmptyText
    prompt_version: PositiveInt
    prompt_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ForwardEventsSpec(FrozenModel):
    """Which outputs are events: their feeds, the extraction status and the feature tested."""

    feeds: list[Key] = Field(min_length=1)
    status: Literal["ok"]
    feature: Literal["tone"]
    overlap: Literal["first_per_instrument_within_horizon"]


class ForwardWindowSpec(FrozenModel):
    """When the sample is read: from the segment's first output to the later of a date and a
    span after that first output (fixed before any output exists)."""

    not_before: date
    months_after_first: PositiveInt


class ForwardBootstrapSpec(FrozenModel):
    n_boot: PositiveInt
    block_event_dates: PositiveInt
    seed: NonNegativeInt


class ForwardAdequacySpec(FrozenModel):
    min_events: PositiveInt
    min_price_coverage: Annotated[Decimal, Field(gt=0, le=1)]


class ForwardPowerSpec(FrozenModel):
    """Power inputs stated before any output: the spread of the outcome and of the feature, and
    the events expected."""

    sd_outcome: Positive
    sd_feature: Positive
    events: PositiveInt


class ForwardTestSpec(FrozenModel):
    """A forward test (``kind: forward_test``) of a signal whose evidence can only be produced
    live (DECISIONS D70): events stamped when produced, each scored on the returns that followed
    its first close, against the price signal it is said to add to."""

    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$")]
    title: NonEmptyText
    kind: Literal["forward_test"]
    hypothesis: Annotated[str, Field(pattern=r"^H[0-9]+$")]
    mechanism: NonEmptyText
    citation: NonEmptyText
    universe: NonEmptyText
    data_status: NonEmptyText
    segment: SegmentSpec
    events: ForwardEventsSpec
    entry: Literal["first_close_after_produced"]
    horizon_sessions: PositiveInt
    benchmark: Key
    controls: list[Literal["reaction", "momentum"]] = Field(min_length=1)
    momentum_sessions: PositiveInt
    primary_metric: Literal["excess_log_return_slope"]
    theta: Positive
    theta_justification: NonEmptyText
    alpha: Rate | None
    target_power: Rate | None
    tests_in_confirmation: Literal[1]
    adjustment: NonEmptyText
    trial_budget: Literal[0]
    window: ForwardWindowSpec
    bootstrap: ForwardBootstrapSpec
    subperiods: Literal["halves"]
    placebo: Literal["previous_event_feature"]
    cost_uncertainty_multiplier: Positive
    robustness: NonEmptyText
    adequacy: ForwardAdequacySpec
    power: ForwardPowerSpec


@dataclass(frozen=True, slots=True)
class Prereg:
    path: Path
    sha256: str
    spec: PreregSpec
    body: str


@dataclass(frozen=True, slots=True)
class CrossSectionPrereg:
    path: Path
    sha256: str
    spec: CrossSectionSpec
    body: str


@dataclass(frozen=True, slots=True)
class ForwardTestPrereg:
    path: Path
    sha256: str
    spec: ForwardTestSpec
    body: str


def front_matter(text: str) -> tuple[Any, str]:
    """The YAML between the opening fences, and the prose after them."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONT_MATTER_FENCE:
        raise PreregistrationError("a pre-registration opens with a front matter fence (---)")
    try:
        close = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == FRONT_MATTER_FENCE)
    except StopIteration:
        raise PreregistrationError("the front matter has no closing fence (---)") from None
    try:
        data = strict_yaml("\n".join(lines[1:close]))
    except yaml.YAMLError as exc:
        raise PreregistrationError(f"the front matter is not valid YAML: {exc}") from None
    return data, "\n".join(lines[close + 1 :])


def _read(path: Path) -> tuple[Any, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PreregistrationError(f"{path} cannot be read: {exc}") from None
    return front_matter(text)


def _validate[M: FrozenModel](model: type[M], data: Any, path: Path) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}" for error in exc.errors()
        )
        raise PreregistrationError(
            f"{path.name} is not a valid pre-registration: {problems}"
        ) from None


def load_prereg(path: Path) -> Prereg:
    """Read and validate a portfolio comparison's pre-registration; every problem is named."""
    data, body = _read(path)
    spec = _validate(PreregSpec, data, path)
    return Prereg(path=path, sha256=file_sha256(path), spec=spec, body=body)


def load_any(path: Path) -> Prereg | CrossSectionPrereg | ForwardTestPrereg:
    """Read and validate a pre-registration of any kind, chosen by its ``kind``."""
    data, body = _read(path)
    if isinstance(data, dict) and data.get("kind") == "cross_section":
        spec = _validate(CrossSectionSpec, data, path)
        return CrossSectionPrereg(path=path, sha256=file_sha256(path), spec=spec, body=body)
    if isinstance(data, dict) and data.get("kind") == "forward_test":
        forward = _validate(ForwardTestSpec, data, path)
        return ForwardTestPrereg(path=path, sha256=file_sha256(path), spec=forward, body=body)
    return Prereg(
        path=path, sha256=file_sha256(path), spec=_validate(PreregSpec, data, path), body=body
    )
