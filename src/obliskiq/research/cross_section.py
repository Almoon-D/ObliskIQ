"""Running a pre-registered cross-section: a selection signal tested as the Fama-MacBeth series
of a top group's monthly excess return after costs and tax (MASTER_PLAN §5.5, §5.9; DECISIONS
D62, D63).

Every member of the universe is held through one kind of vehicle. On each month end the
**members** are those whose spans cover the date and whose series has a fresh price; a member
the vendor does not serve counts as lost to survivorship, measured in every year of the sample.
Two rules build portfolios from them:

- **universe**: every member, equally weighted;
- **top group**: the members ranked by momentum (the return from ``lookback`` to ``skip`` month
  ends back, lagged ``lag`` months for a placebo past the horizon); a name enters within the top
  ``enter_fraction`` and is kept while within the top ``keep_fraction``, and the group holds
  ``enter_fraction`` of the ranked members. While no member can be ranked the top group holds
  the universe.

Both trade only what changes: a leaver is sold, an entrant buys an equal share of what the
continuing holdings leave, and every holding is reset to equal weights at the last rebalance
date of the pre-registered month. A portfolio may carry the trailing volatility stop of
``research.simulate`` (H5): a stopped name waits in cash until it is selected again.

Each portfolio runs through the simulator with costs on the listing venue and tax as it arises,
and is liquidated at the horizon. Its **monthly series** is the log change of the account's
value from one month end to the next, from the initial capital to the liquidation value. A
test's primary effect is the mean of the candidate's series minus the other's, times the
periods in a year; its interval comes from a moving-block bootstrap of that series at level
``1 - 2 alpha / m`` (Bonferroni over the confirmation run's tests), with the Newey-West interval
beside it. Robustness: the sign in each subperiod, the break-even cost multiplier (both
portfolios re-run at scaled costs) and a placebo that must not show the effect. Exploration
runs on the development sample; the confirmation run on the later confirmation sample, read as
of its end and nothing later.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from typing import Final

import numpy as np

from ..config.schema import ObliskConfig
from ..domain.research import Observation, ResearchSeries
from ..domain.tax import SecurityIdentity
from ..engines.core.trades import share
from ..errors import SimulationError
from ..jurisdiction.profile import JurisdictionProfile
from ..jurisdiction.tax_policies import TAX_POLICIES
from ..quant.costs import CostBook
from ..quant.inference import MeanEstimate
from ..store import Store
from ..store.research import research_observations, research_series_info
from .data import SurvivorshipReport, survivorship
from .decide import AdequacyCheck, Decision, decide
from .prereg import (
    CrossSectionPrereg,
    CrossSectionSpec,
    CrossSectionTestSpec,
    MemberSpec,
    PortfolioSpec,
    SampleSpec,
    SelectionSpec,
    SignalSpec,
)
from .registry import PreregLock, TrialRegistry, canonical_sha256, inputs_sha256, sharpes
from .simulate import (
    EURO,
    Book,
    MarketView,
    Panel,
    SimInstrument,
    SimulationResult,
    SimulationSettings,
    Simulator,
    StopRule,
)
from .stats import (
    Breakeven,
    DeflatedSharpe,
    bootstrap,
    breakeven_multiplier,
    deflated_sharpe,
    detectable_effect,
    moments,
    newey_west,
    percentile_interval,
    power,
    sharpe_ratio,
)
from .study import Robustness, SubperiodEstimate, less_costs, month_ends

type Weights = Mapping[str, Decimal]

ZERO = Decimal(0)
ONE = Decimal(1)
#: Months in a year, for counting the months of a sample before its data are read.
MONTHS_PER_YEAR: Final = 12
#: The fewest observations a Newey-West mean is computed from.
MIN_NEWEY_WEST_POINTS: Final = 3
#: What this study kind's simulation cannot show, stated in every report.
CROSS_SECTION_NOTES: Final = (
    "Every member is an index proxy priced as one kind of vehicle, its yearly cost deducted day "
    "by day; a member without such a vehicle at a date is held as if it had one.",
    "Trades fill at the rebalance date's close, moved by the cost model's spread and slippage.",
    "A protective stop is watched on daily closes and fills at the first close at or below it: "
    "a gap through the stop is borne in full, and a touch inside a session that recovers by the "
    "close is not seen. The cash from a stop waits for the next rebalance date.",
    "A year's tax is paid at the first rebalance date on or after its payment date; the profile's "
    "current rules and rate table are applied to every simulated year.",
    "Losses still carried forward at the horizon are worth nothing.",
    "The interval resamples the realised monthly series in blocks; the tax a resampled history "
    "would have paid is not re-simulated.",
)


def round_half_up(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


# ── the portfolios ──


@dataclass(frozen=True, slots=True)
class SelectionStrategy:
    """A portfolio of the cross-section (the module docstring): the universe or the top group,
    trading only what changes, reset to equal weights once a year."""

    name: str
    members: tuple[MemberSpec, ...]
    rule: str
    signal: SignalSpec
    selection: SelectionSpec
    lag_months: int

    def targets(self, view: MarketView, book: Book) -> Weights | None:
        eligible = [
            m.key for m in self.members if m.member_on(view.as_of) and view.close(m.key) is not None
        ]
        chosen = (
            self.top_group(view, book, eligible) if self.rule == "top_group" else list(eligible)
        )
        return self.weights(chosen, view.as_of, book)

    def momentum(self, view: MarketView, key: str) -> float | None:
        """The return from ``lookback`` to ``skip`` month ends back, ``lag`` months earlier;
        ``None`` while the member's history is shorter."""
        closes = view.month_end_closes(key)
        recent = 1 + self.lag_months + self.signal.skip_months
        past = 1 + self.lag_months + self.signal.lookback_months
        if len(closes) < past:
            return None
        return closes[-recent][1] / closes[-past][1] - 1

    def top_group(self, view: MarketView, book: Book, eligible: Sequence[str]) -> list[str]:
        scores = {k: s for k in eligible if (s := self.momentum(view, k)) is not None}
        if not scores:
            return list(eligible)
        ranked = sorted(scores, key=lambda key: (-scores[key], key))
        count = Decimal(len(ranked))
        size = max(1, round_half_up(self.selection.enter_fraction * count))
        keep = max(size, round_half_up(self.selection.keep_fraction * count))
        held = {key for key, units in book.units.items() if units > ZERO}
        kept = [key for key in ranked[:keep] if key in held][:size]
        fill = [key for key in ranked if key not in kept][: size - len(kept)]
        return kept + fill

    def weights(self, chosen: Sequence[str], on: date, book: Book) -> Weights:
        """Equal weights at the reset or from nothing held; otherwise the continuing holdings
        keep theirs and the entrants share what is left equally."""
        if not chosen:
            return {}
        held = {key for key, units in book.units.items() if units > ZERO}
        if on.month == self.selection.reset_month or not held & set(chosen):
            each = share(ONE, Decimal(len(chosen)))
            return dict.fromkeys(chosen, each)
        continuing = {key: book.weight(key) for key in chosen if key in held}
        total = sum(continuing.values(), ZERO)
        if total > ONE:
            continuing = {key: share(weight, total) for key, weight in continuing.items()}
            total = sum(continuing.values(), ZERO)
        entrants = [key for key in chosen if key not in held]
        if not entrants:
            return continuing
        each = share(ONE - total, Decimal(len(entrants)))
        return {**continuing, **dict.fromkeys(entrants, each)}


def build_portfolio(name: str, spec: PortfolioSpec, study: CrossSectionSpec) -> SelectionStrategy:
    return SelectionStrategy(
        name=name,
        members=tuple(study.study.served()),
        rule=spec.rule,
        signal=study.study.signal,
        selection=study.study.selection,
        lag_months=spec.lag_months,
    )


# ── results ──


@dataclass(frozen=True, slots=True)
class PortfolioRun:
    """One portfolio's runs after and before tax, and its monthly series after tax."""

    name: str
    after_tax: SimulationResult
    before_tax: SimulationResult
    returns: tuple[float, ...]

    @property
    def turnover(self) -> float:
        """Value traded a year over the mean mark-to-market value, after tax (a run has at
        least two dates, so a mark and a positive span)."""
        run = self.after_tax
        return float(run.traded) / float(np.mean(run.nav)) / run.years


@dataclass(frozen=True, slots=True)
class SelectionTest:
    """One test: its effect, interval, power, robustness and decision."""

    spec: CrossSectionTestSpec
    estimate: float | None
    pre_tax_estimate: float | None
    se: float | None
    newey_west: MeanEstimate | None
    detectable: float | None
    declared_power: float
    declared_detectable: float
    robustness: Robustness | None
    deflated: DeflatedSharpe | None
    decision: Decision

    @property
    def name(self) -> str:
        return self.spec.name


@dataclass(frozen=True, slots=True)
class PlaceboResult:
    """The placebo's effect and interval at the confirmation level; it fails as it should while
    the interval's lower end stays below its test's theta."""

    candidate: str
    against: str
    test: str
    estimate: float | None
    interval: tuple[float, float] | None
    theta: float

    @property
    def fails(self) -> bool | None:
        return None if self.interval is None else self.interval[0] < self.theta


@dataclass(frozen=True, slots=True)
class LostYear:
    """One year of the sample: the most members on a rebalance date, the members that were not
    served on any of its dates, and the largest share of members lost on one date."""

    year: int
    members: int
    lost: tuple[str, ...]
    share: float


@dataclass(frozen=True, slots=True)
class CrossSectionResult:
    """Everything a report states about one run."""

    prereg: CrossSectionPrereg
    explore: bool
    lock: PreregLock | None
    run_at: datetime
    alpha: float
    alpha_source: str
    target_power: float
    target_power_source: str
    sample: SampleSpec
    series: tuple[ResearchSeries, ...]
    inputs_sha256: str
    dates: tuple[date, ...]
    coverage: float | None
    lost_by_year: tuple[LostYear, ...]
    survivorship: SurvivorshipReport
    checks: tuple[AdequacyCheck, ...]
    portfolios: tuple[PortfolioRun, ...]
    tests: tuple[SelectionTest, ...]
    placebo: PlaceboResult | None
    trials: int
    tax_rules: tuple[str, ...]
    cost_labels: tuple[str, ...]

    def portfolio(self, name: str) -> PortfolioRun | None:
        return next((p for p in self.portfolios if p.name == name), None)


# ── reading the data ──


@dataclass(frozen=True, slots=True)
class _Inputs:
    series: tuple[ResearchSeries, ...]
    missing: tuple[str, ...]
    panel: Panel
    dates: tuple[date, ...]


def sample_months(sample: SampleSpec) -> int:
    """The months between a sample's first and last month ends, before any data are read."""
    return (sample.end.year - sample.start.year) * MONTHS_PER_YEAR + (
        sample.end.month - sample.start.month
    )


def declared_se(spec: CrossSectionSpec, test: CrossSectionTestSpec) -> float:
    """The standard error of the annualised effect before any data: the declared standard
    deviation of one month over the months of the confirmation sample, scaled to a year."""
    months = sample_months(spec.study.confirmation)
    return float(test.sd_per_period) * spec.periods_per_year / math.sqrt(months)


def gather(
    store: Store, prereg: CrossSectionPrereg, sample: SampleSpec, base: str, days_per_year: float
) -> _Inputs:
    """Every served member's series as of the sample's end, less the vehicle's yearly cost, and
    the reference rates the vehicle's currency needs."""
    study = prereg.spec.study
    ids = [m.price_series for m in study.served() if m.price_series is not None]
    currencies = sorted({study.vehicle.currency, base} - {EURO})
    fx_ids = [f"{study.fx_dataset}.{c}" for c in currencies] if study.fx_dataset else []
    found: list[ResearchSeries] = []
    missing: list[str] = []
    for series_id in [*ids, *fx_ids]:
        info = research_series_info(store, series_id)
        if info is None:
            missing.append(series_id)
        else:
            found.append(info)

    def rows(series_id: str) -> tuple[Observation, ...]:
        if series_id in missing:
            return ()
        return tuple(research_observations(store, series_id, as_of=sample.end))

    prices = {
        m.key: less_costs(rows(m.price_series), study.vehicle.expense_ratio, days_per_year)
        for m in study.served()
        if m.price_series is not None
    }
    fx = {series_id.rsplit(".", 1)[1]: rows(series_id) for series_id in fx_ids}
    dates = month_ends(prices[study.calendar], sample.start, sample.end)
    return _Inputs(tuple(found), tuple(missing), Panel(prices, {}, fx), dates)


def instruments(spec: CrossSectionSpec) -> list[SimInstrument]:
    vehicle = spec.study.vehicle
    return [
        SimInstrument(
            key=member.key,
            venue=vehicle.venue,
            asset_class=vehicle.asset_class,
            currency=vehicle.currency,
            security=SecurityIdentity(
                key=member.key,
                kind=vehicle.kind,
                isin=None,
                issuer=vehicle.issuer,
                tracked_index=member.name,
                listed=True,
            ),
            issuer_country=None,
            market_cap_usd=vehicle.market_cap_usd,
            income_country=None,
        )
        for member in spec.study.served()
    ]


def monthly_returns(run: SimulationResult) -> tuple[float, ...]:
    """The log change of the account's value month by month: from the initial capital to the
    second month end's mark, and to the liquidation value at the horizon."""
    if run.liquidation_value <= ZERO:
        raise SimulationError(f"{run.strategy} ended with nothing: its series is not defined")
    values = [float(run.initial), *run.nav[1:-1], float(run.liquidation_value)]
    return tuple(math.log(b / a) for a, b in pairwise(values))


def excess(candidate: PortfolioRun, against: PortfolioRun) -> tuple[float, ...]:
    return tuple(c - a for c, a in zip(candidate.returns, against.returns, strict=True))


# ── the runner ──


class CrossSectionRunner:
    """Runs cross-sections with one configuration and one jurisdiction profile."""

    def __init__(self, settings: ObliskConfig, profile: JurisdictionProfile) -> None:
        self._settings = settings
        self._policy = TAX_POLICIES.resolve(profile, settings)
        self._costs = CostBook.from_config(settings)
        self._rules_sha256 = {
            "costs": canonical_sha256(settings.costs.model_dump(mode="json")),
            "tax": canonical_sha256(
                None if profile.tax is None else profile.tax.model_dump(mode="json")
            ),
        }

    def _simulation(
        self, spec: CrossSectionSpec, multiplier: Decimal, taxed: bool
    ) -> SimulationSettings:
        mechanics = self._settings.research.simulation
        return SimulationSettings(
            base_currency=self._settings.account.base_currency,
            initial_capital=spec.study.initial_capital,
            no_trade_band=mechanics.no_trade_band_fraction,
            max_price_age_days=mechanics.max_price_age_days,
            max_fx_age_days=self._settings.research.data.max_fx_age_days,
            whole_units=mechanics.whole_units,
            days_per_year=float(mechanics.days_per_year),
            cost_multiplier=multiplier,
            taxed=taxed,
        )

    def simulate(
        self,
        spec: CrossSectionSpec,
        panel: Panel,
        dates: Sequence[date],
        names: Sequence[str],
        *,
        multiplier: Decimal = ONE,
        taxed: bool = True,
    ) -> dict[str, SimulationResult]:
        """Every named portfolio on one panel, through one simulator."""
        simulator = Simulator(
            instruments(spec), panel, self._costs, self._policy,
            self._simulation(spec, multiplier, taxed),
        )  # fmt: skip
        stop = spec.study.stop
        rule = (
            None
            if stop is None
            else StopRule(stop.volatility_sessions, stop.horizon_sessions, float(stop.multiple))
        )
        portfolios = spec.study.portfolios
        return {
            name: simulator.run(
                build_portfolio(name, portfolios[name], spec),
                dates,
                stop=rule if portfolios[name].stop else None,
            )
            for name in names
        }

    def run(
        self,
        prereg: CrossSectionPrereg,
        store: Store,
        registry: TrialRegistry,
        *,
        explore: bool,
        lock: PreregLock | None,
        now: datetime,
    ) -> CrossSectionResult:
        spec = prereg.spec
        study = spec.study
        defaults = self._settings.research.decision
        alpha = float(spec.alpha if spec.alpha is not None else defaults.alpha)
        alpha_source = "pre-registration" if spec.alpha is not None else "config research.decision"
        target = float(
            spec.target_power if spec.target_power is not None else defaults.target_power
        )
        target_source = (
            "pre-registration" if spec.target_power is not None else "config research.decision"
        )
        sample = study.development if explore else study.confirmation
        days_per_year = float(self._settings.research.simulation.days_per_year)
        base = self._settings.account.base_currency
        inputs = gather(store, prereg, sample, base, days_per_year)
        lost = self._lost_by_year(spec, inputs.dates)
        coverage = self._coverage(spec, inputs)
        checks = self._checks(spec, inputs, lost, coverage, explore=explore)
        mechanics = self._simulation(spec, ONE, True)
        parameters = {
            "alpha": alpha,
            "target_power": target,
            "sample": sample.model_dump(mode="json"),
            "bootstrap": spec.bootstrap.model_dump(),
            "no_trade_band": str(mechanics.no_trade_band),
            "whole_units": mechanics.whole_units,
            "max_price_age_days": mechanics.max_price_age_days,
            "max_fx_age_days": mechanics.max_fx_age_days,
            "days_per_year": mechanics.days_per_year,
            "base_currency": mechanics.base_currency,
            "policy": self._policy.policy_id,
            **self._rules_sha256,
        }
        digest = inputs_sha256(
            spec_sha256=prereg.sha256,
            series={s.series_id: s.content_sha256 for s in inputs.series},
            parameters=parameters,
        )
        tests_count = spec.tests_in_confirmation
        trials = registry.trials(spec.id)
        names = spec.compared()
        runs: dict[str, PortfolioRun] = {}
        pre_tax: dict[str, tuple[float, ...]] = {}
        cost_labels: frozenset[str] = frozenset()
        if all(check.passed for check in checks):
            try:
                after = self.simulate(spec, inputs.panel, inputs.dates, names)
                before = self.simulate(spec, inputs.panel, inputs.dates, names, taxed=False)
                runs = {
                    name: PortfolioRun(
                        name, after[name], before[name], monthly_returns(after[name])
                    )
                    for name in names
                }
                # The pre-tax series gives a descriptive estimate only.
                pre_tax = {name: monthly_returns(before[name]) for name in names}
            except SimulationError as exc:
                checks = (*checks, AdequacyCheck("simulation", False, str(exc)))
                runs, pre_tax = {}, {}
            else:
                cost_labels = frozenset().union(*(r.cost_labels for r in after.values()))
        pool = list(sharpes(trials))
        if runs:
            for test in spec.tests:
                sharpe = sharpe_ratio(excess(runs[test.candidate], runs[test.against]))
                if sharpe is not None:
                    pool.append(sharpe)
        tests = tuple(
            self._test(
                spec,
                test,
                runs,
                pre_tax,
                inputs,
                checks,
                pool,
                alpha=alpha,
                tests=tests_count,
                target=target,
            )
            for test in spec.tests
        )
        placebo = None
        if spec.placebo is not None:
            theta = float(next(t.theta for t in spec.tests if t.name == spec.placebo.test))
            estimate = interval = None
            if runs:
                series = excess(runs[spec.placebo.candidate], runs[spec.placebo.against])
                estimate, interval, _se = self._interval(
                    spec, series, alpha=alpha, tests=tests_count
                )
            placebo = PlaceboResult(
                spec.placebo.candidate,
                spec.placebo.against,
                spec.placebo.test,
                estimate,
                interval,
                theta,
            )
        return CrossSectionResult(
            prereg=prereg,
            explore=explore,
            lock=lock,
            run_at=now,
            alpha=alpha,
            alpha_source=alpha_source,
            target_power=target,
            target_power_source=target_source,
            sample=sample,
            series=inputs.series,
            inputs_sha256=digest,
            dates=inputs.dates,
            coverage=coverage,
            lost_by_year=lost,
            survivorship=survivorship(
                store,
                [m.price_series for m in study.served() if m.price_series is not None],
                start=sample.start,
                end=sample.end,
                as_of=sample.end,
            ),
            checks=checks,
            portfolios=tuple(runs[name] for name in names) if runs else (),
            tests=tests,
            placebo=placebo,
            trials=len(trials) + len(spec.tests),
            tax_rules=self._policy.describe(),
            cost_labels=tuple(sorted(cost_labels)),
        )

    # ── the steps ──

    def _interval(
        self, spec: CrossSectionSpec, series: Sequence[float], *, alpha: float, tests: int
    ) -> tuple[float, tuple[float, float], float | None]:
        """The annualised mean of a monthly series, its block-bootstrap interval at the
        confirmation level and the bootstrap's standard deviation."""
        scale = spec.periods_per_year
        draws = bootstrap(
            series,
            lambda values: scale * float(values.mean()),
            block=spec.bootstrap.block_months,
            n_boot=spec.bootstrap.n_boot,
            seed=spec.bootstrap.seed,
        )
        se = float(draws.std(ddof=1)) if draws.size > 1 else None
        estimate = scale * float(np.mean(series))
        return estimate, percentile_interval(draws.tolist(), alpha=alpha, tests=tests), se

    def _test(
        self,
        spec: CrossSectionSpec,
        test: CrossSectionTestSpec,
        runs: Mapping[str, PortfolioRun],
        pre_tax: Mapping[str, tuple[float, ...]],
        inputs: _Inputs,
        checks: tuple[AdequacyCheck, ...],
        pool: Sequence[float],
        *,
        alpha: float,
        tests: int,
        target: float,
    ) -> SelectionTest:
        theta = float(test.theta)
        before = declared_se(spec, test)
        estimate = pre_tax_estimate = se = detectable = study_power = None
        interval: tuple[float, float] | None = None
        nw: MeanEstimate | None = None
        robustness: Robustness | None = None
        deflated: DeflatedSharpe | None = None
        if runs:
            series = excess(runs[test.candidate], runs[test.against])
            estimate, interval, se = self._interval(spec, series, alpha=alpha, tests=tests)
            pre_tax_estimate = spec.periods_per_year * float(
                np.mean(
                    [
                        c - a
                        for c, a in zip(pre_tax[test.candidate], pre_tax[test.against], strict=True)
                    ]
                )
            )
            nw = newey_west(
                series,
                lags=spec.newey_west_lags,
                alpha=alpha,
                tests=tests,
                min_points=MIN_NEWEY_WEST_POINTS,
            )
            if se is not None and se > 0:
                study_power = power(theta, se, alpha=alpha, tests=tests)
                detectable = detectable_effect(se, alpha=alpha, tests=tests, target_power=target)
            robustness = self._robustness(spec, test, inputs, series, estimate)
            deflated = self._deflated(series, pool)
        return SelectionTest(
            spec=test,
            estimate=estimate,
            pre_tax_estimate=pre_tax_estimate,
            se=se,
            newey_west=nw,
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

    def _robustness(
        self,
        spec: CrossSectionSpec,
        test: CrossSectionTestSpec,
        inputs: _Inputs,
        series: Sequence[float],
        estimate: float,
    ) -> Robustness:
        scale = spec.periods_per_year
        subperiods: list[SubperiodEstimate] = []
        # The k-th month of the series ends on the (k + 1)-th rebalance date.
        ends = inputs.dates[1:]
        for first, last in spec.subperiods:
            part = [value for day, value in zip(ends, series, strict=True) if first <= day <= last]
            if not part:
                subperiods.append(SubperiodEstimate(first, last, None, None))
                continue
            mean = scale * float(np.mean(part))
            subperiods.append(SubperiodEstimate(first, last, mean, (mean > 0) == (estimate > 0)))
        search = self._settings.research.breakeven
        pair = [test.candidate, test.against]

        def effect_at(multiplier: float) -> float:
            try:
                runs = self.simulate(
                    spec, inputs.panel, inputs.dates, pair, multiplier=Decimal(repr(multiplier))
                )
                candidate, against = (monthly_returns(runs[name]) for name in pair)
            except SimulationError:
                # Costs so high that a portfolio cannot trade or ends with nothing: no effect.
                return -math.inf
            return scale * float(np.mean([c - a for c, a in zip(candidate, against, strict=True)]))

        breakeven: Breakeven = breakeven_multiplier(
            effect_at,
            theta=float(test.theta),
            upper=float(search.max_multiplier),
            tolerance=float(search.multiplier_tolerance),
        )
        exceeds = breakeven.status == "above_search" or (
            breakeven.multiplier is not None
            and breakeven.multiplier > float(spec.cost_uncertainty_multiplier)
        )
        placebo = (
            "see the placebo below"
            if spec.placebo is not None and spec.placebo.test == test.name
            else "not registered for this test"
        )
        return Robustness(
            subperiods=tuple(subperiods),
            breakeven=breakeven,
            cost_uncertainty=spec.cost_uncertainty_multiplier,
            breakeven_exceeds_uncertainty=exceeds,
            placebo=placebo,
        )

    @staticmethod
    def _deflated(series: Sequence[float], pool: Sequence[float]) -> DeflatedSharpe | None:
        """The monthly Sharpe of the excess series against the best of every trial the
        registry holds for the study and every test of this run."""
        sharpe = sharpe_ratio(series)
        shape = moments(series)
        if sharpe is None or shape is None:
            return None
        every = list(pool) if pool else [sharpe]
        variance = float(np.var(every, ddof=1)) if len(every) > 1 else 0.0
        return deflated_sharpe(
            sharpe,
            observations=len(series),
            skew=shape[0],
            kurtosis=shape[1],
            trials=len(every),
            sharpe_variance=variance,
        )

    # ── adequacy ──

    @staticmethod
    def _lost_by_year(spec: CrossSectionSpec, dates: Sequence[date]) -> tuple[LostYear, ...]:
        years: dict[int, tuple[int, set[str], float]] = {}
        for day in dates:
            members = [m for m in spec.study.members if m.member_on(day)]
            lost = {m.key for m in members if m.price_series is None}
            fraction = len(lost) / len(members) if members else 0.0
            most, names, worst = years.get(day.year, (0, set(), 0.0))
            years[day.year] = (max(most, len(members)), names | lost, max(worst, fraction))
        return tuple(
            LostYear(year, most, tuple(sorted(names)), worst)
            for year, (most, names, worst) in sorted(years.items())
        )

    def _coverage(self, spec: CrossSectionSpec, inputs: _Inputs) -> float | None:
        """The share of (served member, rebalance date) pairs of the sample with a fresh
        price."""
        age = self._settings.research.simulation.max_price_age_days
        pairs = priced = 0
        for member in spec.study.served():
            days = sorted(row.observed_on for row in inputs.panel.prices.get(member.key, ()))
            for day in inputs.dates:
                if not member.member_on(day):
                    continue
                pairs += 1
                position = bisect_right(days, day)
                if position and (day - days[position - 1]).days <= age:
                    priced += 1
        return None if pairs == 0 else priced / pairs

    def _checks(
        self,
        spec: CrossSectionSpec,
        inputs: _Inputs,
        lost: Sequence[LostYear],
        coverage: float | None,
        *,
        explore: bool,
    ) -> tuple[AdequacyCheck, ...]:
        adequacy = spec.adequacy
        allowed = set(adequacy.point_in_time)
        late = sorted(s.series_id for s in inputs.series if s.point_in_time not in allowed)
        months = max(len(inputs.dates) - 1, 0)
        needed = adequacy.min_effective_observations
        if explore:
            needed = adequacy.min_development_observations
        worst = max((year.share for year in lost), default=0.0)
        over = [str(year.year) for year in lost if year.share > float(adequacy.max_lost_share)]
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
                "survivorship",
                not over,
                f"at most {worst:.1%} of the members on a rebalance date are not served "
                f"(pre-registered maximum {adequacy.max_lost_share})"
                + (f"; exceeded in {', '.join(over)}" if over else ""),
            ),
            AdequacyCheck(
                "coverage",
                coverage is not None and coverage >= float(adequacy.min_coverage),
                f"{0 if coverage is None else coverage:.1%} of served member-dates priced "
                f"(pre-registered minimum {adequacy.min_coverage})",
            ),
            AdequacyCheck(
                "effective observations",
                months >= needed,
                f"{months} non-overlapping months (pre-registered minimum {needed})",
            ),
        )
