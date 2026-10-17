"""The statistics of a pre-registered test (DECISIONS D60, D62, D63).

- **Growth and drawdown**: the annualised log growth of liquidation value over calendar time,
  drawdowns of mark-to-market value and the probability that a path breaches a limit.
- **Dependence**: a moving-block bootstrap (seeded), Newey-West on a series, the Fama-MacBeth
  series of a cross-sectional effect, and n_eff = n / (1 + (n - 1) rho) with rho estimated
  from a panel as the mean pairwise correlation.
- **Inference**: intervals at level 1 - 2 alpha, Bonferroni-adjusted over the tests of a
  confirmation run; the power to detect theta and the effect detectable at a given power; the
  deflated Sharpe ratio across every trial; the break-even cost multiplier.

Every threshold is an argument; callers read them from the pre-registration or the
configuration. ``None`` means "not computable", never zero (as in ``quant.stats``).

**Power** here is the probability that the interval's lower end lies above zero when the true
effect is theta: ``Phi(theta / se - z)`` with ``z`` the critical value of the one-sided tail
``alpha / tests``. The effect detectable at power ``p`` is ``(z + z_p) * se``, exactly its
inverse.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from statistics import NormalDist
from typing import Final

import numpy as np

from ..quant.inference import MeanEstimate, newey_west_mean

#: The Euler-Mascheroni constant, in the expected maximum of normal draws (Bailey and Lopez de
#: Prado 2014).
EULER_MASCHERONI: Final = 0.5772156649015329
#: The fewest pairs a cross-sectional slope is fitted on (a line through two points has no error).
_MIN_SLOPE_PAIRS: Final = 3
#: The divisor of the kurtosis term in the variance of an estimated Sharpe ratio (Mertens 2002).
_KURTOSIS_DIVISOR: Final = 4

_NORMAL = NormalDist()


# ── growth and drawdown ──


def years_between(start: date, end: date, *, days_per_year: float) -> float:
    """Elapsed calendar time in years."""
    return (end - start).days / days_per_year


def annualised_log_growth(initial: float, final: float, years: float) -> float | None:
    """``ln(final / initial) / years``: the growth rate of wealth per year, continuously
    compounded. ``None`` for a non-positive value or an empty period."""
    if initial <= 0 or final <= 0 or years <= 0:
        return None
    return math.log(final / initial) / years


def drawdowns(values: Sequence[float]) -> np.ndarray:
    """Each value against the running peak, as a non-positive fraction."""
    series = np.asarray(values, dtype=float)
    if series.size == 0 or np.any(series <= 0):
        raise ValueError("drawdowns need a non-empty series of positive values")
    return series / np.maximum.accumulate(series) - 1.0


def max_drawdown(values: Sequence[float]) -> float:
    """The deepest fall from a running peak (``-0.3`` is 30 % below it; ``0`` if none)."""
    return float(drawdowns(values).min())


def breach_probability(path_drawdowns: Sequence[float], limit: float) -> float:
    """The share of paths whose maximum drawdown reached ``limit`` (a positive fraction)."""
    if not 0 < limit < 1:
        raise ValueError(f"a drawdown limit is a fraction strictly between 0 and 1, got {limit}")
    worst = np.asarray(path_drawdowns, dtype=float)
    if worst.size == 0:
        raise ValueError("a breach probability needs at least one path")
    return float(np.mean(worst <= -limit))


# ── the block bootstrap ──


def block_bootstrap_indices(n: int, *, block: int, n_boot: int, seed: int) -> np.ndarray:
    """``n_boot`` resampled index paths of length ``n`` made of moving blocks of ``block``.

    Blocks start uniformly among the ``n - block + 1`` possible starts and are laid end to end
    until the path is full; the last one is cut. Seeded: one seed, one set of paths.
    """
    if n < 1 or block < 1 or n_boot < 1:
        raise ValueError(f"n, block and n_boot must be positive, got {n}, {block}, {n_boot}")
    block = min(block, n)
    per_path = -(-n // block)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n - block + 1, size=(n_boot, per_path))
    paths = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(n_boot, -1)
    return paths[:, :n]


def bootstrap(
    values: Sequence[float],
    statistic: Callable[[np.ndarray], float],
    *,
    block: int,
    n_boot: int,
    seed: int,
) -> np.ndarray:
    """``statistic`` of each moving-block resample of ``values``."""
    series = np.asarray(values, dtype=float)
    indices = block_bootstrap_indices(series.size, block=block, n_boot=n_boot, seed=seed)
    return np.array([statistic(series[path]) for path in indices])


def percentile_interval(
    samples: Sequence[float], *, alpha: float, tests: int
) -> tuple[float, float]:
    """The interval at level ``1 - 2 alpha / tests`` from a bootstrap distribution."""
    tail = _tail(alpha, tests)
    draws = np.asarray(samples, dtype=float)
    if draws.size == 0:
        raise ValueError("a percentile interval needs at least one draw")
    low, high = np.quantile(draws, [tail, 1.0 - tail])
    return float(low), float(high)


# ── intervals, power and the Bonferroni adjustment ──


def _tail(alpha: float, tests: int) -> float:
    if not 0 < alpha < 1 / 2:
        raise ValueError(f"alpha must lie strictly between 0 and 0.5, got {alpha}")
    if tests < 1:
        raise ValueError(f"the number of tests must be at least 1, got {tests}")
    return alpha / tests


def critical_value(alpha: float, tests: int) -> float:
    """The standard-normal quantile of ``1 - alpha / tests``: each end of a ``1 - 2 alpha``
    interval, Bonferroni-adjusted over ``tests``."""
    return _NORMAL.inv_cdf(1.0 - _tail(alpha, tests))


def bonferroni_interval(
    estimate: float, se: float, *, alpha: float, tests: int
) -> tuple[float, float]:
    """``estimate -/+ z * se`` at level ``1 - 2 alpha / tests``."""
    if se < 0 or not math.isfinite(se):
        raise ValueError(f"a standard error is finite and not negative, got {se}")
    half = critical_value(alpha, tests) * se
    return estimate - half, estimate + half


def power(theta: float, se: float, *, alpha: float, tests: int) -> float:
    """The probability that the interval's lower end exceeds zero when the effect is ``theta``."""
    if se <= 0:
        raise ValueError(f"power needs a positive standard error, got {se}")
    return _NORMAL.cdf(theta / se - critical_value(alpha, tests))


def detectable_effect(se: float, *, alpha: float, tests: int, target_power: float) -> float:
    """The smallest effect detected with probability ``target_power``."""
    if not 0 < target_power < 1:
        raise ValueError(f"a power is a probability strictly between 0 and 1, got {target_power}")
    return (critical_value(alpha, tests) + _NORMAL.inv_cdf(target_power)) * se


# ── dependence ──


def newey_west(
    values: Sequence[float | None], *, lags: int, alpha: float, tests: int, min_points: int
) -> MeanEstimate:
    """The mean of a dependent series with a Newey-West standard error (Bartlett kernel,
    ``quant.inference``) and its interval at level ``1 - 2 alpha / tests``."""
    return newey_west_mean(values, lags, alpha=2 * _tail(alpha, tests), min_points=min_points)


def effective_n(n: float, rho: float) -> float:
    """Observations that correlate on average by ``rho`` carry ``n / (1 + (n - 1) rho)``."""
    if n < 1:
        raise ValueError(f"n must be at least 1, got {n}")
    if not -1 / max(n - 1, 1) <= rho <= 1:
        raise ValueError(f"rho must lie in [-1 / (n - 1), 1], got {rho}")
    return n / (1 + (n - 1) * rho)


def panel_correlation(panel: Sequence[Sequence[float | None]], *, min_overlap: int) -> float | None:
    """The mean pairwise correlation between the columns (names) of a panel whose rows are
    periods; each pair uses the periods where both are present. ``None`` with no usable pair."""
    matrix = np.array([[np.nan if v is None else v for v in row] for row in panel], dtype=float)
    if matrix.ndim != 2 or matrix.shape[1] < 2:
        return None
    found: list[float] = []
    for first in range(matrix.shape[1]):
        for second in range(first + 1, matrix.shape[1]):
            both = ~np.isnan(matrix[:, first]) & ~np.isnan(matrix[:, second])
            if both.sum() < min_overlap:
                continue
            a, b = matrix[both, first], matrix[both, second]
            if a.std() == 0 or b.std() == 0:
                continue
            found.append(float(np.corrcoef(a, b)[0, 1]))
    return float(np.mean(found)) if found else None


@dataclass(frozen=True, slots=True)
class FamaMacBeth:
    """The per-period slopes of a cross-sectional effect and their Newey-West mean."""

    slopes: tuple[float | None, ...]
    estimate: MeanEstimate


def cross_sectional_slope(
    outcomes: Sequence[float | None], signals: Sequence[float | None]
) -> float | None:
    """The OLS slope of outcomes on signals across names in one period (with an intercept)."""
    pairs = np.array(
        [(s, o) for s, o in zip(signals, outcomes, strict=True) if s is not None and o is not None],
        dtype=float,
    ).reshape(-1, 2)
    if pairs.shape[0] < _MIN_SLOPE_PAIRS:
        return None
    x, y = pairs[:, 0], pairs[:, 1]
    spread = x - x.mean()
    variance = float(spread @ spread)
    if variance == 0:
        return None
    return float(spread @ (y - y.mean())) / variance


def fama_macbeth(
    outcomes: Sequence[Sequence[float | None]],
    signals: Sequence[Sequence[float | None]],
    *,
    lags: int,
    alpha: float,
    tests: int,
    min_periods: int,
) -> FamaMacBeth:
    """One slope per period (rows), then their mean with Newey-West lags at least the horizon."""
    if len(outcomes) != len(signals):
        raise ValueError("outcomes and signals need the same periods")
    slopes = tuple(cross_sectional_slope(o, s) for o, s in zip(outcomes, signals, strict=True))
    estimate = newey_west(slopes, lags=lags, alpha=alpha, tests=tests, min_points=min_periods)
    return FamaMacBeth(slopes, estimate)


# ── the Sharpe ratio across trials ──


def sharpe_ratio(returns: Sequence[float]) -> float | None:
    """The per-period Sharpe ratio of excess returns (sample standard deviation)."""
    series = np.asarray(returns, dtype=float)
    if series.size < 2:
        return None
    spread = float(series.std(ddof=1))
    return None if spread == 0 else float(series.mean()) / spread


def moments(returns: Sequence[float]) -> tuple[float, float] | None:
    """Skewness and (non-excess) kurtosis of a series, population moments."""
    series = np.asarray(returns, dtype=float)
    spread = float(series.std())
    if series.size < 2 or spread == 0:
        return None
    standard = (series - series.mean()) / spread
    return float(np.mean(standard**3)), float(np.mean(standard**4))


def expected_maximum_sharpe(trials: int, sharpe_variance: float) -> float:
    """The Sharpe ratio the best of ``trials`` unskilled trials reaches in expectation."""
    if trials < 1 or sharpe_variance < 0:
        raise ValueError("trials must be at least 1 and the variance not negative")
    if trials == 1:
        return 0.0
    first = _NORMAL.inv_cdf(1 - 1 / trials)
    second = _NORMAL.inv_cdf(1 - 1 / (trials * math.e))
    return math.sqrt(sharpe_variance) * ((1 - EULER_MASCHERONI) * first + EULER_MASCHERONI * second)


@dataclass(frozen=True, slots=True)
class DeflatedSharpe:
    sharpe: float
    benchmark: float
    probability: float


def deflated_sharpe(
    sharpe: float,
    *,
    observations: int,
    skew: float,
    kurtosis: float,
    trials: int,
    sharpe_variance: float,
) -> DeflatedSharpe:
    """The probability that the true Sharpe ratio exceeds what the best of ``trials`` would show
    by chance (Bailey and Lopez de Prado 2014); per-period, non-annualised inputs."""
    if observations < 2:
        raise ValueError(f"the deflated Sharpe needs at least two observations, got {observations}")
    benchmark = expected_maximum_sharpe(trials, sharpe_variance)
    scale = 1 - skew * sharpe + (kurtosis - 1) / _KURTOSIS_DIVISOR * sharpe**2
    if scale <= 0:
        raise ValueError("the moments give a non-positive variance for the Sharpe ratio")
    statistic = (sharpe - benchmark) * math.sqrt(observations - 1) / math.sqrt(scale)
    return DeflatedSharpe(sharpe, benchmark, _NORMAL.cdf(statistic))


# ── the break-even cost multiplier ──


@dataclass(frozen=True, slots=True)
class Breakeven:
    """The cost multiplier at which the effect falls to theta.

    ``multiplier`` is ``None`` when the effect is below theta even without costs
    (``status`` ``below_theta_without_costs``) or still above it at the searched maximum
    (``above_search``, the maximum in ``bound``).
    """

    multiplier: float | None
    status: str
    bound: float


def breakeven_multiplier(
    effect_at: Callable[[float], float], *, theta: float, upper: float, tolerance: float
) -> Breakeven:
    """Bisect for the multiplier ``k`` with ``effect_at(k) == theta``; the effect is assumed to
    fall as costs rise."""
    if upper <= 0 or tolerance <= 0:
        raise ValueError("the search bound and tolerance must be positive")
    if effect_at(0.0) < theta:
        return Breakeven(None, "below_theta_without_costs", 0.0)
    if effect_at(upper) >= theta:
        return Breakeven(None, "above_search", upper)
    low, high = 0.0, upper
    while high - low > tolerance:
        middle = (low + high) / 2
        if effect_at(middle) >= theta:
            low = middle
        else:
            high = middle
    return Breakeven((low + high) / 2, "found", upper)
