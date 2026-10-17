"""Running a pre-registered study: the data it reads, the adequacy checks, the historical runs,
the bootstrap, one test per candidate, the robustness checks, the frontier and the decisions
(MASTER_PLAN §5.4, §5.9; DECISIONS D60, D62, D63).

The one study kind this build runs is a **portfolio comparison**: several candidate strategies,
each against one reference over the same instruments, every portfolio simulated with costs and
tax as they arise and liquidated at the horizon. A candidate's primary effect is its annualised
log growth of liquidation value minus the reference's, after costs and tax, and each candidate is
one test: its interval has level ``1 - 2 alpha / m`` for ``m`` candidates (Bonferroni). The
interval comes from a paired moving-block bootstrap of whole months — every instrument's return,
its income yield and every reference rate resampled together — with every portfolio simulated on
each path, so each path's tax is paid as that path would have paid it. A path keeps the real
history before its first date, which the strategies' lookbacks read.

Beside the tests: every portfolio's run before tax, its distribution of growth, drawdown breaches
and terminal wealth over the paths, and the **frontier** (D60): for each drawdown limit and
breach probability of the pre-registered grid, the portfolio with the highest median growth whose
probability of breaching the limit does not exceed it; portfolios whose growth the bootstrap
cannot tell apart from that one's resolve to the simplest, then the lowest turnover. A
pre-registered **structure check** runs the tilt before tax on the longest history (descriptive).

Everything is read as of the horizon and nothing later: the store bounds every read by
publication date, and the strategies see only what was public on each rebalance date.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Final

import numpy as np

from ..config.schema import ObliskConfig
from ..domain.research import Observation, ResearchSeries
from ..domain.tax import SecurityIdentity
from ..errors import SimulationError
from ..jurisdiction.profile import JurisdictionProfile
from ..jurisdiction.tax_policies import TAX_POLICIES
from ..quant.costs import CostBook
from ..quant.trend import TrendRule
from ..store import Store
from ..store.research import research_observations, research_series_info
from .data import SurvivorshipReport, survivorship
from .decide import AdequacyCheck, Decision, decide
from .prereg import (
    BandedWeightsSpec,
    EquityOverlaySpec,
    InstrumentSpec,
    Prereg,
    RiskBalanceSpec,
    StrategySpec,
    StructureSpec,
)
from .registry import PreregLock, TrialRegistry, canonical_sha256, inputs_sha256, sharpes
from .simulate import (
    EURO,
    IndexedSeries,
    Panel,
    SimInstrument,
    SimulationResult,
    SimulationSettings,
    Simulator,
    Strategy,
)
from .stats import (
    Breakeven,
    DeflatedSharpe,
    block_bootstrap_indices,
    breach_probability,
    breakeven_multiplier,
    deflated_sharpe,
    detectable_effect,
    effective_n,
    moments,
    percentile_interval,
    power,
    sharpe_ratio,
)
from .strategies import build_strategy, static_weights, tilt_rule
from .structure import StructureResult, structure_check

#: What the simulation itself cannot show, stated in every report.
SIMULATION_NOTES: Final = (
    "Monthly marks: drawdowns understate falls inside a month, and no exit inside a month is "
    "simulated.",
    "Trades fill at the rebalance date's close, moved by the cost model's spread and slippage.",
    "Income is credited at the next rebalance date, converted at its ex-date's reference rate.",
    "A year's tax is paid at the first rebalance date on or after its payment date.",
    "The profile's current rules and rate table are applied to every simulated year.",
    "Currency conversions are instant at the reference rate and cost nothing; no currency "
    "balance is held, so gains on balances are not taxed.",
    "Losses still carried forward at the horizon are worth nothing.",
    "A sale whose costs would exceed what it raises (a minimum commission on a few units) is not "
    "made; its units stay held and count as nothing at the horizon.",
    "Bootstrap paths resample whole months of every instrument and rate together; on a path, "
    "income is paid at month end at the resampled month's yield, and the history before the "
    "first date is the real one.",
)
#: What this study kind has no placebo for.
NO_PLACEBO: Final = (
    "not applicable: a portfolio comparison has no signal to lag; its robustness is the sign in "
    "each subperiod and the break-even cost multiplier"
)
#: The terminal-wealth percentile reported beside the median (D60).
TERMINAL_PERCENTILE: Final = 5
#: Shares of the paths per worker process, so that a slow share does not hold the others up.
CHUNKS_PER_WORKER: Final = 4


@dataclass(frozen=True, slots=True)
class StrategyStats:
    """One portfolio: its historical runs after and before tax and its distribution over the
    bootstrap paths."""

    name: str
    simplicity: int
    historical: SimulationResult | None
    pre_tax: SimulationResult | None
    median_log_growth: float | None
    terminal_wealth_p5: float | None
    breach: tuple[tuple[Decimal, float], ...]
    static_weights: Mapping[str, Decimal] | None
    #: Bootstrap paths on which a sale was not made because it cost more than it raised.
    unsold_paths: int | None = None

    @property
    def turnover(self) -> float | None:
        """Value traded a year over the mean mark-to-market value, after tax."""
        run = self.historical
        if run is None or not run.nav or run.years <= 0:
            return None
        return float(run.traded) / float(np.mean(run.nav)) / run.years


@dataclass(frozen=True, slots=True)
class SubperiodEstimate:
    start: date
    end: date
    estimate: float | None
    sign_holds: bool | None


@dataclass(frozen=True, slots=True)
class Robustness:
    subperiods: tuple[SubperiodEstimate, ...]
    breakeven: Breakeven
    cost_uncertainty: Decimal
    breakeven_exceeds_uncertainty: bool
    placebo: str


@dataclass(frozen=True, slots=True)
class CandidateTest:
    """One candidate against the reference: its effect, its interval and its decision."""

    name: str
    estimate: float | None
    se: float | None
    detectable: float | None
    declared_power: float
    declared_detectable: float
    robustness: Robustness | None
    deflated: DeflatedSharpe | None
    decision: Decision


@dataclass(frozen=True, slots=True)
class FrontierPoint:
    """One point of the grid: who respects it, the best of them, and the resolved choice."""

    limit: Decimal
    probability: Decimal
    eligible: tuple[str, ...]
    best: str | None
    indistinguishable: tuple[str, ...]
    chosen: str | None


@dataclass(frozen=True, slots=True)
class StudyResult:
    """Everything a report states about one run."""

    prereg: Prereg
    explore: bool
    lock: PreregLock | None
    run_at: datetime
    alpha: float
    alpha_source: str
    target_power: float
    target_power_source: str
    series: tuple[ResearchSeries, ...]
    inputs_sha256: str
    dates: tuple[date, ...]
    coverage: float | None
    survivorship: SurvivorshipReport
    checks: tuple[AdequacyCheck, ...]
    reference: StrategyStats
    candidates: tuple[StrategyStats, ...]
    tests: tuple[CandidateTest, ...]
    frontier: tuple[FrontierPoint, ...]
    structure: StructureResult | None
    trials: int
    tax_rules: tuple[str, ...]
    cost_labels: tuple[str, ...]

    @property
    def portfolios(self) -> tuple[StrategyStats, ...]:
        return (self.reference, *self.candidates)


# ── reading the data ──


@dataclass(frozen=True, slots=True)
class _Inputs:
    series: tuple[ResearchSeries, ...]
    missing: tuple[str, ...]
    panel: Panel
    dates: tuple[date, ...]


def _series_ids(prereg: Prereg, base: str) -> tuple[list[str], list[str]]:
    study = prereg.spec.study
    ids = list(dict.fromkeys(i.price_series for i in study.instruments))
    ids += [i.income_series for i in study.instruments if i.income_series is not None]
    currencies = (
        sorted({i.currency for i in study.instruments} | {base}) if study.fx_dataset else []
    )
    fx = [f"{study.fx_dataset}.{c}" for c in currencies if c != EURO]
    return ids, fx


def _month_ends(rows: Sequence[Observation], start: date, end: date) -> tuple[date, ...]:
    """The last observed date of each month between ``start`` and ``end``."""
    last: dict[tuple[int, int], date] = {}
    for row in rows:
        day = row.observed_on
        if start <= day <= end and row.available_on <= end:
            key = (day.year, day.month)
            last[key] = max(last.get(key, day), day)
    return tuple(sorted(last.values()))


def _less_costs(
    rows: tuple[Observation, ...], expense_ratio: Decimal | None, days_per_year: float
) -> tuple[Observation, ...]:
    """An index proxy less its vehicle's yearly cost, accrued day by day from its first row."""
    if expense_ratio is None or not rows:
        return rows
    rate = float(expense_ratio)
    anchor = min(row.observed_on for row in rows)
    return tuple(
        row.model_copy(
            update={
                "value": row.value
                * math.exp(-rate * (row.observed_on - anchor).days / days_per_year)
            }
        )
        for row in rows
    )


def gather(store: Store, prereg: Prereg, base: str, days_per_year: float) -> _Inputs:
    """Read every series the study names, as of its horizon; index proxies lose their
    vehicle's expense ratio."""
    study = prereg.spec.study
    as_of = study.end
    ids, fx_ids = _series_ids(prereg, base)
    found: list[ResearchSeries] = []
    missing: list[str] = []
    for series_id in [*ids, *fx_ids]:
        info = research_series_info(store, series_id)
        if info is None:
            missing.append(series_id)
        else:
            found.append(info)

    def rows(series_id: str | None) -> tuple[Observation, ...]:
        if series_id is None or series_id in missing:
            return ()
        return tuple(research_observations(store, series_id, as_of=as_of))

    adjusted: dict[tuple[str, Decimal | None], tuple[Observation, ...]] = {}
    for i in study.instruments:
        # Instruments on one series and cost (a role's two vehicles) share one tuple of rows.
        if (i.price_series, i.expense_ratio) not in adjusted:
            adjusted[(i.price_series, i.expense_ratio)] = _less_costs(
                rows(i.price_series), i.expense_ratio, days_per_year
            )
    prices = {i.key: adjusted[(i.price_series, i.expense_ratio)] for i in study.instruments}
    income = {i.key: rows(i.income_series) for i in study.instruments}
    fx = {series_id.rsplit(".", 1)[1]: rows(series_id) for series_id in fx_ids}
    dates = _month_ends(prices[study.calendar], study.start, study.end)
    return _Inputs(tuple(found), tuple(missing), Panel(prices, income, fx), dates)


def _instruments(specs: Sequence[InstrumentSpec]) -> list[SimInstrument]:
    return [
        SimInstrument(
            key=spec.key,
            venue=spec.venue,
            asset_class=spec.asset_class,
            currency=spec.currency,
            security=SecurityIdentity(
                key=spec.key,
                kind=spec.security.kind,
                isin=spec.security.isin,
                issuer=spec.security.issuer,
                tracked_index=spec.security.tracked_index,
                listed=spec.security.listed,
            ),
            issuer_country=spec.issuer_country,
            market_cap_usd=spec.market_cap_usd,
            income_country=spec.income_country,
        )
        for spec in specs
    ]


def _boundary(
    rows: Sequence[Observation], dates: Sequence[date], max_age: int
) -> list[float | None]:
    """The latest value public at each date, if it is fresh enough."""
    index = IndexedSeries(rows)
    out: list[float | None] = []
    for day in dates:
        latest = index.latest(day, day)
        fresh = latest is not None and day - latest.observed_on <= timedelta(days=max_age)
        out.append(latest.value if fresh and latest is not None else None)
    return out


# ── the bootstrap ──


@dataclass(frozen=True, slots=True)
class _Months:
    """Per month: each instrument's log return and income yield, each rate's log change; and
    the history before the first date, which every path keeps."""

    first_prices: dict[str, float]
    first_rates: dict[str, float]
    returns: dict[str, np.ndarray]
    yields: dict[str, np.ndarray]
    rate_changes: dict[str, np.ndarray]
    prior_prices: dict[str, tuple[Observation, ...]]
    prior_rates: dict[str, tuple[Observation, ...]]


def _months(panel: Panel, dates: Sequence[date], max_price_age: int, max_fx_age: int) -> _Months:
    def levels(rows: Sequence[Observation], age: int, what: str) -> np.ndarray:
        values = _boundary(rows, dates, age)
        if any(v is None or v <= 0 for v in values):
            raise SimulationError(f"{what} lacks a value at a rebalance date")
        return np.array(values, dtype=float)

    def before(rows: Sequence[Observation]) -> tuple[Observation, ...]:
        return tuple(row for row in rows if row.observed_on < dates[0])

    def month_ends_before(rows: Sequence[Observation]) -> tuple[Observation, ...]:
        """Every vintage of the last observed day of each month before the first date: the
        rows a month-end close reads (a strategy's lookback sees no other price)."""
        prior = before(rows)
        last: dict[tuple[int, int], date] = {}
        for row in prior:
            month = (row.observed_on.year, row.observed_on.month)
            last[month] = max(last.get(month, row.observed_on), row.observed_on)
        ends = set(last.values())
        return tuple(row for row in prior if row.observed_on in ends)

    returns, yields, first = {}, {}, {}
    prior: dict[str, tuple[Observation, ...]] = {}
    shared: dict[int, tuple[np.ndarray, np.ndarray, tuple[Observation, ...]]] = {}
    for key, rows in panel.prices.items():
        # One series priced for two instruments is resampled once and shared by both.
        if id(rows) not in shared:
            series_levels = levels(rows, max_price_age, key)
            shared[id(rows)] = (
                series_levels,
                np.diff(np.log(series_levels)),
                month_ends_before(rows),
            )
        prices, returns[key], prior[key] = shared[id(rows)]
        first[key] = float(prices[0])
        paid = [
            sum(
                r.value
                for r in panel.income.get(key, ())
                if dates[k - 1] < r.observed_on <= dates[k] and r.available_on <= dates[k]
            )
            for k in range(1, len(dates))
        ]
        yields[key] = np.array(paid, dtype=float) / prices[:-1]
    rate_changes, first_rates = {}, {}
    for currency, rows in panel.fx.items():
        rates = levels(rows, max_fx_age, f"the {currency} rate")
        first_rates[currency] = float(rates[0])
        rate_changes[currency] = np.diff(np.log(rates))
    return _Months(
        first,
        first_rates,
        returns,
        yields,
        rate_changes,
        prior,
        {currency: before(rows) for currency, rows in panel.fx.items()},
    )


def _synthetic(months: _Months, dates: Sequence[date], path: np.ndarray) -> Panel:
    """A panel on the rebalance dates whose months are ``path``'s months of history, after the
    real history before the first date."""

    def series(first: float, changes: np.ndarray) -> tuple[np.ndarray, tuple[Observation, ...]]:
        levels = first * np.exp(np.concatenate([[0.0], np.cumsum(changes[path])]))
        return levels, tuple(
            Observation(observed_on=d, available_on=d, value=float(v))
            for d, v in zip(dates, levels, strict=True)
        )

    prices: dict[str, tuple[Observation, ...]] = {}
    income: dict[str, tuple[Observation, ...]] = {}
    built: dict[int, tuple[np.ndarray, tuple[Observation, ...]]] = {}
    for key, first in months.first_prices.items():
        changes = months.returns[key]
        if id(changes) not in built:
            levels, resampled = series(first, changes)
            built[id(changes)] = levels, (*months.prior_prices.get(key, ()), *resampled)
        levels, prices[key] = built[id(changes)]
        paid = months.yields[key][path] * levels[:-1]
        income[key] = tuple(
            Observation(observed_on=d, available_on=d, value=float(v))
            for d, v in zip(dates[1:], paid, strict=True)
            if v > 0
        )
    fx = {
        c: (*months.prior_rates.get(c, ()), *series(first, months.rate_changes[c])[1])
        for c, first in months.first_rates.items()
    }
    return Panel(prices, income, fx)


# ── the run ──


type _Simulate = Callable[
    [Panel, Sequence[date], Decimal, Sequence[str]], dict[str, SimulationResult]
]


def _wealth(result: SimulationResult) -> float:
    return float(result.liquidation_value / result.initial)


def _growth(result: SimulationResult) -> float:
    """A run's annualised log growth; a run that ended with nothing has none and stops the study."""
    if result.log_growth is None:
        raise SimulationError(f"{result.strategy} ended with nothing: its growth is not defined")
    return result.log_growth


def _growth_difference(candidate: SimulationResult, reference: SimulationResult) -> float:
    return _growth(candidate) - _growth(reference)


def _not_run(spec: StrategySpec, weights: Mapping[str, Decimal] | None) -> StrategyStats:
    return StrategyStats(spec.name, spec.simplicity, None, None, None, None, (), weights)


@dataclass(frozen=True, slots=True)
class _Draws:
    """Per path and portfolio: growth, worst drawdown, terminal wealth multiple and the sales
    not made because they cost more than they raised."""

    names: tuple[str, ...]
    growth: np.ndarray
    drawdown: np.ndarray
    wealth: np.ndarray
    unsold: np.ndarray

    def column(self, name: str) -> int:
        return self.names.index(name)


def declared_se(prereg: Prereg, candidate: str) -> float:
    """The standard error of the annualised effect before any data: the declared standard
    deviation per observation over the effective observations, scaled to a year."""
    inputs = prereg.spec.power
    per_period = effective_n(inputs.names_per_period, float(inputs.rho))
    observations = inputs.periods * per_period
    sd = float(prereg.spec.sd_per_period(candidate))
    return sd * inputs.periods_per_year / math.sqrt(observations)


class StudyRunner:
    """Runs portfolio comparisons with one configuration and one jurisdiction profile: the tax
    policy and the cost book come from them, and their content enters every run's inputs hash."""

    def __init__(self, settings: ObliskConfig, profile: JurisdictionProfile) -> None:
        self._settings = settings
        self._profile = profile
        self._policy = TAX_POLICIES.resolve(profile, settings)
        self._strategies: dict[str, tuple[list[SimInstrument], dict[str, Strategy]]] = {}
        self._costs = CostBook.from_config(settings)
        self._rules_sha256 = {
            "costs": canonical_sha256(settings.costs.model_dump(mode="json")),
            "tax": canonical_sha256(
                None if profile.tax is None else profile.tax.model_dump(mode="json")
            ),
        }

    def _simulation(self, prereg: Prereg, multiplier: Decimal, taxed: bool) -> SimulationSettings:
        mechanics = self._settings.research.simulation
        return SimulationSettings(
            base_currency=self._settings.account.base_currency,
            initial_capital=prereg.spec.study.initial_capital,
            no_trade_band=mechanics.no_trade_band_fraction,
            max_price_age_days=mechanics.max_price_age_days,
            max_fx_age_days=self._settings.research.data.max_fx_age_days,
            whole_units=mechanics.whole_units,
            days_per_year=float(mechanics.days_per_year),
            cost_multiplier=multiplier,
            taxed=taxed,
        )

    def run(
        self,
        prereg: Prereg,
        store: Store,
        registry: TrialRegistry,
        *,
        explore: bool,
        lock: PreregLock | None,
        now: datetime,
    ) -> StudyResult:
        spec = prereg.spec
        study = spec.study
        decision_defaults = self._settings.research.decision
        alpha = float(spec.alpha if spec.alpha is not None else decision_defaults.alpha)
        alpha_source = "pre-registration" if spec.alpha is not None else "config research.decision"
        target = float(
            spec.target_power if spec.target_power is not None else decision_defaults.target_power
        )
        target_source = (
            "pre-registration" if spec.target_power is not None else "config research.decision"
        )
        tests = spec.tests_in_confirmation
        base = self._settings.account.base_currency
        days_per_year = float(self._settings.research.simulation.days_per_year)
        inputs = gather(store, prereg, base, days_per_year)
        checks = self._checks(prereg, inputs)
        weights = {
            s.name: (
                static_weights(s, study.core, self._settings)
                if study.core is not None
                and isinstance(s, BandedWeightsSpec | RiskBalanceSpec | EquityOverlaySpec)
                else None
            )
            for s in study.portfolios
        }

        def simulate(
            panel: Panel,
            dates: Sequence[date],
            multiplier: Decimal,
            names: Sequence[str],
            *,
            taxed: bool = True,
        ) -> dict[str, SimulationResult]:
            return self._simulate(prereg, panel, dates, multiplier, names, taxed=taxed)

        survivors = survivorship(
            store,
            list(dict.fromkeys(i.price_series for i in study.instruments)),
            start=study.start,
            end=study.end,
            as_of=study.end,
        )
        hashes = {s.series_id: s.content_sha256 for s in inputs.series}
        mechanics = self._simulation(prereg, Decimal(1), True)
        parameters = {
            "alpha": alpha,
            "target_power": target,
            "bootstrap": spec.bootstrap.model_dump(),
            "no_trade_band": str(mechanics.no_trade_band),
            "whole_units": mechanics.whole_units,
            "max_price_age_days": mechanics.max_price_age_days,
            "max_fx_age_days": mechanics.max_fx_age_days,
            "days_per_year": mechanics.days_per_year,
            "base_currency": mechanics.base_currency,
            "policy": self._policy.policy_id,
            "risk_budget": self._settings.numerics.risk_budget.model_dump(),
            **self._rules_sha256,
        }
        digest = inputs_sha256(spec_sha256=prereg.sha256, series=hashes, parameters=parameters)
        coverage = self._coverage(inputs)
        reference_spec = study.reference
        names = [s.name for s in study.portfolios]
        stats = {s.name: _not_run(s, weights[s.name]) for s in study.portfolios}
        estimates: dict[str, float | None] = dict.fromkeys(names)
        intervals: dict[str, tuple[float, float] | None] = dict.fromkeys(names)
        ses: dict[str, float | None] = dict.fromkeys(names)
        robustness: dict[str, Robustness | None] = dict.fromkeys(names)
        deflated: dict[str, DeflatedSharpe | None] = dict.fromkeys(names)
        frontier: tuple[FrontierPoint, ...] = ()
        cost_labels: frozenset[str] = frozenset()
        trials = registry.trials(spec.id)
        if all(check.passed for check in checks):
            try:
                historical = simulate(inputs.panel, inputs.dates, Decimal(1), names)
                pre_tax = simulate(inputs.panel, inputs.dates, Decimal(1), names, taxed=False)
                reference_run = historical[reference_spec.name]
                for name in names[1:]:
                    estimates[name] = _growth_difference(historical[name], reference_run)
                cost_labels = frozenset().union(*(r.cost_labels for r in historical.values()))
                draws = self._bootstrap(prereg, inputs, names)
                for candidate in study.candidates:
                    robustness[candidate.name] = self._robustness(
                        prereg, inputs, candidate.name, reference_spec.name,
                        estimates[candidate.name], simulate,
                    )  # fmt: skip
            except SimulationError as exc:
                checks = (*checks, AdequacyCheck("simulation", False, str(exc)))
                estimates = dict.fromkeys(names)
                robustness = dict.fromkeys(names)
            else:
                ref = draws.column(reference_spec.name)
                for candidate in study.candidates:
                    differences = (
                        draws.growth[:, draws.column(candidate.name)] - draws.growth[:, ref]
                    )
                    intervals[candidate.name] = percentile_interval(
                        differences.tolist(), alpha=alpha, tests=tests
                    )
                    ses[candidate.name] = (
                        float(differences.std(ddof=1)) if differences.size > 1 else None
                    )
                for name in names:
                    stats[name] = self._stats(
                        stats[name], historical[name], pre_tax[name], draws, prereg
                    )
                every = [
                    sharpe_ratio(self._excess(historical[c.name], reference_run))
                    for c in study.candidates
                ]
                pool = [*sharpes(trials), *(s for s in every if s is not None)]
                for candidate in study.candidates:
                    deflated[candidate.name] = self._deflated(
                        historical[candidate.name], reference_run, pool
                    )
                frontier = self._frontier(prereg, stats, draws, alpha=alpha, tests=tests)
        candidate_tests = tuple(
            self._test(
                prereg,
                candidate.name,
                estimates[candidate.name],
                intervals[candidate.name],
                ses[candidate.name],
                robustness[candidate.name],
                deflated[candidate.name],
                checks,
                alpha=alpha,
                tests=tests,
                target=target,
            )
            for candidate in study.candidates
        )
        structure = (
            None
            if spec.structure is None or study.core is None
            else self._structure(prereg, spec.structure, tilt_rule(study.core), store, alpha=alpha)
        )
        return StudyResult(
            prereg=prereg,
            explore=explore,
            lock=lock,
            run_at=now,
            alpha=alpha,
            alpha_source=alpha_source,
            target_power=target,
            target_power_source=target_source,
            series=inputs.series,
            inputs_sha256=digest,
            dates=inputs.dates,
            coverage=coverage,
            survivorship=survivors,
            checks=checks,
            reference=stats[reference_spec.name],
            candidates=tuple(stats[c.name] for c in study.candidates),
            tests=candidate_tests,
            frontier=frontier,
            structure=structure,
            trials=len(trials) + len(study.candidates),
            tax_rules=self._policy.describe(),
            cost_labels=tuple(sorted(cost_labels)),
        )

    # ── the steps ──

    def _test(
        self,
        prereg: Prereg,
        name: str,
        estimate: float | None,
        interval: tuple[float, float] | None,
        se: float | None,
        robustness: Robustness | None,
        deflated: DeflatedSharpe | None,
        checks: tuple[AdequacyCheck, ...],
        *,
        alpha: float,
        tests: int,
        target: float,
    ) -> CandidateTest:
        theta = float(prereg.spec.theta)
        study_power = detectable = None
        if se is not None and se > 0:
            study_power = power(theta, se, alpha=alpha, tests=tests)
            detectable = detectable_effect(se, alpha=alpha, tests=tests, target_power=target)
        before = declared_se(prereg, name)
        return CandidateTest(
            name=name,
            estimate=estimate,
            se=se,
            detectable=detectable,
            declared_power=power(theta, before, alpha=alpha, tests=tests),
            declared_detectable=detectable_effect(
                before, alpha=alpha, tests=tests, target_power=target
            ),
            robustness=robustness,
            deflated=deflated,
            decision=decide(
                interval=interval,
                theta=theta,
                power=study_power,
                target_power=target,
                checks=checks,
            ),
        )

    def _coverage(self, inputs: _Inputs) -> float | None:
        """The share of rebalance dates at which every instrument has a fresh price."""
        if not inputs.dates:
            return None
        age = self._settings.research.simulation.max_price_age_days
        priced = [
            [value is not None for value in _boundary(rows, inputs.dates, age)]
            for rows in inputs.panel.prices.values()
        ]
        complete = sum(all(column) for column in zip(*priced, strict=True))
        return complete / len(inputs.dates)

    def _checks(self, prereg: Prereg, inputs: _Inputs) -> tuple[AdequacyCheck, ...]:
        adequacy = prereg.spec.adequacy
        allowed = set(adequacy.point_in_time)
        late = sorted(s.series_id for s in inputs.series if s.point_in_time not in allowed)
        coverage = self._coverage(inputs)
        periods = max(len(inputs.dates) - 1, 0)
        effective = effective_n(periods, 0.0) if periods else 0.0
        return (
            AdequacyCheck(
                "data loaded",
                not inputs.missing,
                "every series is in the research store"
                if not inputs.missing
                else f"never loaded: {', '.join(inputs.missing)}",
            ),
            AdequacyCheck(
                "point in time",
                not late,
                f"every series is {', '.join(sorted(p.value for p in allowed))}"
                if not late
                else f"not point in time as pre-registered: {', '.join(late)}",
            ),
            AdequacyCheck(
                "coverage",
                coverage is not None and coverage >= float(adequacy.min_coverage),
                f"{0 if coverage is None else coverage:.1%} of {len(inputs.dates)} rebalance "
                f"dates fully priced (pre-registered minimum {adequacy.min_coverage})",
            ),
            AdequacyCheck(
                "effective observations",
                effective >= adequacy.min_effective_observations,
                f"{effective:g} non-overlapping months (pre-registered minimum "
                f"{adequacy.min_effective_observations})",
            ),
        )

    def _built(self, prereg: Prereg) -> tuple[list[SimInstrument], dict[str, Strategy]]:
        study = prereg.spec.study
        if prereg.sha256 not in self._strategies:
            self._strategies[prereg.sha256] = (
                _instruments(study.instruments),
                {s.name: build_strategy(s, study, self._settings) for s in study.portfolios},
            )
        return self._strategies[prereg.sha256]

    def _simulate(
        self,
        prereg: Prereg,
        panel: Panel,
        dates: Sequence[date],
        multiplier: Decimal,
        names: Sequence[str],
        *,
        taxed: bool = True,
    ) -> dict[str, SimulationResult]:
        """Every named portfolio on one panel, through one simulator."""
        instruments, strategies = self._built(prereg)
        simulator = Simulator(
            instruments,
            panel,
            self._costs,
            self._policy,
            self._simulation(prereg, multiplier, taxed),
        )
        return {name: simulator.run(strategies[name], dates) for name in names}

    def paths(
        self,
        prereg: Prereg,
        months: _Months,
        dates: Sequence[date],
        names: Sequence[str],
        paths: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Growth, worst drawdown, terminal wealth multiple and sales not made of every
        portfolio (columns) on each resampled path (rows)."""
        shape = (len(paths), len(names))
        growth, drawdown, wealth = np.empty(shape), np.empty(shape), np.empty(shape)
        unsold = np.zeros(shape, dtype=int)
        for row, path in enumerate(paths):
            panel = _synthetic(months, dates, path)
            runs = self._simulate(prereg, panel, dates, Decimal(1), names)
            for column, name in enumerate(names):
                growth[row, column] = _growth(runs[name])
                drawdown[row, column] = runs[name].max_drawdown
                wealth[row, column] = _wealth(runs[name])
                unsold[row, column] = runs[name].unsold
        return growth, drawdown, wealth, unsold

    def _bootstrap(self, prereg: Prereg, inputs: _Inputs, names: Sequence[str]) -> _Draws:
        spec = prereg.spec.bootstrap
        research = self._settings.research
        months = _months(
            inputs.panel,
            inputs.dates,
            research.simulation.max_price_age_days,
            research.data.max_fx_age_days,
        )
        paths = block_bootstrap_indices(
            len(inputs.dates) - 1, block=spec.block_months, n_boot=spec.n_boot, seed=spec.seed
        )
        workers = min(research.workers, len(paths))
        if workers <= 1:
            return _Draws(tuple(names), *self.paths(prereg, months, inputs.dates, names, paths))
        # The paths are drawn above from the seed; each worker runs a contiguous share and the
        # shares are put back in order, so the draws do not depend on the number of workers.
        chunks = np.array_split(paths, workers * CHUNKS_PER_WORKER)
        tasks = [
            PathTask(self._settings, self._profile, prereg, months, inputs.dates, tuple(names), c)
            for c in chunks
            if len(c)
        ]
        with ProcessPoolExecutor(max_workers=workers) as pool:
            parts = list(pool.map(run_paths, tasks))
        return _Draws(
            tuple(names), *(np.concatenate(column) for column in zip(*parts, strict=True))
        )

    @staticmethod
    def _stats(
        known: StrategyStats,
        historical: SimulationResult,
        pre_tax: SimulationResult,
        draws: _Draws,
        prereg: Prereg,
    ) -> StrategyStats:
        column = draws.column(known.name)
        drawdowns = draws.drawdown[:, column].tolist()
        return StrategyStats(
            name=known.name,
            simplicity=known.simplicity,
            historical=historical,
            pre_tax=pre_tax,
            median_log_growth=float(np.median(draws.growth[:, column])),
            terminal_wealth_p5=float(np.percentile(draws.wealth[:, column], TERMINAL_PERCENTILE)),
            breach=tuple(
                (limit, breach_probability(drawdowns, float(limit)))
                for limit in prereg.spec.drawdown_limits
            ),
            static_weights=known.static_weights,
            unsold_paths=int((draws.unsold[:, column] > 0).sum()),
        )

    def _robustness(
        self,
        prereg: Prereg,
        inputs: _Inputs,
        candidate: str,
        reference: str,
        estimate: float | None,
        simulate: _Simulate,
    ) -> Robustness:
        spec = prereg.spec
        pair = [candidate, reference]
        subperiods: list[SubperiodEstimate] = []
        for first, last in spec.subperiods:
            dates = [d for d in inputs.dates if first <= d <= last]
            if len(dates) < len(pair) or estimate is None:
                subperiods.append(SubperiodEstimate(first, last, None, None))
                continue
            runs = simulate(inputs.panel, dates, Decimal(1), pair)
            part = _growth_difference(runs[candidate], runs[reference])
            subperiods.append(SubperiodEstimate(first, last, part, (part > 0) == (estimate > 0)))
        search = self._settings.research.breakeven

        def effect_at(multiplier: float) -> float:
            try:
                runs = simulate(inputs.panel, inputs.dates, Decimal(repr(multiplier)), pair)
                return _growth_difference(runs[candidate], runs[reference])
            except SimulationError:
                # Costs so high that a strategy cannot trade or ends with nothing: no effect.
                return -math.inf

        breakeven = breakeven_multiplier(
            effect_at,
            theta=float(spec.theta),
            upper=float(search.max_multiplier),
            tolerance=float(search.multiplier_tolerance),
        )
        exceeds = breakeven.status == "above_search" or (
            breakeven.multiplier is not None
            and breakeven.multiplier > float(spec.cost_uncertainty_multiplier)
        )
        return Robustness(
            subperiods=tuple(subperiods),
            breakeven=breakeven,
            cost_uncertainty=spec.cost_uncertainty_multiplier,
            breakeven_exceeds_uncertainty=exceeds,
            placebo=NO_PLACEBO,
        )

    @staticmethod
    def _excess(candidate: SimulationResult, reference: SimulationResult) -> list[float]:
        return [
            c - r
            for c, r in zip(candidate.period_log_returns, reference.period_log_returns, strict=True)
        ]

    def _deflated(
        self, candidate: SimulationResult, reference: SimulationResult, pool: Sequence[float]
    ) -> DeflatedSharpe | None:
        """The candidate's monthly Sharpe of excess log returns against the best of every trial
        the registry holds for the study and every candidate of this run."""
        excess = self._excess(candidate, reference)
        sharpe = sharpe_ratio(excess)
        shape = moments(excess)
        if sharpe is None or shape is None:
            return None
        every = list(pool) if pool else [sharpe]
        variance = float(np.var(every, ddof=1)) if len(every) > 1 else 0.0
        return deflated_sharpe(
            sharpe,
            observations=len(excess),
            skew=shape[0],
            kurtosis=shape[1],
            trials=len(every),
            sharpe_variance=variance,
        )

    @staticmethod
    def _frontier(
        prereg: Prereg,
        stats: Mapping[str, StrategyStats],
        draws: _Draws,
        *,
        alpha: float,
        tests: int,
    ) -> tuple[FrontierPoint, ...]:
        grid = prereg.spec.frontier
        if grid is None:
            return ()
        points: list[FrontierPoint] = []

        def order(name: str) -> tuple[int, float, str]:
            turnover = stats[name].turnover
            return stats[name].simplicity, math.inf if turnover is None else turnover, name

        for limit in prereg.spec.drawdown_limits:
            for probability in grid.breach_probabilities:
                eligible = [
                    name
                    for name in draws.names
                    if dict(stats[name].breach)[limit] <= float(probability)
                ]
                if not eligible:
                    points.append(FrontierPoint(limit, probability, (), None, (), None))
                    continue
                best = max(eligible, key=lambda n: (stats[n].median_log_growth or -math.inf, n))
                column = draws.column(best)
                apart = []
                for name in eligible:
                    if name == best:
                        continue
                    gap = draws.growth[:, column] - draws.growth[:, draws.column(name)]
                    low, high = percentile_interval(gap.tolist(), alpha=alpha, tests=tests)
                    if low <= 0 <= high:
                        apart.append(name)
                chosen = min([best, *apart], key=order)
                points.append(
                    FrontierPoint(
                        limit, probability, tuple(eligible), best, tuple(sorted(apart)), chosen
                    )
                )
        return tuple(points)

    # ── the structure check ──

    def _structure(
        self,
        prereg: Prereg,
        spec: StructureSpec,
        rule: TrendRule,
        store: Store,
        *,
        alpha: float,
    ) -> StructureResult:
        """Read the structure check's series as of its end and run the tilt on them
        (descriptive)."""
        ids = [*spec.market_series, spec.cash_series]
        infos = {series_id: research_series_info(store, series_id) for series_id in ids}
        missing = sorted(series_id for series_id, info in infos.items() if info is None)
        allowed = set(spec.point_in_time)
        late = sorted(
            series_id
            for series_id, info in infos.items()
            if info is not None and info.point_in_time not in allowed
        )
        columns = {
            series_id: {
                row.observed_on: row.value
                for row in research_observations(store, series_id, as_of=spec.end)
                if spec.start <= row.observed_on <= spec.end
            }
            for series_id in ids
            if series_id not in missing
        }
        months = (
            sorted(set.intersection(*(set(column) for column in columns.values())))
            if columns
            else []
        )
        checks = (
            AdequacyCheck(
                "data loaded",
                not missing,
                "every series is in the research store"
                if not missing
                else f"never loaded: {', '.join(missing)}",
            ),
            AdequacyCheck(
                "point in time",
                not late,
                f"every series is {', '.join(sorted(p.value for p in allowed))}"
                if not late
                else f"not point in time as pre-registered: {', '.join(late)}",
            ),
            AdequacyCheck(
                "history",
                len(months) > rule.months_needed,
                f"{len(months)} common months (the tilt needs {rule.months_needed} first)",
            ),
        )
        market = [sum(columns[s][m] for s in spec.market_series) for m in months]
        cash = [columns[spec.cash_series][m] for m in months] if months else []
        return structure_check(
            spec,
            months,
            market,
            cash,
            checks,
            rule=rule,
            bootstrap=prereg.spec.bootstrap,
            limits=prereg.spec.drawdown_limits,
            alpha=alpha,
            days_per_year=float(self._settings.research.simulation.days_per_year),
        )


@dataclass(frozen=True, slots=True)
class PathTask:
    """A share of the bootstrap paths, with everything a worker process needs to run them."""

    settings: ObliskConfig
    profile: JurisdictionProfile
    prereg: Prereg
    months: _Months
    dates: tuple[date, ...]
    names: tuple[str, ...]
    paths: np.ndarray


def run_paths(task: PathTask) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Run a share of the paths in a worker process (the same code as in process)."""
    runner = StudyRunner(task.settings, task.profile)
    return runner.paths(task.prereg, task.months, task.dates, task.names, task.paths)
