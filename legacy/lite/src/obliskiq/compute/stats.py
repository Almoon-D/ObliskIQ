"""Pure numerics for the computation layer.

No config, no store, no logging. Everything here takes numbers and returns
numbers, which is what makes the four engines above it testable against hand-
computed values rather than against each other.

Two conventions hold throughout.

**``None`` means "not computable", never zero.** A correlation over four
observations, a beta over eight months, a percentile over nine points: each is a
number a caller could print and nobody could defend. Returning ``None`` forces
the caller to write ``n/a`` with a reason, which is the whole discipline of the
grid. Every function that can fail says how many observations it wanted.

**Returns are simple, volatilities are log.** A return quoted to a user is
``last/first - 1`` because that is what the account actually earns. Dispersion is
measured on log returns because they are additive over time and symmetric, which
is what an annualisation and a correlation assume.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise

#: A sample needs at least this many points before a standard deviation, a
#: correlation or a regression means anything at all. Callers impose their own,
#: larger floors from ``grid.min_observations``; this is the arithmetic limit
#: below which the operation is undefined rather than merely weak.
MIN_SAMPLE = 2


def log_returns(values: list[float]) -> list[float]:
    """Log returns of consecutive positive values.

    Non-positive values break the logarithm, so a pair containing one is dropped
    rather than clamped: a zero print is bad data, and clamping it to a floor
    invents a return that never happened.

    An infinite bar has the same effect one step later than the positivity
    check catches: both endpoints can be positive and finite-looking while
    their RATIO is not — a finite price following an infinite one divides to
    exactly ``0.0``, which ``math.log`` refuses with a ``ValueError`` rather
    than returning a number, and an infinite price following a finite one
    divides to ``inf``, which ``math.log`` accepts and returns as ``inf``
    without complaint. Both are dropped here rather than raised or returned:
    a pair spanning a non-finite bar has no return that means anything, and a
    caller assembling its own series two bars at a time must not have this
    step in the pipeline decide whether the whole computation crashes.
    """
    out: list[float] = []
    for previous, current in pairwise(values):
        if previous is None or current is None or previous <= 0 or current <= 0:
            continue
        ratio = current / previous
        if not math.isfinite(ratio) or ratio <= 0:
            continue
        out.append(math.log(ratio))
    return out


#: A PRICE series changes multiplicatively, so its change is a log return.
TRANSFORM_LOG_RETURN = "log_return"

#: A RATE series does not. A log return on a rate near zero is not a return and
#: on a rate AT zero it does not exist — ECB_DFR has sat at zero — so the change
#: of a rate is the first difference of its level, in the series' own units.
TRANSFORM_LEVEL_CHANGE = "level_change"

TRANSFORMS = (TRANSFORM_LOG_RETURN, TRANSFORM_LEVEL_CHANGE)


def _period_change(previous: float, current: float, transform: str) -> float | None:
    """One period's change under ``transform``, or ``None`` where it has none.

    Both branches guard against a non-finite endpoint reaching the caller as a
    change: a level difference against an infinite rate is itself infinite or
    ``nan``, and a log-return ratio that is not finite and positive is not a
    return — see :func:`log_returns` for why the ratio, not just the two
    endpoints, has to be checked.
    """
    if transform == TRANSFORM_LEVEL_CHANGE:
        if not math.isfinite(previous) or not math.isfinite(current):
            return None
        return current - previous
    if previous <= 0 or current <= 0:
        return None
    ratio = current / previous
    if not math.isfinite(ratio) or ratio <= 0:
        return None
    return math.log(ratio)


def paired_changes(
    left: list[float],
    right: list[float],
    *,
    left_transform: str = TRANSFORM_LOG_RETURN,
    right_transform: str = TRANSFORM_LOG_RETURN,
) -> tuple[list[float], list[float]]:
    """Period-by-period changes of two ALIGNED series, kept in lockstep.

    Correlating two series means correlating them period by period, so a period
    either survives in both or in neither. Transforming each separately does not
    do that: it drops a pair wherever ONE side has an unusable print, the two
    outputs then differ in length, and slicing the last N of each silently pairs
    a Tuesday with a Thursday.

    The two transforms are independent because the two series need not be the
    same KIND of thing. An equity index against a policy rate is a log return
    against a level change, and forcing one transform on both would either take
    a logarithm of a rate that can be zero or turn a price series into a
    non-comparable difference. What each side is, is a property of the series and
    is recorded on the row.

    ``left`` and ``right`` must already be aligned by date.
    """
    if len(left) != len(right):
        raise ValueError(f"paired_changes needs aligned series, got {len(left)} and {len(right)}")
    for transform in (left_transform, right_transform):
        if transform not in TRANSFORMS:
            raise ValueError(f"unknown transform {transform!r}; expected one of {TRANSFORMS}")
    out_left: list[float] = []
    out_right: list[float] = []
    for index in range(1, len(left)):
        a0, a1 = left[index - 1], left[index]
        b0, b1 = right[index - 1], right[index]
        if None in (a0, a1, b0, b1):
            continue
        change_left = _period_change(a0, a1, left_transform)
        change_right = _period_change(b0, b1, right_transform)
        if change_left is None or change_right is None:
            continue
        out_left.append(change_left)
        out_right.append(change_right)
    return out_left, out_right


def paired_log_returns(left: list[float], right: list[float]) -> tuple[list[float], list[float]]:
    """Log returns of two ALIGNED price series, kept in lockstep.

    The price-against-price case of :func:`paired_changes`, which every caller
    predating the cross-asset correlation matrix wants. A period is kept only
    when all four prices bounding it are positive.
    """
    return paired_changes(left, right)


def simple_return(first: float | None, last: float | None) -> float | None:
    """``last / first - 1``. ``None`` when either endpoint is unusable."""
    if first is None or last is None or first <= 0:
        return None
    return last / first - 1.0


def mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def stdev(values: list[float]) -> float | None:
    """Sample standard deviation (n-1). ``None`` below :data:`MIN_SAMPLE`."""
    if len(values) < MIN_SAMPLE:
        return None
    average = sum(values) / len(values)
    variance = sum((v - average) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def percentile_of(sample: list[float], value: float) -> float | None:
    """Where ``value`` sits in ``sample``, as a fraction in [0, 1].

    The fraction of observations at or below it. Ties count as below, so a series
    pinned at one level reads as the 100th percentile rather than the 50th —
    which is the honest reading of "it has never been higher".
    """
    usable = [v for v in sample if v is not None and math.isfinite(v)]
    if not usable:
        return None
    at_or_below = sum(1 for v in usable if v <= value)
    return at_or_below / len(usable)


def drawdown_from_peak(values: list[float]) -> float | None:
    """Current level against the running peak, as a negative fraction.

    ``-0.32`` is 32% below the peak. Zero means the series is at its high.
    """
    usable = [v for v in values if v is not None and v > 0]
    if not usable:
        return None
    peak = max(usable)
    if peak <= 0:
        return None
    return usable[-1] / peak - 1.0


def max_drawdown(values: list[float]) -> float | None:
    """The LARGEST peak-to-trough decline inside the window, as a negative fraction.

    Distinct from :func:`drawdown_from_peak`, which measures where the series
    sits *today* relative to its highest point. This measures the worst fall it
    lived through, wherever in the window that happened — a market fully
    recovered from a 40% collapse reads zero on one and -0.40 on the other, and
    only the second is a statement about risk.

    ``None`` for an empty or unusable series, never zero: no drawdown measured is
    not a drawdown of nothing.
    """
    usable = [v for v in values if v is not None and v > 0]
    if not usable:
        return None
    peak = usable[0]
    worst = 0.0
    for value in usable:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst


def distance_from_low(values: list[float]) -> float | None:
    """How far the last value sits above the window low, as a positive fraction."""
    usable = [v for v in values if v is not None and v > 0]
    if not usable:
        return None
    trough = min(usable)
    if trough <= 0:
        return None
    return usable[-1] / trough - 1.0


def correlation(left: list[float], right: list[float]) -> float | None:
    """Pearson correlation. ``None`` when either series has no dispersion.

    A flat series has no correlation with anything — the coefficient is 0/0, not
    zero — and reporting zero would read as decoupling rather than as a halted
    quote.
    """
    if len(left) != len(right) or len(left) < MIN_SAMPLE:
        return None
    mean_left = sum(left) / len(left)
    mean_right = sum(right) / len(right)
    covariance = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right, strict=True))
    var_left = sum((a - mean_left) ** 2 for a in left)
    var_right = sum((b - mean_right) ** 2 for b in right)
    # An infinite reading in either series makes its own mean and variance
    # infinite or nan, which the plain `<= 0` test does not catch — `inf <= 0`
    # is False, so a flat-series refusal built for the zero case let an
    # infinite one through as a confident, wrong coefficient. The function's
    # own contract is None rather than a number nobody could defend.
    if (
        not math.isfinite(var_left)
        or not math.isfinite(var_right)
        or var_left <= 0
        or var_right <= 0
    ):
        return None
    result = covariance / math.sqrt(var_left * var_right)
    return result if math.isfinite(result) else None


def rolling_correlations(
    left: list[float],
    right: list[float],
    *,
    window: int,
    sample: int | None = None,
    step: int | None = None,
) -> list[float]:
    """``window``-length correlations across the last ``sample`` observations.

    The pair's own history, which is what a shift is measured against. Two
    markets that always sit at 0.9 and two that never correlate both deviate
    meaningfully from their own norm, and a single global threshold sees only
    the first.

    ``step`` is how far apart consecutive readings are taken, and it decides
    whether their standard deviation means anything. Stepping by one session —
    the default, and what a chart wants — makes consecutive readings share all
    but one observation, so the series is smooth by construction and its sd
    measures that smoothness rather than the spread of the underlying
    correlation. Passing ``step=window`` gives NON-OVERLAPPING readings, which
    is what :func:`norm_deviation` needs to produce a z-score anybody can
    defend. The last reading is always the most recent one, so the current
    correlation is the value being tested however the history is sampled.
    """
    if window <= 0 or len(left) != len(right):
        return []
    if sample is not None and sample > 0:
        left, right = left[-sample:], right[-sample:]
    stride = 1 if step is None else max(1, step)
    out: list[float] = []
    # Walk backwards from the newest observation so the final window always ends
    # on it; forwards, a stride that does not divide the sample would leave the
    # most recent sessions out of the history entirely.
    ends = list(range(len(left), window - 1, -stride))
    for end in reversed(ends):
        value = correlation(left[end - window : end], right[end - window : end])
        if value is not None:
            out.append(value)
    return out


@dataclass(frozen=True)
class NormDeviation:
    """How far the latest reading sits from its own history, in sample sigmas."""

    current: float
    mean: float
    sd: float
    z_score: float


def norm_deviation(history: list[float]) -> NormDeviation | None:
    """The last value of ``history`` against the baseline the rest of it forms.

    The current reading is excluded from its own baseline: including it drags
    the mean toward whatever is being tested and shrinks every deviation.
    ``None`` when the baseline is too short or has no dispersion — a history
    pinned at one value has nothing to deviate from, and reporting zero would
    read as "unremarkable" rather than "unmeasurable".
    """
    if len(history) < MIN_SAMPLE + 1:
        return None
    baseline = history[:-1]
    centre = mean(baseline)
    spread = stdev(baseline)
    if centre is None or spread is None or spread <= 0:
        return None
    return NormDeviation(
        current=history[-1],
        mean=centre,
        sd=spread,
        z_score=(history[-1] - centre) / spread,
    )


@dataclass(frozen=True)
class Regression:
    """An ordinary least-squares fit of ``y`` on ``x``."""

    beta: float
    alpha: float
    r2: float
    observations: int


def ols(y: list[float], x: list[float]) -> Regression | None:
    """Regress ``y`` on ``x``. ``None`` when the fit is undefined.

    Used for a commodity proxy's beta and R-squared. R-squared is what decides
    whether the proxy may be described as commodity exposure at all: a beta with
    no explanatory power is a coefficient, not a relationship.
    """
    if len(y) != len(x) or len(y) < MIN_SAMPLE:
        return None
    mean_x = sum(x) / len(x)
    mean_y = sum(y) / len(y)
    var_x = sum((v - mean_x) ** 2 for v in x)
    # Same reasoning as correlation(): an infinite reading makes var_x infinite
    # or nan rather than tripping the `<= 0` floor built for a flat series.
    if not math.isfinite(var_x) or var_x <= 0:
        return None
    covariance = sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y, strict=True))
    beta = covariance / var_x
    alpha = mean_y - beta * mean_x
    if not math.isfinite(beta) or not math.isfinite(alpha):
        return None
    total = sum((b - mean_y) ** 2 for b in y)
    if not math.isfinite(total) or total <= 0:
        return None
    residual = sum((b - (alpha + beta * a)) ** 2 for a, b in zip(x, y, strict=True))
    # r2 is the gate a commodity proxy's explanatory power is judged on. A non-
    # finite residual must not collapse to the confident 0.0 that
    # `max(0.0, 1.0 - nan/total)` would otherwise silently produce — that reads
    # as "measured, no relationship" when the true answer is "not measurable".
    if not math.isfinite(residual):
        return None
    return Regression(
        beta=beta,
        alpha=alpha,
        r2=max(0.0, 1.0 - residual / total),
        observations=len(y),
    )


def true_ranges(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    """Wilder's true range per bar, from the second bar onward.

    ``max(high - low, |high - prev_close|, |low - prev_close|)``. The gap terms
    are the point: a name that opens six percent lower has a true range far
    larger than its intraday span, and an average that missed that would set a
    stop inside the gap.
    """
    out: list[float] = []
    for index in range(1, min(len(highs), len(lows), len(closes))):
        high, low, previous = highs[index], lows[index], closes[index - 1]
        if high is None or low is None or previous is None:
            continue
        out.append(max(high - low, abs(high - previous), abs(low - previous)))
    return out


def average_true_range(
    highs: list[float], lows: list[float], closes: list[float], *, period: int
) -> float | None:
    """Mean true range over the last ``period`` bars. ``None`` when short.

    A simple mean of the last ``period`` true ranges rather than Wilder's
    exponential smoothing. Both are called ATR; the simple mean responds to a
    volatility change in exactly ``period`` bars, which makes a widening stop
    explainable from the bars a reader can see.
    """
    ranges = true_ranges(highs, lows, closes)
    if period <= 0 or len(ranges) < period:
        return None
    window = ranges[-period:]
    return sum(window) / len(window)


def annualise(daily_sigma: float, *, trading_days_per_year: float) -> float:
    """Scale a per-session standard deviation to a year."""
    return daily_sigma * math.sqrt(trading_days_per_year)


def annualised_from_period(total_return: float, *, years: float) -> float | None:
    """Compound a cumulative return down to an annual rate.

    ``(1 + r) ** (1 / years) - 1``. ``None`` when the period is degenerate or the
    cumulative return wipes out the position: there is no annual rate that
    compounds to minus one hundred percent over a fraction of a year.
    """
    if years <= 0 or total_return <= -1.0:
        return None
    return (1.0 + total_return) ** (1.0 / years) - 1.0
