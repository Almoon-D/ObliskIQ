"""The forecast check (DECISIONS D92): does an estimator forecast the risk of its base?

On each rebalance date but the last, a total-risk strategy's estimator forecasts its base's
variance (a year's, from its covariance as of that date); the base's realised variance over the
next month is the mean of its squared daily log returns there, a year's worth. A loss scores each
forecast against what followed, and the declared matrix's constant forecast is scored the same
way. The check reports both mean losses, their mean difference with a moving-block bootstrap
interval and the share of months the estimator did better.

It is **a check that the estimator works, never an outcome**: a forecast that follows recent
volatility beats a constant one almost by construction (volatility clusters), so its accuracy
says nothing about growth after tax at the owner's point, which alone decides (D92 rule 1).

The loss is QLIKE, ``r / f - ln(r / f) - 1`` for a realised ``r`` and a forecast ``f``: zero for
a perfect forecast, and robust to the noise in a realised variance (Patton 2011, *Journal of
Econometrics* 160(1)). A month whose realised variance is zero has no QLIKE and is left out.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from ..errors import SimulationError
from ..quant.covariance import CovarianceSeries, EstimatorRule, portfolio_volatility
from .daily import DailyPath
from .stats import bootstrap, percentile_interval


@dataclass(frozen=True, slots=True)
class ForecastCheck:
    """One estimator's forecasts of its base's next-month variance, against the declared
    matrix's. ``difference`` is the mean monthly loss of the estimator minus the declared
    forecast's: below zero, the estimator forecast better."""

    name: str
    estimator: str
    loss: str
    months: int
    mean_loss: float
    mean_declared: float
    difference: float
    interval: tuple[float, float]
    share_better: float


def qlike(realised: np.ndarray, forecast: np.ndarray) -> np.ndarray:
    """``r / f - ln(r / f) - 1``, element by element; both positive."""
    r = np.asarray(realised, dtype=float)
    f = np.asarray(forecast, dtype=float)
    if np.any(r <= 0) or np.any(f <= 0):
        raise ValueError("QLIKE needs positive realised and forecast variances")
    ratio = r / f
    return np.asarray(ratio - np.log(ratio) - 1.0, dtype=float)


def realised_variances(
    path: DailyPath, keys: Sequence[str], weights: np.ndarray, periods_per_year: float
) -> np.ndarray:
    """The base's realised variance in each month of ``path``, a year's worth: the mean of its
    squared daily log returns times the periods in a year."""
    columns = [path.column(key) for key in keys]
    out = []
    for index in range(len(path.dates) - 1):
        month = path.month(index)[:, columns] @ np.asarray(weights, dtype=float)
        out.append(periods_per_year * float(np.mean(np.square(month))))
    return np.array(out)


def forecast_check(
    *,
    name: str,
    estimator: str,
    rule: EstimatorRule,
    declared: np.ndarray,
    weights: np.ndarray,
    keys: Sequence[str],
    path: DailyPath,
    periods_per_year: int,
    block: int,
    n_boot: int,
    seed: int,
    alpha: float,
) -> ForecastCheck:
    """Score ``rule``'s forecasts of the base (``weights`` on ``keys``) over ``path``'s months
    against the constant forecast of the ``declared`` yearly covariance."""
    columns = path.returns[:, [path.column(key) for key in keys]]
    if np.isnan(columns).any():
        raise SimulationError(f"{', '.join(keys)} lack a daily price from the path's start")
    series = CovarianceSeries(columns)
    target = np.asarray(declared, dtype=float) / periods_per_year
    forecasts = []
    for on in path.dates[:-1]:
        found = series.estimate(path.rows_through(on), target, rule)
        if found is None:
            raise SimulationError(f"{name}'s estimator has nothing to forecast with on {on}")
        forecasts.append(portfolio_volatility(found.covariance, weights, periods_per_year) ** 2)
    realised = realised_variances(path, keys, weights, periods_per_year)
    constant = portfolio_volatility(declared, weights, 1) ** 2
    kept = realised > 0
    if not kept.any():
        raise SimulationError(f"{name}'s base never moved: no forecast can be scored")
    own = qlike(realised[kept], np.array(forecasts)[kept])
    flat = qlike(realised[kept], np.full(int(kept.sum()), constant))
    gaps = own - flat
    draws = bootstrap(gaps.tolist(), np.mean, block=block, n_boot=n_boot, seed=seed)
    return ForecastCheck(
        name=name,
        estimator=estimator,
        loss="qlike",
        months=int(kept.sum()),
        mean_loss=float(own.mean()),
        mean_declared=float(flat.mean()),
        difference=float(gaps.mean()),
        interval=percentile_interval(draws.tolist(), alpha=alpha, tests=1),
        share_better=float(np.mean(own < flat)),
    )
