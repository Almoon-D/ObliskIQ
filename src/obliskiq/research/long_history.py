"""The long-history check (DECISIONS D92 rule 4): the breach at the owner's point and the growth of
each total-risk strategy on the longest history its roles have.

A pre-registered history gives each role a series (a level, or returns summed period by period:
an excess return and a bill) and a cash series, daily or monthly, in its own currency. Each
total-risk strategy whose base it covers runs on it **before tax**, at a flat cost on each leg's
traded value: at every month end from ``start`` its rule sets the risky share (its estimator
reading the history's log returns from ``data_start`` through that day, its lengths converted to
the history's observations, or the declared matrix for a twin), the core's bands decide whether
to trade, and between month ends each role's value drifts with its returns. The account is marked
at every period of the history (daily closes for a daily history, month ends for a monthly one).

Reported per strategy: its annualised log growth and deepest drawdown on the history as it
happened, and, over moving-block bootstrap paths of whole months as long as the owner's horizon
(the history's own length if shorter), the share of paths reaching each drawdown limit within
each horizon. The real days before ``start`` are kept before every path, as the main study keeps
its own. The check is descriptive and never an outcome: before tax, in the history's currency,
with the biases the pre-registration states.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise

import numpy as np

from ..domain.research import Observation
from ..engines.core.mechanism import Bands
from ..errors import SimulationError
from ..quant.covariance import CovarianceSeries, portfolio_volatility, required
from .daily import DrawdownStats, breach_within, drawdown_stats
from .decide import AdequacyCheck
from .prereg import MONTHS_PER_YEAR, LongHistorySpec, SeriesSumSpec
from .stats import annualised_log_growth, block_bootstrap_indices, years_between
from .strategies import TotalRisk


@dataclass(frozen=True, slots=True)
class HistoryData:
    """A history's periods (each the day a period ends), each role's and the cash's simple
    return over each period (into that day), and the index of each month's last period."""

    periods: tuple[date, ...]
    roles: Mapping[str, np.ndarray]
    cash: np.ndarray
    month_ends: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class HistoryRun:
    """One strategy on one history: its growth and drawdowns as it happened, and its paths'."""

    name: str
    growth: float | None
    stats: DrawdownStats
    median_growth: float
    breach: tuple[tuple[Decimal, int | None, float], ...]


@dataclass(frozen=True, slots=True)
class HistoryResult:
    """The check on one history: whether its data passed, its span, and each strategy's run."""

    spec: LongHistorySpec
    checks: tuple[AdequacyCheck, ...]
    first: date | None
    last: date | None
    months: int
    path_months: int
    runs: tuple[HistoryRun, ...]
    skipped: tuple[str, ...]


def _key(day: date, frequency: str) -> tuple[int, int] | date:
    return (day.year, day.month) if frequency == "monthly" else day


def history_data(
    spec: LongHistorySpec,
    rows: Mapping[str, Sequence[Observation]],
    *,
    max_age_days: int,
) -> HistoryData:
    """Align the history's series on the periods of its first role's first series, from
    ``data_start`` to ``end``: a level is the latest value within ``max_age_days`` (its return
    the change between two periods), a return series must have its own value in the period."""
    first_role = next(iter(spec.roles.values()))
    anchor = [
        row.observed_on
        for row in rows[first_role.series[0]]
        if spec.data_start <= row.observed_on <= spec.end
    ]
    periods = sorted(set(anchor))
    if len(periods) < 2:
        raise SimulationError(f"long history {spec.name} has fewer than two periods")

    def returns(component: SeriesSumSpec) -> np.ndarray:
        if component.kind == "level":
            levels = _levels(rows[component.series[0]], periods, max_age_days, spec.name)
            return np.asarray(levels[1:] / levels[:-1] - 1.0, dtype=float)
        total = np.zeros(len(periods) - 1)
        for series_id in component.series:
            by_period = {
                _key(row.observed_on, spec.frequency): row.value for row in rows[series_id]
            }
            try:
                total += np.array(
                    [by_period[_key(day, spec.frequency)] for day in periods[1:]], dtype=float
                )
            except KeyError as missing:
                raise SimulationError(
                    f"long history {spec.name}: {series_id} has no value for {missing}"
                ) from None
        return total

    ends: dict[tuple[int, int], int] = {}
    for index, day in enumerate(periods[1:]):
        ends[(day.year, day.month)] = index
    return HistoryData(
        periods=tuple(periods[1:]),
        roles={role: returns(component) for role, component in spec.roles.items()},
        cash=returns(spec.cash),
        month_ends=tuple(sorted(ends.values())),
    )


def _levels(
    rows: Sequence[Observation], periods: Sequence[date], max_age_days: int, name: str
) -> np.ndarray:
    ordered = sorted(rows, key=lambda row: row.observed_on)
    days = [row.observed_on for row in ordered]
    out = []
    position = 0
    for day in periods:
        while position < len(days) and days[position] <= day:
            position += 1
        latest = position - 1
        if (
            latest < 0
            or day - days[latest] > timedelta(days=max_age_days)
            or not (ordered[latest].value > 0)
        ):
            raise SimulationError(f"long history {name} has no fresh level on {day}")
        out.append(ordered[latest].value)
    return np.array(out, dtype=float)


@dataclass(frozen=True, slots=True)
class _Path:
    """Simple returns per period (rows) of the base's roles (columns) and the cash, the month
    ends to rebalance on (period indices, the first the start) and each period's time in years
    from the start."""

    roles: np.ndarray
    cash: np.ndarray
    rebalance: tuple[int, ...]
    times: np.ndarray


def run_rule(
    path: _Path,
    scaling: TotalRisk,
    bands: Bands,
    cost_per_leg: float,
) -> tuple[np.ndarray, np.ndarray]:
    """The account's value at every period from the first rebalance on (starting at 1), and
    those periods' times: the rule's targets at each rebalance, the bands deciding whether to
    trade, a flat cost on every leg's traded value."""
    weights = scaling.weights
    if np.any(path.roles <= -1) or np.any(path.cash <= -1):
        raise SimulationError("a role or the cash lost everything in a period of the history")
    series = None if scaling.estimator is None else CovarianceSeries(np.log1p(path.roles))
    target = scaling.declared / scaling.periods_per_year
    first = path.rebalance[0]
    value = 1.0
    held = np.zeros(len(weights))
    cash = 1.0
    values = [value]
    ends = [*path.rebalance, path.roles.shape[0] - 1]
    for step, (start, stop) in enumerate(pairwise(ends)):
        risky_share = Decimal(repr(float(held.sum() / value))) if step else None
        if scaling.estimator is None or series is None:
            forecast = scaling.declared_volatility()
        else:
            found = required(series.estimate(start + 1, target, scaling.estimator))
            forecast = portfolio_volatility(found.covariance, weights, scaling.periods_per_year)
        share = float(scaling.share(forecast, risky_share))
        wanted = np.append(share * weights, 1.0 - share)
        current = np.append(held, cash) / value
        width = np.minimum(float(bands.absolute), float(bands.relative) * wanted)
        if not step or bool(np.any(np.abs(current - wanted) > width)):
            traded = float(np.abs(wanted - current).sum()) if step else 1.0
            value *= 1.0 - cost_per_leg * traded
            held = wanted[:-1] * value
            cash = wanted[-1] * value
        for row in range(start + 1, stop + 1):
            held = held * (1.0 + path.roles[row])
            cash *= 1.0 + path.cash[row]
            value = float(held.sum() + cash)
            values.append(value)
    return np.array(values), path.times[first:]


def _real_path(data: HistoryData, keys: Sequence[str], start: int, days_per_year: float) -> _Path:
    last = len(data.periods) - 1
    rebalance = tuple(m for m in data.month_ends if start <= m < last)
    origin = data.periods[rebalance[0]]
    times = np.array(
        [years_between(origin, day, days_per_year=days_per_year) for day in data.periods]
    )
    return _Path(
        roles=np.column_stack([data.roles[key] for key in keys]),
        cash=data.cash,
        rebalance=rebalance,
        times=times,
    )


def _resampled(data: HistoryData, keys: Sequence[str], start: int, order: np.ndarray) -> _Path:
    """The real periods through the start, then the months of ``order`` (indices into the months
    after the start), each month's periods whole, timed in equal steps within the month."""
    months = [m for m in data.month_ends if m >= start]
    blocks = [(months[k], months[k + 1]) for k in range(len(months) - 1)]
    roles = np.column_stack([data.roles[key] for key in keys])
    pieces_roles = [roles[: start + 1]]
    pieces_cash = [data.cash[: start + 1]]
    rebalance = [start]
    times: list[float] = [0.0] * (start + 1)
    for k in order:
        first, last = blocks[int(k)]
        pieces_roles.append(roles[first + 1 : last + 1])
        pieces_cash.append(data.cash[first + 1 : last + 1])
        count = last - first
        times += [len(rebalance) - 1 + (i + 1) / count for i in range(count)]
        rebalance.append(rebalance[-1] + count)
    return _Path(
        roles=np.concatenate(pieces_roles),
        cash=np.concatenate(pieces_cash),
        rebalance=tuple(rebalance[:-1]),
        times=np.array(times) / MONTHS_PER_YEAR,
    )


def long_history_check(
    spec: LongHistorySpec,
    data: HistoryData | None,
    checks: tuple[AdequacyCheck, ...],
    strategies: Mapping[str, TotalRisk],
    *,
    bands: Bands,
    limits: Sequence[Decimal],
    horizons: Sequence[int | None],
    horizon_years: int,
    seed: int,
    days_per_year: float,
) -> HistoryResult:
    """Run every strategy whose base the history covers (the module docstring)."""
    covered = {name: s for name, s in strategies.items() if set(s.base) <= set(spec.roles)}
    skipped = tuple(sorted(set(strategies) - set(covered)))
    if data is None or not all(check.passed for check in checks):
        return HistoryResult(spec, checks, None, None, 0, 0, (), skipped)
    start = next((i for i in data.month_ends if data.periods[i] >= spec.start), None)
    months = [m for m in data.month_ends if start is not None and m >= start]
    if start is None or len(months) < 2:
        failed = (*checks, AdequacyCheck("history", False, "fewer than two months after start"))
        return HistoryResult(spec, failed, None, None, 0, 0, (), skipped)
    count = len(months) - 1
    path_months = min(count, horizon_years * MONTHS_PER_YEAR)
    paths = block_bootstrap_indices(count, block=spec.block_months, n_boot=spec.n_boot, seed=seed)
    runs = []
    rate = float(spec.cost_per_leg)
    for name, scaling in covered.items():
        keys = list(scaling.base)
        real = _real_path(data, keys, start, days_per_year)
        values, times = run_rule(real, scaling, bands, rate)
        stats = drawdown_stats(values, times, [float(x) for x in limits])
        growth = annualised_log_growth(1.0, float(values[-1]), float(times[-1]))
        hits = np.empty((len(paths), len(limits)))
        growths = np.empty(len(paths))
        for row, order in enumerate(paths):
            path = _resampled(data, keys, start, order[:path_months])
            path_values, path_times = run_rule(path, scaling, bands, rate)
            found = drawdown_stats(path_values, path_times, [float(x) for x in limits])
            hits[row] = found.first_hit
            growths[row] = math.log(path_values[-1]) / path_times[-1]
        breach = tuple(
            (limit, horizon, breach_within(hits[:, i], horizon))
            for i, limit in enumerate(limits)
            for horizon in horizons
        )
        runs.append(HistoryRun(name, growth, stats, float(np.median(growths)), breach))
    return HistoryResult(
        spec=spec,
        checks=checks,
        first=data.periods[start],
        last=data.periods[-1],
        months=count,
        path_months=path_months,
        runs=tuple(runs),
        skipped=skipped,
    )
