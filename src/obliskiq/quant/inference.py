"""Honest inference: standard errors that respect overlap, corrections for the number of looks, and
skill scores against a base rate.

A verdict is only as honest as the standard error behind it. Forward returns of consecutive
cohorts overlap (a four-week horizon shares three of its four weeks with the next cohort), so an
independent-observations t-test overstates significance. And a p-value means something only for
one pre-specified test: mining N pairs by K lags at p < 0.05 produces about 0.05 * N * K false
"patterns" by construction, so any search must correct for the number of looks. Both live here,
once, with their tests.

Every threshold is an argument. The significance level, the false-discovery rate, the number of
bootstrap resamples, the seed and the minimum number of observations a result needs are all
required keyword arguments with no default: they are decisions, and a decision with a default is
one nobody made. Callers read them from configuration. The critical value comes from the normal
distribution at the level asked for, not from a remembered constant.

Derived from the Simons-style inference helpers of Almoon-D/Inversor-Inteligente
(``modules/analytics/stat_utils.py`` @68280e1, Apache License 2.0,
https://www.apache.org/licenses/LICENSE-2.0), modified and used here under AGPL-3.0-or-later as
the Apache License permits.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist

_SQRT_TWO = math.sqrt(2.0)

#: The fewest observations at which each statistic is an estimate and not a constant.
_MEAN_FLOOR = 2
_RANK_PAIRS_FLOOR = 3


def normal_cdf(x: float) -> float:
    """Standard normal CDF, in closed form through ``erf``."""
    return 0.5 * (1.0 + math.erf(x / _SQRT_TWO))


def two_sided_p_from_z(z: float) -> float:
    """Two-sided p-value of a standard-normal statistic; a non-finite one is not significant."""
    if not math.isfinite(z):
        return 1.0
    return float(math.erfc(abs(z) / _SQRT_TWO))


def critical_value(alpha: float) -> float:
    """The two-sided standard-normal critical value for a test at level ``alpha``."""
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must lie strictly between 0 and 1, got {alpha}")
    return NormalDist().inv_cdf(1.0 - alpha / 2.0)


def benjamini_hochberg(p_values: Sequence[float | None], *, q: float) -> list[bool]:
    """Benjamini-Hochberg step-up procedure: control the false discovery rate at ``q``.

    One reject flag per input p-value, in the same order. A missing or non-finite p-value is never
    rejected and does not count toward the number of tests ``m``.

    Rule: sort the ``m`` finite p-values ascending, find the largest rank ``k`` with
    ``p_(k) <= (k / m) * q``, and reject ranks 1 through ``k``.
    """
    finite = [
        (index, float(p))
        for index, p in enumerate(p_values)
        if p is not None and math.isfinite(float(p))
    ]
    reject = [False] * len(p_values)
    count = len(finite)
    if count == 0:
        return reject
    finite.sort(key=lambda pair: pair[1])
    cutoff_rank = 0
    for rank, (_, p) in enumerate(finite, start=1):
        if p <= (rank / count) * q:
            cutoff_rank = rank
    for rank, (index, _) in enumerate(finite, start=1):
        if rank <= cutoff_rank:
            reject[index] = True
    return reject


def _require_minimum(minimum: int, *, floor: int) -> None:
    """Refuse a minimum so small that the statistic is undefined or meaningless.

    A standard error needs two observations and a rank correlation three; below that the result
    is a constant (an interval of zero width, a coefficient of exactly one), not an estimate.
    """
    if minimum < floor:
        raise ValueError(
            f"a minimum number of observations must be at least {floor} here, got {minimum}"
        )


def _finite(values: Sequence[float | None]) -> list[float]:
    return [float(v) for v in values if v is not None and math.isfinite(float(v))]


@dataclass(frozen=True, slots=True)
class MeanEstimate:
    """A mean with its standard error and a two-sided interval at level ``1 - alpha``."""

    n: int
    mean: float | None
    se: float | None
    t: float | None
    p: float | None
    ci: tuple[float, float] | None


def newey_west_mean(
    values: Sequence[float | None], lags: int, *, alpha: float, min_points: int
) -> MeanEstimate:
    """Mean of a serially dependent series with a Newey-West (Bartlett kernel) standard error.

    ``lags`` should be the horizon in sampling periods minus one for overlapping forward returns:
    weekly cohorts scored at four weeks overlap for three lags. The long-run variance is
    ``gamma_0 + 2 * sum_{l=1..L} (1 - l/(L+1)) * gamma_l``, floored at ``gamma_0`` (the variance of
    independent observations): for overlapping forward returns dependence can only be positive in
    expectation, so a kernel estimate below the independent variance is sampling noise (an
    alternating series, say) that would claim precision the data does not have. Conservative by
    construction, which makes the standard error at least the independent one at any ``lags``.

    Everything is ``None`` below ``min_points`` observations. The p-value uses the normal
    approximation, slightly anti-conservative for very small samples, so callers set
    ``min_points`` high enough before rendering a verdict.
    """
    _require_minimum(min_points, floor=_MEAN_FLOOR)
    z = critical_value(alpha)
    xs = _finite(values)
    n = len(xs)
    if n < min_points:
        return MeanEstimate(n, None, None, None, None, None)
    mean = sum(xs) / n
    deviations = [x - mean for x in xs]
    max_lag = max(0, min(int(lags), n - 1))

    def gamma(lag: int) -> float:
        return sum(deviations[i] * deviations[i - lag] for i in range(lag, n)) / n

    long_run = gamma(0)
    for lag in range(1, max_lag + 1):
        long_run += 2.0 * (1.0 - lag / (max_lag + 1.0)) * gamma(lag)
    long_run = max(long_run, gamma(0))
    se = math.sqrt(long_run / n)
    if se == 0.0:
        return MeanEstimate(n, mean, 0.0, None, None, (mean, mean))
    t_stat = mean / se
    return MeanEstimate(
        n=n,
        mean=mean,
        se=se,
        t=t_stat,
        p=two_sided_p_from_z(t_stat),
        ci=(mean - z * se, mean + z * se),
    )


def _average_ranks(values: Sequence[float]) -> list[float]:
    """1-based ranks with ties given their average rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = average
        i = j + 1
    return ranks


def spearman_ic(
    signal: Sequence[float | None], outcome: Sequence[float | None], *, min_pairs: int
) -> float | None:
    """Rank correlation (information coefficient) between a signal and a realised outcome.

    Pairs with a missing or non-finite member are dropped. ``None`` below ``min_pairs`` usable
    pairs or when either side has no variance: a coefficient is undefined there, never zero.
    """
    _require_minimum(min_pairs, floor=_RANK_PAIRS_FLOOR)
    pairs = [
        (float(s), float(o))
        for s, o in zip(signal, outcome, strict=False)
        if s is not None and o is not None and math.isfinite(float(s)) and math.isfinite(float(o))
    ]
    if len(pairs) < min_pairs:
        return None
    signal_ranks = _average_ranks([s for s, _ in pairs])
    outcome_ranks = _average_ranks([o for _, o in pairs])
    n = len(pairs)
    mean_signal, mean_outcome = sum(signal_ranks) / n, sum(outcome_ranks) / n
    covariance = sum(
        (a - mean_signal) * (b - mean_outcome)
        for a, b in zip(signal_ranks, outcome_ranks, strict=True)
    )
    var_signal = sum((a - mean_signal) ** 2 for a in signal_ranks)
    var_outcome = sum((b - mean_outcome) ** 2 for b in outcome_ranks)
    if var_signal == 0.0 or var_outcome == 0.0:
        return None
    return covariance / math.sqrt(var_signal * var_outcome)


def _scored_pairs(
    probabilities: Sequence[float | None], outcomes: Sequence[int]
) -> list[tuple[float, int]]:
    return [
        (float(p), int(o))
        for p, o in zip(probabilities, outcomes, strict=False)
        if p is not None and math.isfinite(float(p))
    ]


def brier_score(probabilities: Sequence[float | None], outcomes: Sequence[int]) -> float | None:
    """Mean squared error of stated probabilities against 0/1 outcomes (lower is better)."""
    pairs = _scored_pairs(probabilities, outcomes)
    if not pairs:
        return None
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


@dataclass(frozen=True, slots=True)
class BrierSkill:
    """Brier skill against the climatological forecast, with the base rate it was judged against."""

    n: int
    brier: float | None
    base_rate: float | None
    brier_ref: float | None
    skill: float | None
    mean_stated: float | None


def brier_skill(probabilities: Sequence[float | None], outcomes: Sequence[int]) -> BrierSkill:
    """Brier skill against the climatological forecast (always state the observed base rate).

    ``skill = 1 - Brier / Brier_ref`` with ``Brier_ref = base * (1 - base)``: positive means the
    stated probabilities carry information beyond "it usually happens X % of the time"; zero or
    negative means they do not. The reference, and so the skill, is ``None`` when the base rate is
    0 or 1.
    """
    pairs = _scored_pairs(probabilities, outcomes)
    n = len(pairs)
    if n == 0:
        return BrierSkill(0, None, None, None, None, None)
    brier = sum((p - o) ** 2 for p, o in pairs) / n
    base = sum(o for _, o in pairs) / n
    reference = base * (1.0 - base)
    return BrierSkill(
        n=n,
        brier=brier,
        base_rate=base,
        brier_ref=reference,
        skill=(1.0 - brier / reference) if reference > 0 else None,
        mean_stated=sum(p for p, _ in pairs) / n,
    )


@dataclass(frozen=True, slots=True)
class BootstrapInterval:
    n: int
    mean: float | None
    lo: float | None
    hi: float | None


def bootstrap_mean_ci(
    values: Sequence[float | None],
    *,
    block: int,
    n_boot: int,
    seed: int,
    alpha: float,
    min_points: int,
) -> BootstrapInterval:
    """Moving-block bootstrap interval for a mean (``block`` above 1 preserves serial dependence).

    Seeded, because a verdict must not change between two runs on the same data. ``lo`` and ``hi``
    are ``None`` below ``min_points`` observations.
    """
    _require_minimum(min_points, floor=_MEAN_FLOOR)
    critical_value(alpha)
    if n_boot < 1:
        raise ValueError(f"n_boot must be at least 1, got {n_boot}")
    xs = _finite(values)
    n = len(xs)
    if n < min_points:
        return BootstrapInterval(n, (sum(xs) / n) if n else None, None, None)
    block = max(1, min(int(block), n))
    rng = random.Random(seed)
    starts = n - block + 1
    means: list[float] = []
    for _ in range(n_boot):
        sample: list[float] = []
        while len(sample) < n:
            start = rng.randrange(starts)
            sample.extend(xs[start : start + block])
        means.append(sum(sample[:n]) / n)
    means.sort()
    lo = means[int((alpha / 2.0) * n_boot)]
    hi = means[min(n_boot - 1, int((1.0 - alpha / 2.0) * n_boot))]
    return BootstrapInterval(n, sum(xs) / n, lo, hi)
