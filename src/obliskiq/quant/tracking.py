"""How closely two price series move together: the correlation of their periodic log returns.

The vehicle selector ranks a vehicle by how closely it tracks its exposure's data series, and a
swap partner must move with the vehicle it replaces (MASTER_PLAN §5.4, §5.8). Both are this one
measurement, on two series already in one currency (the caller converts them): each series is
reduced to its last value in each period (a calendar month, or an ISO week), the periods both
series hold are kept, and a return is taken only between consecutive periods, so a missing period
never stretches a return over two. Not daily returns: listings on different venues close at
different times (a European ETF hours before the index it tracks is struck), which makes daily,
and even weekly, returns of one exposure look less correlated than they are (DECISIONS D121).

No reading is fabricated. Too few common returns, or a series that does not move, is a reading
without a value and with the reason; it is never interpolated or defaulted.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from typing import Literal

import numpy as np

type Point = tuple[date, float]
type Period = Literal["month", "week"]
type PeriodKey = tuple[int, int]

DAYS_PER_WEEK = 7
MONTHS_PER_YEAR = 12


@dataclass(frozen=True, slots=True)
class CorrelationReading:
    """A correlation of periodic log returns, or why there is none.

    ``value`` is ``None`` exactly when ``reason`` says why it could not be measured; ``returns``
    is the number of common returns of ``period`` it rests on, and ``first``/``last`` their span.
    """

    value: float | None
    returns: int
    first: date | None
    last: date | None
    reason: str | None
    period: Period = "month"

    @property
    def measured(self) -> bool:
        return self.value is not None

    @classmethod
    def unmeasured(cls, reason: str, returns: int = 0) -> CorrelationReading:
        return cls(None, returns, None, None, reason)

    def describe(self) -> str:
        if self.value is None:
            return f"unmeasured: {self.reason}"
        return (
            f"{self.value:.3f} on {self.returns} {self.period}ly returns, {self.first} to "
            f"{self.last}"
        )


def _key(day: date, period: Period) -> PeriodKey:
    if period == "month":
        return (day.year, day.month)
    iso = day.isocalendar()
    return (iso.year, iso.week)


def _last_values(points: Sequence[Point], period: Period) -> dict[PeriodKey, Point]:
    """The last positive, finite value of each period."""
    values: dict[PeriodKey, Point] = {}
    for day, value in sorted(points):
        if math.isfinite(value) and value > 0:
            values[_key(day, period)] = (day, value)
    return values


def _consecutive(earlier: PeriodKey, later: PeriodKey, period: Period) -> bool:
    """Whether ``later`` is the period right after ``earlier``."""
    if period == "month":
        return (later[0] - earlier[0]) * MONTHS_PER_YEAR + later[1] - earlier[1] == 1
    gap = date.fromisocalendar(*later, 1) - date.fromisocalendar(*earlier, 1)
    return gap.days == DAYS_PER_WEEK


def return_correlation(
    first: Sequence[Point], second: Sequence[Point], *, period: Period, min_returns: int
) -> CorrelationReading:
    """The correlation of the log returns of ``first`` and ``second`` (one currency) over
    consecutive periods both hold."""
    a, b = _last_values(first, period), _last_values(second, period)
    keys = sorted(set(a) & set(b))
    if len(keys) < 2:
        return CorrelationReading.unmeasured(f"{len(keys)} common {period}(s) of prices")
    ra: list[float] = []
    rb: list[float] = []
    for earlier, later in pairwise(keys):
        if not _consecutive(earlier, later, period):
            continue
        ra.append(math.log(a[later][1] / a[earlier][1]))
        rb.append(math.log(b[later][1] / b[earlier][1]))
    if len(ra) < min_returns:
        return CorrelationReading.unmeasured(
            f"{len(ra)} common {period}ly returns, fewer than the {min_returns} a reading needs",
            len(ra),
        )
    x, y = np.asarray(ra), np.asarray(rb)
    if float(np.std(x)) == 0.0 or float(np.std(y)) == 0.0:
        return CorrelationReading.unmeasured("a series did not move over the window", len(ra))
    value = float(np.corrcoef(x, y)[0, 1])
    return CorrelationReading(value, len(ra), a[keys[0]][0], a[keys[-1]][0], None, period)
