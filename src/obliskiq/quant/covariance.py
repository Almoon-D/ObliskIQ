"""Covariance estimation for scaling total risk (MASTER_PLAN §5.4; DECISIONS D90, D92).

The covariance of a few assets' returns, estimated from the observations available on a date,
in four steps a pre-registration fixes:

1. **Weights** over the observations, oldest first: an exponentially weighted moving average
   (EWMA) whose weight halves every ``half_life`` observations back, an equal weight on each of
   the last ``window`` observations, or their blend ``ewma_share * EWMA + (1 - ewma_share) *
   window``. Each part sums to 1 over the observations it has, so a short history is weighted
   as fully as a long one.
2. **The second moment** ``S = sum_t w_t x_t x_t'`` about zero (RiskMetrics' convention: a
   daily mean is small beside its spread and is not estimated), and its effective number of
   observations ``n = 1 / sum_t w_t^2`` (Kish): ``T`` for equal weights.
3. **Ledoit-Wolf shrinkage** toward a declared target ``F``: ``delta F + (1 - delta) S`` with
   ``delta = min(1, sum_t w_t^2 ||x_t x_t' - S||^2 / ||F - S||^2)`` (Frobenius norms). This is
   Ledoit and Wolf's optimal intensity (2003, *Journal of Empirical Finance* 10(5); 2004,
   *Journal of Multivariate Analysis* 88(2)) for a target that is not estimated from the
   sample, whose covariance with ``S`` is therefore zero, written for weighted observations:
   with equal weights ``1/T`` the numerator is their ``pi / T`` and the denominator their
   ``gamma``.
4. **A prior** worth ``prior_observations`` observations of ``F``:
   ``(n0 F + n Sigma) / (n0 + n)``. With no observation the estimate is ``F``; as observations
   arrive they take over (DECISIONS D90: such an estimator needs no history before its first
   date).

``F`` is in the units of the returns: a yearly matrix divided by the periods in a year. A
lookback (D92 rule 3) counts an EWMA as three half-lives and a window as its length.

The functions take and return numpy arrays and floats; they know nothing of dates, currencies or
the store. :class:`CovarianceSeries` keeps one history's outer products so that the estimates on
many dates of one path share them.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from dataclasses import dataclass
from typing import Final

import numpy as np

#: An EWMA's lookback in half-lives (D92 rule 3): its weights beyond three half-lives sum to
#: an eighth.
HALF_LIVES_PER_LOOKBACK: Final = 3
#: The fewest observations an equally weighted window holds.
MIN_WINDOW: Final = 2


@dataclass(frozen=True, slots=True)
class EstimatorRule:
    """The constants of an estimator (the module docstring); a pre-registration fixes them.

    ``half_life`` and ``window`` count observations; either may be ``None`` when the blend gives
    its part no weight. ``shrinkage`` turns Ledoit-Wolf shrinkage toward the target on.
    """

    half_life: float | None
    window: int | None
    ewma_share: float
    shrinkage: bool
    prior_observations: float

    def __post_init__(self) -> None:
        if not 0 <= self.ewma_share <= 1:
            raise ValueError(f"the EWMA's share of the blend lies in [0, 1], got {self.ewma_share}")
        if self.ewma_share > 0 and (self.half_life is None or not self.half_life > 0):
            raise ValueError("an EWMA with weight in the blend needs a positive half-life")
        if self.ewma_share < 1 and (self.window is None or self.window < MIN_WINDOW):
            raise ValueError(f"a window with weight in the blend holds {MIN_WINDOW} or more")
        if not self.prior_observations >= 0 or not math.isfinite(self.prior_observations):
            raise ValueError("the prior's weight is a finite number of observations, 0 or more")

    @property
    def lookback(self) -> float:
        """Observations the estimate reads back (D92 rule 3): three half-lives of an EWMA, the
        length of a window, the longer of the two in a blend."""
        ewma = (
            HALF_LIVES_PER_LOOKBACK * self.half_life
            if self.ewma_share > 0 and self.half_life is not None
            else 0.0
        )
        window = float(self.window) if self.ewma_share < 1 and self.window is not None else 0.0
        return max(ewma, window)


def observation_weights(count: int, rule: EstimatorRule) -> np.ndarray:
    """Each of ``count`` observations' weight, oldest first, summing to 1."""
    if count < 1:
        raise ValueError("weights need at least one observation")
    weights = np.zeros(count)
    if rule.ewma_share > 0 and rule.half_life is not None:
        decay = 0.5 ** (1.0 / rule.half_life)
        ewma = decay ** np.arange(count - 1, -1, -1, dtype=float)
        weights += rule.ewma_share * ewma / ewma.sum()
    if rule.ewma_share < 1 and rule.window is not None:
        held = min(rule.window, count)
        weights[count - held :] += (1 - rule.ewma_share) / held
    return weights


def effective_observations(weights: np.ndarray) -> float:
    """Kish's effective sample size of weights summing to 1: ``1 / sum w^2``."""
    return float(1.0 / np.sum(np.square(weights)))


@dataclass(frozen=True, slots=True)
class Estimate:
    """A covariance estimate, the effective observations behind it and the shrinkage
    intensity applied (``None`` when shrinkage is off or no observation exists)."""

    covariance: np.ndarray
    observations: float
    intensity: float | None


def check_target(target: np.ndarray, assets: int) -> np.ndarray:
    """``target`` as a symmetric positive-definite ``assets x assets`` matrix, or refused."""
    matrix = np.asarray(target, dtype=float)
    if matrix.shape != (assets, assets):
        raise ValueError(f"{assets} assets need an {assets}x{assets} target, got {matrix.shape}")
    if not np.all(np.isfinite(matrix)) or not np.allclose(matrix, matrix.T):
        raise ValueError("the target is a finite symmetric matrix")
    if np.linalg.eigvalsh(matrix).min() <= 0:
        raise ValueError("the target is not positive definite")
    return matrix


class CovarianceSeries:
    """One history of returns (rows are observations, oldest first; columns assets) and the
    outer products every estimate on it reads."""

    def __init__(self, returns: np.ndarray) -> None:
        rows = np.asarray(returns, dtype=float)
        if rows.ndim != 2 or rows.shape[1] < 1:
            raise ValueError("returns are a two-dimensional array with one column per asset")
        if not np.all(np.isfinite(rows)):
            raise ValueError("returns are finite")
        self.returns = rows
        self.outer = rows[:, :, None] * rows[:, None, :]
        #: Each observation's squared Frobenius norm of its outer product.
        self.outer_norms = np.einsum("tij,tij->t", self.outer, self.outer)

    @property
    def assets(self) -> int:
        return int(self.returns.shape[1])

    def __len__(self) -> int:
        return int(self.returns.shape[0])

    def estimate(self, count: int, target: np.ndarray, rule: EstimatorRule) -> Estimate | None:
        """The estimate from the first ``count`` observations; ``None`` with none and no prior."""
        prior = check_target(target, self.assets)
        if not 0 <= count <= len(self):
            raise ValueError(f"{count} observations asked of a history of {len(self)}")
        if count == 0:
            if rule.prior_observations > 0:
                return Estimate(prior.copy(), 0.0, None)
            return None
        weights = observation_weights(count, rule)
        outer = self.outer[:count]
        moment = np.tensordot(weights, outer, axes=1)
        observations = effective_observations(weights)
        covariance, intensity = moment, None
        if rule.shrinkage:
            squared = np.square(weights)
            # sum_t w_t^2 ||P_t - S||^2, expanded so that the outer products are read once.
            spread = (
                float(squared @ self.outer_norms[:count])
                - 2.0 * float(np.sum(np.tensordot(squared, outer, axes=1) * moment))
                + float(np.sum(moment * moment)) * float(squared.sum())
            )
            distance = float(np.sum(np.square(prior - moment)))
            intensity = 1.0 if distance == 0 else min(1.0, max(spread, 0.0) / distance)
            covariance = intensity * prior + (1.0 - intensity) * moment
        n0 = rule.prior_observations
        covariance = (n0 * prior + observations * covariance) / (n0 + observations)
        return Estimate(covariance, observations, intensity)


def required(found: Estimate | None) -> Estimate:
    """``found``, which a caller with an observation or a prior always has."""
    if found is None:
        raise ValueError("no observation and no prior: nothing to estimate from")
    return found


def estimate(returns: np.ndarray, target: np.ndarray, rule: EstimatorRule) -> Estimate | None:
    """The estimate from every row of ``returns`` (the module docstring)."""
    series = CovarianceSeries(returns)
    return series.estimate(len(series), target, rule)


def portfolio_volatility(
    covariance: np.ndarray, weights: np.ndarray, periods_per_year: float
) -> float:
    """``sqrt(periods_per_year * w' C w)``: a yearly volatility from a per-period covariance."""
    w = np.asarray(weights, dtype=float)
    variance = float(w @ np.asarray(covariance, dtype=float) @ w)
    if not variance > 0 or periods_per_year <= 0:
        raise ValueError("a volatility needs a positive variance and periods in a year")
    return math.sqrt(periods_per_year * variance)
