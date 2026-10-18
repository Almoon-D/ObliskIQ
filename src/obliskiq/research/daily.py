"""Daily closes inside a study's months (DECISIONS D88, D92, D93).

A study rebalances at month ends, but D88 measures a drawdown on daily closes and an estimator of
risk reads daily returns. This module holds the daily part:

- **The history**: each instrument's price in the base currency on every day of a calendar (the
  calendar instrument's days, MSCI's calculation days in Session 4e), each the latest price and
  reference rate public that day within the age limits, as log returns. The days from the first
  day asked through the first rebalance date are the **prior** (an estimator's warm-up reads
  them); the days after one rebalance date through the next are a **month**. A month's returns
  sum to the log change between its two month-end prices, the change the monthly bootstrap
  resamples, so the daily and the monthly views of a path agree at every month end.
- **A path**: the prior followed by months in order, the history's own (the historical run) or
  whole months resampled together, every instrument's days of a month as they happened (a
  bootstrap path). On a path a strategy reads the returns through a rebalance date and nothing
  later.
- **Daily marks**: the account's value on each day of a path, the units and cash held after a
  rebalance carried at each day's prices until the next one, and its drawdown statistics: the
  worst fall from a running peak, when each limit is first reached, the share of marks beyond it
  and the drawdown episodes (a fall from one peak until the next) that reach it.

A day before an instrument's first price is ``NaN`` in the prior; inside a month every
instrument must be priced, else the history refuses it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from typing import Final

import numpy as np

from ..errors import SimulationError
from .stats import years_between

#: A decade, for drawdown episodes per decade.
YEARS_PER_DECADE: Final = 10.0


@dataclass(frozen=True, slots=True)
class DailyPath:
    """Daily log returns in the base currency (rows: days, oldest first; columns: ``keys``) and
    the rows through each rebalance date of ``dates``."""

    keys: tuple[str, ...]
    dates: tuple[date, ...]
    returns: np.ndarray
    ends: tuple[int, ...]

    def __post_init__(self) -> None:
        if len(self.ends) != len(self.dates):
            raise ValueError("a daily path needs the rows through every rebalance date")
        if self.returns.shape != (self.ends[-1] if self.ends else 0, len(self.keys)):
            raise ValueError("a daily path's returns do not match its keys and rows")

    def column(self, key: str) -> int:
        try:
            return self.keys.index(key)
        except ValueError:
            raise SimulationError(f"{key} has no daily history") from None

    def rows_through(self, on: date) -> int:
        """The rows public on rebalance date ``on``: every day through it."""
        try:
            return self.ends[self.dates.index(on)]
        except ValueError:
            raise SimulationError(f"{on} is not a rebalance date of the daily path") from None

    def month(self, index: int) -> np.ndarray:
        """The returns of the days after rebalance date ``index`` through the next."""
        return self.returns[self.ends[index] : self.ends[index + 1]]


@dataclass(frozen=True, slots=True)
class DailyHistory:
    """A study's daily history: the prior's returns and each month's (the module docstring)."""

    keys: tuple[str, ...]
    dates: tuple[date, ...]
    prior: np.ndarray
    months: tuple[np.ndarray, ...]

    def __post_init__(self) -> None:
        if len(self.months) != len(self.dates) - 1:
            raise ValueError("a daily history holds one month between each two rebalance dates")
        for index, month in enumerate(self.months):
            if month.shape[0] < 1 or np.isnan(month).any():
                raise SimulationError(
                    f"a price is missing on a day between {self.dates[index]} and "
                    f"{self.dates[index + 1]}"
                )

    def path(self, order: Sequence[int] | np.ndarray | None = None) -> DailyPath:
        """The prior then the months in ``order`` (indices into the months; ``None``: their own
        order), dated by the rebalance dates."""
        chosen = range(len(self.months)) if order is None else order
        if len(chosen) != len(self.months):
            raise ValueError("a path holds as many months as the history")
        blocks = [self.prior, *(self.months[int(k)] for k in chosen)]
        counts = np.cumsum([block.shape[0] for block in blocks])
        return DailyPath(
            self.keys,
            self.dates,
            np.concatenate(blocks, axis=0),
            tuple(int(count) for count in counts),
        )


type BasePrice = Callable[[str, date], float | None]


def daily_history(
    keys: Sequence[str],
    days: Sequence[date],
    dates: Sequence[date],
    price: BasePrice,
) -> DailyHistory:
    """The history of ``keys`` on ``days`` (sorted, the calendar's), split at the rebalance
    ``dates`` (each one of ``days``); ``price`` gives a key's base-currency price on a day, or
    ``None`` when none is fresh."""
    calendar = set(days)
    if not dates or any(day not in calendar for day in dates):
        raise SimulationError("every rebalance date must be a day of the daily calendar")
    grid = [day for day in days if day <= dates[-1]]
    levels = np.array(
        [[_positive(price(key, day)) for key in keys] for day in grid], dtype=float
    ).reshape(len(grid), len(keys))
    with np.errstate(invalid="ignore"):
        returns = np.diff(np.log(levels), axis=0)
    # Row r of returns is the change into grid[r + 1].
    position = {day: index for index, day in enumerate(grid)}
    cuts = [position[day] for day in dates]
    prior = returns[: cuts[0]]
    months = tuple(returns[first:last] for first, last in pairwise(cuts))
    return DailyHistory(tuple(keys), tuple(dates), prior, months)


def _positive(value: float | None) -> float:
    return math.nan if value is None or not value > 0 else value


# ── daily marks ──


@dataclass(frozen=True, slots=True)
class Holding:
    """What the account held after a rebalance date's trades and tax: cash, units and each held
    unit's base-currency price that day."""

    cash: float
    units: Mapping[str, float]
    prices: Mapping[str, float]


def daily_values(
    nav: Sequence[float],
    holdings: Sequence[Holding],
    path: DailyPath,
    *,
    days_per_year: float,
) -> tuple[np.ndarray, np.ndarray]:
    """The account's value on each day of ``path`` from its first rebalance date to its last,
    and each value's time in years from the first: between two dates the holdings after the
    first are carried at the day's prices, and each date's own value is ``nav``'s (after its
    trades and tax, the last before the terminal sale). A run whose holdings change between
    dates (a protective stop) is not marked this way."""
    if len(nav) != len(path.dates) or len(holdings) != len(path.dates) - 1:
        raise ValueError("daily marks need a value at every date and holdings at each but the last")
    values: list[np.ndarray] = [np.array([nav[0]])]
    times: list[np.ndarray] = [np.array([0.0])]
    start = path.dates[0]
    for index, held in enumerate(holdings):
        month = path.month(index)
        inside = month[:-1]
        if inside.shape[0]:
            marked = np.full(inside.shape[0], held.cash)
            for key, units in held.units.items():
                growth = np.exp(np.cumsum(inside[:, path.column(key)]))
                marked += units * held.prices[key] * growth
            begin = years_between(start, path.dates[index], days_per_year=days_per_year)
            span = years_between(
                path.dates[index], path.dates[index + 1], days_per_year=days_per_year
            )
            steps = np.arange(1, month.shape[0]) / month.shape[0]
            values.append(marked)
            times.append(begin + steps * span)
        values.append(np.array([nav[index + 1]]))
        times.append(
            np.array([years_between(start, path.dates[index + 1], days_per_year=days_per_year)])
        )
    return np.concatenate(values), np.concatenate(times)


@dataclass(frozen=True, slots=True)
class DrawdownStats:
    """A path's drawdowns from its running peak (``limits`` are positive fractions):

    - ``worst``: the deepest fall (``-0.3`` is 30 % below the peak; ``0`` if none);
    - ``first_hit``: the years until each limit is first reached (``inf``: never);
    - ``time_beyond``: the share of marks at or beyond each limit;
    - ``episodes``: the falls from a peak until the next peak that reach each limit;
    - ``years``: the path's length.
    """

    worst: float
    first_hit: tuple[float, ...]
    time_beyond: tuple[float, ...]
    episodes: tuple[int, ...]
    years: float


def drawdown_stats(values: np.ndarray, times: np.ndarray, limits: Sequence[float]) -> DrawdownStats:
    """The drawdown statistics of a path of positive values at ``times`` (years, rising)."""
    series = np.asarray(values, dtype=float)
    when = np.asarray(times, dtype=float)
    if series.size == 0 or series.shape != when.shape or np.any(series <= 0):
        raise ValueError("drawdowns need positive values, each with its time")
    if any(not 0 < limit < 1 for limit in limits):
        raise ValueError("a drawdown limit is a fraction strictly between 0 and 1")
    peaks = np.maximum.accumulate(series)
    falls = series / peaks - 1.0
    # A new episode starts at each new peak; its depth is the deepest fall before the next.
    episode = np.cumsum(series >= peaks)
    depth = np.full(int(episode.max()) + 1, 0.0)
    np.minimum.at(depth, episode, falls)
    first, beyond, counts = [], [], []
    for limit in limits:
        reached = falls <= -limit
        first.append(float(when[np.argmax(reached)]) if reached.any() else math.inf)
        beyond.append(float(reached.mean()))
        counts.append(int((depth <= -limit).sum()))
    return DrawdownStats(
        worst=float(min(falls.min(), 0.0)),
        first_hit=tuple(first),
        time_beyond=tuple(beyond),
        episodes=tuple(counts),
        years=float(when[-1] - when[0]),
    )


def breach_within(first_hits: np.ndarray, horizon_years: float | None) -> float:
    """The share of paths whose drawdown reached a limit within ``horizon_years`` (``None``:
    anywhere on the path)."""
    hits = np.asarray(first_hits, dtype=float)
    if hits.size == 0:
        raise ValueError("a breach probability needs at least one path")
    if horizon_years is None:
        return float(np.mean(np.isfinite(hits)))
    return float(np.mean(hits <= horizon_years))


def episodes_per_decade(episodes: np.ndarray, years: np.ndarray) -> float:
    """The mean, over paths, of the drawdown episodes reaching a limit per ten years."""
    counts = np.asarray(episodes, dtype=float)
    spans = np.asarray(years, dtype=float)
    if counts.size == 0 or np.any(spans <= 0):
        raise ValueError("a rate needs paths of positive length")
    return float(np.mean(counts / spans) * YEARS_PER_DECADE)
