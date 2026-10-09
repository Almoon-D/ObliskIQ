"""
Small, dependency-free statistical helpers shared across analytics modules.

Simons: "If it's not statistically significant, it doesn't exist" — but a p-value is only
meaningful for ONE pre-specified test. Mining N pairs x K lags at p < 0.05 produces
~0.05 * N * K false "patterns" by construction, so any search must correct for the number of
looks. Keep those corrections here (one implementation, one set of tests) rather than inline.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence


def benjamini_hochberg(p_values: Sequence[float], q: float = 0.05) -> List[bool]:
    """
    Benjamini-Hochberg step-up procedure: control the false discovery rate at ``q``.

    Returns one reject flag per input p-value, in the SAME order as the input. A non-finite
    p-value is never rejected and does not count toward the number of tests ``m``.

    Rule: sort the m finite p-values ascending, find the largest rank k with
    p_(k) <= (k / m) * q, and reject ranks 1..k.
    """
    finite = [(i, float(p)) for i, p in enumerate(p_values) if p is not None and math.isfinite(float(p))]
    m = len(finite)
    reject = [False] * len(p_values)
    if m == 0:
        return reject
    finite.sort(key=lambda pair: pair[1])
    cutoff_rank = 0
    for rank, (_, p) in enumerate(finite, start=1):
        if p <= (rank / m) * q:
            cutoff_rank = rank
    for rank, (index, _) in enumerate(finite, start=1):
        if rank <= cutoff_rank:
            reject[index] = True
    return reject


# ---------------------------------------------------------------------------
# Inference helpers for the AI scoreboard / forecast panel (v6.16.0)
#
# Why these live here and not inline: the scoreboard's verdicts are only as honest as the standard
# errors behind them. Forward returns of consecutive weekly cohorts OVERLAP (a 4-week horizon shares
# 3 of its 4 weeks with the next cohort), so an iid t-test overstates significance. Everything below
# is deliberately small, dependency-free and unit-tested against hand-computed values.
# ---------------------------------------------------------------------------


def normal_cdf(x: float) -> float:
    """Standard normal CDF (closed form via erf; no scipy)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def two_sided_p_from_z(z: float) -> float:
    """Two-sided p-value of a standard-normal statistic."""
    if not math.isfinite(z):
        return 1.0
    return float(math.erfc(abs(z) / math.sqrt(2.0)))


def newey_west_mean(values: Sequence[float], lags: int) -> dict:
    """
    Mean of a serially dependent series with a Newey-West (Bartlett kernel) standard error.

    ``lags`` should be (horizon in sampling periods) - 1 for overlapping forward returns: weekly
    cohorts scored at 4 weeks overlap for 3 lags. The long-run variance is
    ``gamma_0 + 2 * sum_{l=1..L} (1 - l/(L+1)) * gamma_l``, floored at ``gamma_0`` (the iid variance): for
    overlapping forward returns dependence can only be positive in expectation, so a kernel estimate BELOW
    the iid variance is sampling noise (an alternating series, say) that would claim precision the data
    does not have. Conservative by construction.

    Returns ``{"n", "mean", "se", "t", "p", "ci95": (lo, hi)}`` — all None for fewer than 3 points.
    The p-value uses the normal approximation, which is slightly anti-conservative for very small n;
    callers enforce a minimum n before rendering any verdict.
    """
    xs = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    n = len(xs)
    empty = {"n": n, "mean": None, "se": None, "t": None, "p": None, "ci95": None}
    if n < 3:
        return empty
    mean = sum(xs) / n
    dev = [x - mean for x in xs]
    max_lag = max(0, min(int(lags), n - 1))

    def gamma(lag: int) -> float:
        return sum(dev[i] * dev[i - lag] for i in range(lag, n)) / n

    long_run = gamma(0)
    for lag in range(1, max_lag + 1):
        long_run += 2.0 * (1.0 - lag / (max_lag + 1.0)) * gamma(lag)
    long_run = max(long_run, gamma(0))
    se = math.sqrt(long_run / n)
    if se == 0.0:
        return {"n": n, "mean": mean, "se": 0.0, "t": None, "p": None, "ci95": (mean, mean)}
    t_stat = mean / se
    return {
        "n": n,
        "mean": mean,
        "se": se,
        "t": t_stat,
        "p": two_sided_p_from_z(t_stat),
        "ci95": (mean - 1.96 * se, mean + 1.96 * se),
    }


def _average_ranks(values: Sequence[float]) -> List[float]:
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


def spearman_ic(signal: Sequence[float], outcome: Sequence[float]) -> Optional[float]:
    """
    Rank correlation (Information Coefficient) between a signal and a realised outcome.

    Pairs with a non-finite member are dropped. Returns None for fewer than 5 usable pairs or when
    either side has no variance (an IC is undefined, never 0).
    """
    pairs = [
        (float(s), float(o))
        for s, o in zip(signal, outcome)
        if s is not None and o is not None and math.isfinite(float(s)) and math.isfinite(float(o))
    ]
    if len(pairs) < 5:
        return None
    rs = _average_ranks([p[0] for p in pairs])
    ro = _average_ranks([p[1] for p in pairs])
    n = len(pairs)
    ms, mo = sum(rs) / n, sum(ro) / n
    cov = sum((a - ms) * (b - mo) for a, b in zip(rs, ro))
    var_s = sum((a - ms) ** 2 for a in rs)
    var_o = sum((b - mo) ** 2 for b in ro)
    if var_s == 0.0 or var_o == 0.0:
        return None
    return cov / math.sqrt(var_s * var_o)


def brier_score(probabilities: Sequence[float], outcomes: Sequence[int]) -> Optional[float]:
    """Mean squared error of stated probabilities against 0/1 outcomes (lower is better)."""
    pairs = [(float(p), int(o)) for p, o in zip(probabilities, outcomes) if p is not None and math.isfinite(float(p))]
    if not pairs:
        return None
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


def brier_skill(probabilities: Sequence[float], outcomes: Sequence[int]) -> dict:
    """
    Brier skill against the climatological forecast (always state the observed base rate).

    ``skill = 1 - Brier / Brier_ref`` with ``Brier_ref = base * (1 - base)``: positive means the
    stated probabilities carry information beyond "it usually happens X% of the time"; zero or
    negative means they do not. Reference is None (skill None) when the base rate is 0 or 1.
    """
    pairs = [(float(p), int(o)) for p, o in zip(probabilities, outcomes) if p is not None and math.isfinite(float(p))]
    n = len(pairs)
    if n == 0:
        return {"n": 0, "brier": None, "base_rate": None, "brier_ref": None, "skill": None, "mean_stated": None}
    brier = sum((p - o) ** 2 for p, o in pairs) / n
    base = sum(o for _, o in pairs) / n
    ref = base * (1.0 - base)
    return {
        "n": n,
        "brier": brier,
        "base_rate": base,
        "brier_ref": ref,
        "skill": (1.0 - brier / ref) if ref > 0 else None,
        "mean_stated": sum(p for p, _ in pairs) / n,
    }


def bootstrap_mean_ci(
    values: Sequence[float], *, block: int = 1, n_boot: int = 2000, seed: int = 20260702, alpha: float = 0.05
) -> dict:
    """
    Moving-block bootstrap confidence interval for a mean (block > 1 preserves serial dependence).

    Seeded for reproducibility — a verdict must not change between two runs on the same data.
    Returns ``{"n", "mean", "lo", "hi"}`` (lo/hi None for fewer than 5 points).
    """
    import random

    xs = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    n = len(xs)
    if n < 5:
        return {"n": n, "mean": (sum(xs) / n) if n else None, "lo": None, "hi": None}
    block = max(1, min(int(block), n))
    rng = random.Random(seed)
    starts = n - block + 1
    means: List[float] = []
    for _ in range(n_boot):
        sample: List[float] = []
        while len(sample) < n:
            start = rng.randrange(starts)
            sample.extend(xs[start : start + block])
        means.append(sum(sample[:n]) / n)
    means.sort()
    lo = means[int((alpha / 2.0) * n_boot)]
    hi = means[min(n_boot - 1, int((1.0 - alpha / 2.0) * n_boot))]
    return {"n": n, "mean": sum(xs) / n, "lo": lo, "hi": hi}
