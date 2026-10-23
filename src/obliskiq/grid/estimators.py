"""The realised-volatility estimators the grid reads, and the non-overlapping rolling readings
their percentiles rank against.

Close-to-close comes from the collectors' own implementation (``data.market.vol``), so the strike
band and the cell cannot disagree. Parkinson and Garman-Klass are ported from lite's
``collect/vol.py``: they read the intraday range and see nothing of the overnight gap, so they are
estimators of a different quantity (materially lower on any market that closes at night), each its
own named leaf with its own percentile, and nothing subtracts one from an implied vol.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from collections.abc import Callable, Sequence
from itertools import pairwise
from typing import Final

from ..data.market.vol import realised_vol

#: Parkinson's constant: the expected squared log range of a driftless Brownian motion over one
#: period is ``4 ln 2`` times its variance.
PARKINSON_SCALE: Final = 4.0 * math.log(2.0)
#: Garman-Klass's weight on the squared open-to-close return, ``2 ln 2 - 1``. Subtracted, but it
#: cannot win on a well-formed bar: the open and the close lie inside the range.
GARMAN_KLASS_CLOSE_WEIGHT: Final = 2.0 * math.log(2.0) - 1.0

type RangeBar = tuple[float, float]
type OhlcBar = tuple[float, float, float, float]


def range_bars(highs: Sequence[float | None], lows: Sequence[float | None]) -> list[RangeBar]:
    """Usable ``(high, low)`` pairs, oldest first: both present, positive and ordered. A bar is a
    term of its own, so dropping an unusable one splices nothing."""
    out: list[RangeBar] = []
    for high, low in zip(highs, lows, strict=True):
        if high is None or low is None or low <= 0 or high < low:
            continue
        out.append((high, low))
    return out


def ohlc_bars(
    opens: Sequence[float | None],
    highs: Sequence[float | None],
    lows: Sequence[float | None],
    closes: Sequence[float],
) -> list[OhlcBar]:
    """Usable ``(open, high, low, close)`` bars: an open or a close outside its own bar's range
    is a corrupt row, not a wide one."""
    out: list[OhlcBar] = []
    for open_, high, low, close in zip(opens, highs, lows, closes, strict=True):
        if open_ is None or high is None or low is None or low <= 0 or high < low:
            continue
        if not (low <= open_ <= high and low <= close <= high):
            continue
        out.append((open_, high, low, close))
    return out


def restated_open_share(bars: Sequence[OhlcBar]) -> float | None:
    """The share of bars whose open is exactly the previous close: a venue that publishes no
    opening print restates the close, and Garman-Klass would then compute a different formula.
    Compared exactly, since a restatement is a copied value; ``None`` below two bars."""
    gaps = list(pairwise(bars))
    if not gaps:
        return None
    restated = sum(1 for previous, current in gaps if current[0] == previous[-1])
    return restated / len(gaps)


def parkinson(bars: Sequence[RangeBar], *, trading_days_per_year: float) -> float | None:
    """Annualised Parkinson volatility over exactly ``bars``; ``None`` without any range."""
    if not bars:
        return None
    total = sum(math.log(high / low) ** 2 for high, low in bars)
    if total <= 0:
        return None
    return math.sqrt(total / (PARKINSON_SCALE * len(bars)) * trading_days_per_year)


def garman_klass(bars: Sequence[OhlcBar], *, trading_days_per_year: float) -> float | None:
    """Annualised Garman-Klass volatility over exactly ``bars``; ``None`` without any range (a
    non-positive variance is refused, never square-rooted)."""
    if not bars:
        return None
    total = 0.0
    for open_, high, low, close in bars:
        total += 0.5 * math.log(high / low) ** 2
        total -= GARMAN_KLASS_CLOSE_WEIGHT * math.log(close / open_) ** 2
    if total <= 0:
        return None
    return math.sqrt(total / len(bars) * trading_days_per_year)


def close_to_close(
    closes: Sequence[float], tenor: int, *, trading_days_per_year: float
) -> float | None:
    """Annualised close-to-close volatility over the last ``tenor`` returns (``tenor + 1``
    closes), the collectors' implementation."""
    return realised_vol(closes, tenor_days=tenor, trading_days_per_year=trading_days_per_year)


def rolling[T](
    values: Sequence[T], span: int, measure: Callable[[Sequence[T]], float | None], *, step: int
) -> list[float]:
    """Every reading ``measure`` yields over windows of ``span`` items, ``step`` apart.

    Walks backwards from the newest item so the last window always ends on it. ``step`` decides
    what the count means: one session apart, two windows share all but one observation and a
    hundred readings are not a hundred pieces of evidence; stepped by the tenor they share none.
    A window ``measure`` declines contributes nothing, never a zero.
    """
    stride = max(1, step)
    out: list[float] = []
    for end in reversed(range(len(values), span - 1, -stride)):
        value = measure(values[end - span : end])
        if value is not None:
            out.append(value)
    return out


def rolling_close_to_close(
    closes: Sequence[float], tenor: int, *, trading_days_per_year: float
) -> list[float]:
    """The non-overlapping ``tenor``-day close-to-close readings, for a percentile: windows of
    ``tenor + 1`` closes stepped by ``tenor``, so consecutive ones share a boundary close and no
    return."""
    usable = [close for close in closes if close > 0]
    return rolling(
        usable,
        tenor + 1,
        lambda window: close_to_close(window, tenor, trading_days_per_year=trading_days_per_year),
        step=tenor,
    )
