"""The trend tilt's signals: time-series momentum and a moving average, each with a hysteresis
buffer, and the scale they give an asset (MASTER_PLAN §5.4).

- **Momentum** (Moskowitz, Ooi and Pedersen 2012): the asset's return over the last
  ``momentum_months`` month ends in excess of cash's over the same months.
- **Moving average** (Faber 2007): the latest month-end close against the mean of the last
  ``average_months`` month-end closes, as a fraction above or below it.

Each signal is a state, positive or negative. Its first evaluable month sets it by the sign of
the measure; afterwards it turns negative only when the measure falls below ``-hysteresis`` and
positive only when it rises above ``+hysteresis``, so a measure hovering near zero does not flip
it each month. The state is replayed from the first month the history allows, so it depends on
nothing but the closes given. The asset's scale is ``floor + (1 - floor) * positives / signals``:
both positive keep it whole, both negative leave it at the floor, never at zero.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np

#: Momentum and the moving average.
SIGNALS: Final = 2


@dataclass(frozen=True, slots=True)
class TrendRule:
    """The constants of the tilt (a pre-registration fixes them)."""

    momentum_months: int
    average_months: int
    hysteresis: float
    floor: float

    def __post_init__(self) -> None:
        if self.momentum_months < 1 or self.average_months < SIGNALS:
            raise ValueError("momentum needs one month and the average two or more")
        if not 0 <= self.hysteresis < 1:
            raise ValueError(
                f"the hysteresis buffer is a fraction in [0, 1), got {self.hysteresis}"
            )
        if not 0 < self.floor <= 1:
            raise ValueError(f"the scale floor lies in (0, 1], got {self.floor}")

    @property
    def months_needed(self) -> int:
        """Month-end closes the first evaluation needs, the latest included."""
        return max(self.momentum_months + 1, self.average_months)


@dataclass(frozen=True, slots=True)
class TrendState:
    """Both signals at the latest month end, their latest measures and the scale they give."""

    momentum_positive: bool
    average_positive: bool
    momentum: float
    average_gap: float
    scale: float


def hysteretic(measures: Sequence[float] | np.ndarray, buffer: float) -> bool:
    """The state after ``measures`` in order: the first sets it by its sign (zero is
    positive); each later one changes it only by crossing ``±buffer``.

    Only a measure outside the buffer can set the state, so the state after them all is set by
    the last such measure after the first, or by the first when there is none."""
    values = np.asarray(measures, dtype=float)
    if values.size == 0:
        raise ValueError("a state needs at least one measure")
    outside = np.flatnonzero(np.abs(values[1:]) > buffer)
    if outside.size == 0:
        return bool(values[0] >= 0)
    return bool(values[1 + outside[-1]] > 0)


def trend_state(
    closes: Sequence[float], cash: Sequence[float], rule: TrendRule
) -> TrendState | None:
    """The tilt at the last of ``closes`` (month-end closes, oldest first, in one currency), with
    ``cash`` the cash index's closes on the same month ends. ``None`` when the history is
    shorter than the rule needs."""
    if len(cash) != len(closes):
        raise ValueError("the asset's and cash's month ends must align")
    prices = np.asarray(closes, dtype=float)
    bills = np.asarray(cash, dtype=float)
    if prices.size and (prices.min() <= 0 or bills.min() <= 0):
        raise ValueError("closes are positive")
    if prices.size < rule.months_needed:
        return None
    lag = rule.momentum_months
    momentum = prices[lag:] / prices[:-lag] - bills[lag:] / bills[:-lag]
    window = rule.average_months
    running = np.concatenate([[0.0], np.cumsum(prices)])
    averages = (running[window:] - running[:-window]) / window
    gaps = prices[window - 1 :] / averages - 1
    momentum_positive = hysteretic(momentum, rule.hysteresis)
    average_positive = hysteretic(gaps, rule.hysteresis)
    positives = int(momentum_positive) + int(average_positive)
    return TrendState(
        momentum_positive=momentum_positive,
        average_positive=average_positive,
        momentum=float(momentum[-1]),
        average_gap=float(gaps[-1]),
        scale=rule.floor + (1 - rule.floor) * positives / SIGNALS,
    )
