"""Running a pre-registered study: the data it reads, the adequacy checks, the historical run, the
bootstrap, the robustness checks and the decision (MASTER_PLAN §5.9; DECISIONS D60, D62, D63).

The one study kind this build runs is a **portfolio comparison**: a candidate strategy against a
reference over the same instruments, each simulated with costs and tax as they arise and
liquidated at the horizon. The primary effect is the difference in the annualised log growth of
liquidation value, after costs and tax. Its interval comes from a paired moving-block bootstrap
of whole months — every instrument's return, its income yield and every reference rate resampled
together — with both strategies simulated on each path, so each path's tax is paid as that path
would have paid it.

Everything is read as of the horizon and nothing later: the store bounds every read by
publication date, and the strategies see only what was public on each rebalance date.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Final

import numpy as np

from ..config.schema import ObliskConfig
from ..domain.research import Observation, ResearchSeries, Trial
from ..domain.tax import SecurityIdentity
from ..errors import SimulationError
from ..jurisdiction.tax import TaxPolicy
from ..quant.costs import CostBook
from ..store import Store
from ..store.research import research_observations, research_series_info
from .data import SurvivorshipReport, survivorship
from .decide import AdequacyCheck, Decision, decide
from .prereg import InstrumentSpec, Prereg
from .registry import PreregLock, TrialRegistry, inputs_sha256, sharpes
from .simulate import EURO, Panel, SimInstrument, SimulationResult, SimulationSettings, Simulator
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
from .strategies import build_strategy

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
    "Bootstrap paths resample whole months of every instrument and rate together; on a path, "
    "income is paid at month end at the resampled month's yield.",
)
#: What this study kind has no placebo for.
NO_PLACEBO: Final = (
    "not applicable: a portfolio comparison has no signal to lag; its robustness is the sign in "
    "each subperiod and the break-even cost multiplier"
)
#: The terminal-wealth percentile reported beside the median (D60).
TERMINAL_PERCENTILE: Final = 5


@dataclass(frozen=True, slots=True)
class StrategyStats:
    """One strategy: its historical run and its distribution over the bootstrap paths."""

    name: str
    historical: SimulationResult | None
    median_log_growth: float | None
    terminal_wealth_p5: float | None
    breach: tuple[tuple[Decimal, float], ...]


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
    estimate: float | None
    se: float | None
    detectable: float | None
    candidate: StrategyStats
    reference: StrategyStats
    robustness: Robustness | None
    deflated: DeflatedSharpe | None
    trials: int
    decision: Decision
    tax_rules: tuple[str, ...]
    cost_labels: tuple[str, ...]


# ── reading the data ──


@dataclass(frozen=True, slots=True)
class _Inputs:
    series: tuple[ResearchSeries, ...]
    missing: tuple[str, ...]
    panel: Panel
    dates: tuple[date, ...]


def _series_ids(prereg: Prereg, base: str) -> tuple[list[str], list[str]]:
    study = prereg.spec.study
    ids = [i.price_series for i in study.instruments]
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


def gather(store: Store, prereg: Prereg, base: str) -> _Inputs:
    """Read every series the study names, as of its horizon."""
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

    prices = {i.key: rows(i.price_series) for i in study.instruments}
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
    out: list[float | None] = []
    for day in dates:
        visible = [r for r in rows if r.observed_on <= day and r.available_on <= day]
        latest = max(visible, key=lambda r: (r.observed_on, r.available_on), default=None)
        fresh = latest is not None and day - latest.observed_on <= timedelta(days=max_age)
        out.append(latest.value if fresh and latest is not None else None)
    return out


# ── the bootstrap ──


@dataclass(frozen=True, slots=True)
class _Months:
    """Per month: each instrument's log return and income yield, each rate's log change."""

    first_prices: dict[str, float]
    first_rates: dict[str, float]
    returns: dict[str, np.ndarray]
    yields: dict[str, np.ndarray]
    rate_changes: dict[str, np.ndarray]


def _months(panel: Panel, dates: Sequence[date], max_price_age: int, max_fx_age: int) -> _Months:
    def levels(rows: Sequence[Observation], age: int, what: str) -> np.ndarray:
        values = _boundary(rows, dates, age)
        if any(v is None or v <= 0 for v in values):
            raise SimulationError(f"{what} lacks a value at a rebalance date")
        return np.array(values, dtype=float)

    returns, yields, first = {}, {}, {}
    for key, rows in panel.prices.items():
        prices = levels(rows, max_price_age, key)
        first[key] = float(prices[0])
        returns[key] = np.diff(np.log(prices))
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
    return _Months(first, first_rates, returns, yields, rate_changes)


def _synthetic(months: _Months, dates: Sequence[date], path: np.ndarray) -> Panel:
    """A panel on the rebalance dates whose months are ``path``'s months of history."""

    def series(first: float, changes: np.ndarray) -> tuple[np.ndarray, tuple[Observation, ...]]:
        levels = first * np.exp(np.concatenate([[0.0], np.cumsum(changes[path])]))
        return levels, tuple(
            Observation(observed_on=d, available_on=d, value=float(v))
            for d, v in zip(dates, levels, strict=True)
        )

    prices: dict[str, tuple[Observation, ...]] = {}
    income: dict[str, tuple[Observation, ...]] = {}
    for key, first in months.first_prices.items():
        levels, prices[key] = series(first, months.returns[key])
        paid = months.yields[key][path] * levels[:-1]
        income[key] = tuple(
            Observation(observed_on=d, available_on=d, value=float(v))
            for d, v in zip(dates[1:], paid, strict=True)
            if v > 0
        )
    fx = {c: series(first, months.rate_changes[c])[1] for c, first in months.first_rates.items()}
    return Panel(prices, income, fx)


# ── the run ──


@dataclass(frozen=True, slots=True)
class _Draw:
    """One bootstrap path: each strategy's growth, worst drawdown and terminal wealth multiple."""

    candidate: float
    reference: float
    candidate_drawdown: float
    reference_drawdown: float
    candidate_wealth: float
    reference_wealth: float


type _Simulate = Callable[
    [Panel, Sequence[date], Decimal], tuple[SimulationResult, SimulationResult]
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


def _not_run(name: str) -> StrategyStats:
    return StrategyStats(name, None, None, None, ())


class StudyRunner:
    """Runs portfolio comparisons with one configuration, one tax policy and one cost book."""

    def __init__(self, settings: ObliskConfig, policy: TaxPolicy, costs: CostBook) -> None:
        self._settings = settings
        self._policy = policy
        self._costs = costs

    def _simulation(self, prereg: Prereg, multiplier: Decimal) -> SimulationSettings:
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
        decision_defaults = self._settings.research.decision
        alpha = float(spec.alpha if spec.alpha is not None else decision_defaults.alpha)
        alpha_source = "pre-registration" if spec.alpha is not None else "config research.decision"
        target = float(
            spec.target_power if spec.target_power is not None else decision_defaults.target_power
        )
        target_source = (
            "pre-registration" if spec.target_power is not None else "config research.decision"
        )
        base = self._settings.account.base_currency
        inputs = gather(store, prereg, base)
        checks = self._checks(prereg, inputs)
        instruments = _instruments(spec.study.instruments)
        candidate_strategy = build_strategy(
            spec.study.candidate.name, spec.study.candidate.model_dump()
        )
        reference_strategy = build_strategy(
            spec.study.reference.name, spec.study.reference.model_dump()
        )

        def simulate(
            panel: Panel, dates: Sequence[date], multiplier: Decimal
        ) -> tuple[SimulationResult, SimulationResult]:
            simulator = Simulator(
                instruments, panel, self._costs, self._policy, self._simulation(prereg, multiplier)
            )
            return (
                simulator.run(candidate_strategy, dates),
                simulator.run(reference_strategy, dates),
            )

        survivors = survivorship(
            store,
            [i.price_series for i in spec.study.instruments],
            start=spec.study.start,
            end=spec.study.end,
            as_of=spec.study.end,
        )
        hashes = {s.series_id: s.content_sha256 for s in inputs.series}
        mechanics = self._simulation(prereg, Decimal(1))
        parameters = {
            "alpha": alpha,
            "target_power": target,
            "bootstrap": spec.bootstrap.model_dump(),
            "no_trade_band": str(mechanics.no_trade_band),
            "whole_units": mechanics.whole_units,
            "max_price_age_days": mechanics.max_price_age_days,
            "max_fx_age_days": mechanics.max_fx_age_days,
            "policy": self._policy.policy_id,
        }
        digest = inputs_sha256(spec_sha256=prereg.sha256, series=hashes, parameters=parameters)
        coverage = self._coverage(inputs)
        candidate_stats = _not_run(spec.study.candidate.name)
        reference_stats = _not_run(spec.study.reference.name)
        estimate = se = detectable = None
        robustness: Robustness | None = None
        interval: tuple[float, float] | None = None
        study_power: float | None = None
        deflated: DeflatedSharpe | None = None
        cost_labels: frozenset[str] = frozenset()
        trials = registry.trials(spec.id)
        if all(check.passed for check in checks):
            try:
                cand, ref = simulate(inputs.panel, inputs.dates, Decimal(1))
                estimate = _growth_difference(cand, ref)
                cost_labels = cand.cost_labels | ref.cost_labels
                draws = self._bootstrap(prereg, inputs, simulate)
                robustness = self._robustness(prereg, inputs, estimate, simulate)
            except SimulationError as exc:
                checks = (*checks, AdequacyCheck("simulation", False, str(exc)))
                estimate = None
            else:
                differences = np.array([d.candidate - d.reference for d in draws])
                tests = spec.tests_in_confirmation
                interval = percentile_interval(differences.tolist(), alpha=alpha, tests=tests)
                se = float(differences.std(ddof=1)) if differences.size > 1 else None
                if se is not None and se > 0:
                    study_power = power(float(spec.theta), se, alpha=alpha, tests=tests)
                    detectable = detectable_effect(
                        se, alpha=alpha, tests=tests, target_power=target
                    )
                candidate_stats = self._stats(
                    spec.study.candidate.name,
                    cand,
                    [(d.candidate, d.candidate_drawdown, d.candidate_wealth) for d in draws],
                    prereg,
                )
                reference_stats = self._stats(
                    spec.study.reference.name,
                    ref,
                    [(d.reference, d.reference_drawdown, d.reference_wealth) for d in draws],
                    prereg,
                )
                deflated = self._deflated(cand, ref, trials)
        decision = decide(
            interval=interval,
            theta=float(spec.theta),
            power=study_power,
            target_power=target,
            checks=checks,
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
            estimate=estimate,
            se=se,
            detectable=detectable,
            candidate=candidate_stats,
            reference=reference_stats,
            robustness=robustness,
            deflated=deflated,
            trials=len(trials) + 1,
            decision=decision,
            tax_rules=self._policy.describe(),
            cost_labels=tuple(sorted(cost_labels)),
        )

    # ── the steps ──

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

    def _bootstrap(self, prereg: Prereg, inputs: _Inputs, simulate: _Simulate) -> list[_Draw]:
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
        draws: list[_Draw] = []
        for path in paths:
            candidate, reference = simulate(
                _synthetic(months, inputs.dates, path), inputs.dates, Decimal(1)
            )
            draws.append(
                _Draw(
                    _growth(candidate),
                    _growth(reference),
                    candidate.max_drawdown,
                    reference.max_drawdown,
                    _wealth(candidate),
                    _wealth(reference),
                )
            )
        return draws

    @staticmethod
    def _stats(
        name: str,
        historical: SimulationResult,
        draws: Sequence[tuple[float, float, float]],
        prereg: Prereg,
    ) -> StrategyStats:
        growth = np.array([d[0] for d in draws])
        drawdowns = [d[1] for d in draws]
        wealth = np.array([d[2] for d in draws])
        return StrategyStats(
            name=name,
            historical=historical,
            median_log_growth=float(np.median(growth)),
            terminal_wealth_p5=float(np.percentile(wealth, TERMINAL_PERCENTILE)),
            breach=tuple(
                (limit, breach_probability(drawdowns, float(limit)))
                for limit in prereg.spec.drawdown_limits
            ),
        )

    def _robustness(
        self, prereg: Prereg, inputs: _Inputs, estimate: float, simulate: _Simulate
    ) -> Robustness:
        spec = prereg.spec
        subperiods: list[SubperiodEstimate] = []
        for first, last in spec.subperiods:
            dates = [d for d in inputs.dates if first <= d <= last]
            if len(dates) < 2:
                subperiods.append(SubperiodEstimate(first, last, None, None))
                continue
            part = _growth_difference(*simulate(inputs.panel, dates, Decimal(1)))
            subperiods.append(SubperiodEstimate(first, last, part, (part > 0) == (estimate > 0)))
        search = self._settings.research.breakeven

        def effect_at(multiplier: float) -> float:
            try:
                return _growth_difference(
                    *simulate(inputs.panel, inputs.dates, Decimal(repr(multiplier)))
                )
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
    def _deflated(
        candidate: SimulationResult, reference: SimulationResult, trials: Sequence[Trial]
    ) -> DeflatedSharpe | None:
        excess = [
            c - r
            for c, r in zip(candidate.period_log_returns, reference.period_log_returns, strict=True)
        ]
        sharpe = sharpe_ratio(excess)
        shape = moments(excess)
        if sharpe is None or shape is None:
            return None
        every = [*sharpes(trials), sharpe]
        variance = float(np.var(every, ddof=1)) if len(every) > 1 else 0.0
        return deflated_sharpe(
            sharpe,
            observations=len(excess),
            skew=shape[0],
            kurtosis=shape[1],
            trials=len(every),
            sharpe_variance=variance,
        )
