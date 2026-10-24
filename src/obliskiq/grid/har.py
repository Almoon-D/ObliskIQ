"""The realised-volatility forecast: HAR-RV (Corsi 2009), fitted by ordinary least squares.

Tomorrow's variance is modelled as a linear function of today's, the past week's mean and the
past month's mean: volatility cascades across horizons, and three averages carry most of what a
long autoregression would. For a horizon of ``h`` sessions the regression is direct: the target
is the mean daily variance over the next ``h`` sessions, and the forecast is annualised to a vol.

* **Close-to-close squared returns** proxy the daily variance, overnight gaps included: the
  forecast feeds the vol-mispricing component (implied against expected realised), and an implied
  vol prices the total variance to expiry, so a range estimator here would bias every spread
  toward "expensive" (lite's rule for ``iv_minus_hv``).
* **Point in time**: a training row's target lies wholly inside the history held on ``as_of``;
  nothing after it is read.
* **Honest refusal**: too little history, a singular design, or a forecast variance that is not
  positive (a linear model can extrapolate below zero) is no forecast, never a clamped one.
  ``n_obs`` counts the training rows, whose ``h``-session targets overlap for ``h > 1``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np

#: The regressors after the intercept: daily, weekly and monthly components.
COMPONENTS: Final = 3


@dataclass(frozen=True, slots=True)
class HarFit:
    """One horizon's forecast and the fit behind it."""

    horizon: int
    forecast_vol: float | None
    r_squared: float | None
    training_rows: int
    coefficients: tuple[float, ...]
    problem: str | None


def daily_variances(closes: Sequence[float]) -> np.ndarray:
    """Squared daily log returns of consecutive positive closes."""
    prices = np.asarray([close for close in closes if close > 0], dtype=float)
    if prices.size < 2:
        return np.empty(0)
    return np.square(np.diff(np.log(prices)))


def _components(variance: np.ndarray, weekly: int, monthly: int) -> np.ndarray:
    """Rows of (daily, weekly mean, monthly mean) for every session with a full month behind it,
    aligned so row ``i`` ends at session ``monthly - 1 + i``."""
    cumulative = np.concatenate(([0.0], np.cumsum(variance)))
    ends = np.arange(monthly, variance.size + 1)
    week = (cumulative[ends] - cumulative[ends - weekly]) / weekly
    month = (cumulative[ends] - cumulative[ends - monthly]) / monthly
    return np.column_stack((variance[ends - 1], week, month))


def fit_har(
    closes: Sequence[float],
    *,
    horizon: int,
    weekly: int,
    monthly: int,
    min_sessions: int,
    trading_days_per_year: float,
) -> HarFit:
    """The HAR-RV forecast of the annualised vol over the next ``horizon`` sessions."""
    variance = daily_variances(closes)
    if variance.size < min_sessions:
        return HarFit(
            horizon,
            None,
            None,
            0,
            (),
            f"{variance.size} daily returns held, below the {min_sessions} a fit needs "
            "(grid.har_rv.min_sessions)",
        )
    features = _components(variance, weekly, monthly)
    # Row i ends at session monthly - 1 + i; its target is the mean of the next `horizon`.
    cumulative = np.concatenate(([0.0], np.cumsum(variance)))
    starts = np.arange(monthly, variance.size - horizon + 1)
    targets = (cumulative[starts + horizon] - cumulative[starts]) / horizon
    rows = targets.size
    design = np.column_stack((np.ones(rows), features[:rows]))
    if rows <= COMPONENTS + 1 or np.linalg.matrix_rank(design) < COMPONENTS + 1:
        return HarFit(horizon, None, None, rows, (), "the design matrix is singular")
    coefficients, *_ = np.linalg.lstsq(design, targets, rcond=None)
    fitted = design @ coefficients
    spread = float(np.sum(np.square(targets - targets.mean())))
    r_squared = None if spread <= 0 else 1.0 - float(np.sum(np.square(targets - fitted))) / spread
    latest = np.concatenate(([1.0], features[-1]))
    predicted = float(latest @ coefficients)
    coefficient_tuple = tuple(float(c) for c in coefficients)
    if not math.isfinite(predicted) or predicted <= 0:
        return HarFit(
            horizon,
            None,
            r_squared,
            rows,
            coefficient_tuple,
            f"the fitted model forecasts a mean daily variance of {predicted:.3g}, which is not "
            "positive; a linear model extrapolated below zero is no forecast",
        )
    return HarFit(
        horizon,
        math.sqrt(predicted * trading_days_per_year),
        r_squared,
        rows,
        coefficient_tuple,
        None,
    )
