"""research/daily.py: the daily history on a calendar, paths of whole months, daily marks of an
account and drawdown statistics, held to hand-computed values (DECISIONS D88, D92).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from datetime import date

import numpy as np
import pytest

from obliskiq.errors import SimulationError
from obliskiq.research.daily import (
    DailyHistory,
    DailyPath,
    Holding,
    breach_within,
    daily_history,
    daily_values,
    drawdown_stats,
    episodes_per_decade,
)

DAYS = [
    date(2024, 1, 29), date(2024, 1, 30), date(2024, 1, 31),
    date(2024, 2, 1), date(2024, 2, 2), date(2024, 2, 29),
    date(2024, 3, 1), date(2024, 3, 29),
]  # fmt: skip
DATES = [date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 29)]
PRICES = {
    "A": [100.0, 101.0, 102.0, 100.0, 104.0, 105.0, 99.0, 110.0],
    "B": [None, None, 50.0, 51.0, 52.0, 53.0, 54.0, 55.0],
}


def price(key: str, day: date) -> float | None:
    return PRICES[key][DAYS.index(day)]


def test_the_history_splits_at_the_rebalance_dates_and_months_sum_to_month_end_changes():
    history = daily_history(["A", "B"], DAYS, DATES, price)
    assert history.prior.shape == (2, 2)
    assert np.isnan(history.prior[:, 1]).all()  # B has no price before the first date
    assert history.prior[:, 0] == pytest.approx([math.log(1.01), math.log(102 / 101)])
    february, march = history.months
    assert february.shape == (3, 2)
    assert march.shape == (2, 2)
    assert february[:, 0].sum() == pytest.approx(math.log(105 / 102))
    assert march[:, 1].sum() == pytest.approx(math.log(55 / 53))


def test_a_path_is_the_prior_then_the_months_in_order():
    history = daily_history(["A", "B"], DAYS, DATES, price)
    own = history.path()
    assert own.ends == (2, 5, 7)
    assert own.rows_through(DATES[1]) == 5
    swapped = history.path([1, 0])
    assert swapped.month(0) == pytest.approx(history.months[1], nan_ok=True)
    assert swapped.month(1) == pytest.approx(history.months[0], nan_ok=True)
    assert swapped.column("B") == 1
    with pytest.raises(SimulationError, match="C has no daily history"):
        own.column("C")
    with pytest.raises(SimulationError, match="not a rebalance date"):
        own.rows_through(date(2024, 2, 1))
    with pytest.raises(ValueError, match="as many months"):
        history.path([0])


def test_a_missing_price_inside_a_month_or_a_misplaced_calendar_is_refused():
    gaps = {**PRICES, "B": [None, None, 50.0, None, 52.0, 53.0, 54.0, 55.0]}
    with pytest.raises(SimulationError, match="price is missing on a day between 2024-01-31"):
        daily_history(["B"], DAYS, DATES, lambda key, day: gaps[key][DAYS.index(day)])
    with pytest.raises(SimulationError, match="every rebalance date must be a day"):
        daily_history(["A"], DAYS, [date(2024, 1, 30), date(2024, 2, 28)], price)
    with pytest.raises(SimulationError, match="every rebalance date"):
        daily_history(["A"], DAYS, [], price)
    with pytest.raises(ValueError, match="one month between each two"):
        DailyHistory(("A",), tuple(DATES), np.zeros((0, 1)), (np.zeros((1, 1)),))
    with pytest.raises(ValueError, match="rows through every rebalance date"):
        DailyPath(("A",), tuple(DATES), np.zeros((2, 1)), (1, 2))
    with pytest.raises(ValueError, match="do not match its keys"):
        DailyPath(("A",), tuple(DATES[:2]), np.zeros((2, 1)), (1, 3))


def test_daily_marks_carry_the_holdings_at_each_days_prices():
    history = daily_history(["A", "B"], DAYS, DATES, price)
    path = history.path()
    held = [
        Holding(cash=10.0, units={"A": 1.0}, prices={"A": 102.0}),
        Holding(cash=5.0, units={"A": 1.0, "B": 2.0}, prices={"A": 105.0, "B": 53.0}),
    ]
    values, times = daily_values([112.0, 115.0, 175.0], held, path, days_per_year=365.25)
    # February: 1 Feb (A 100) and 2 Feb (A 104), then the date's own value; March: 1 Mar.
    assert values.tolist() == pytest.approx([112.0, 110.0, 114.0, 115.0, 5 + 99 + 108, 175.0])
    span = (DATES[1] - DATES[0]).days / 365.25
    assert times[1] == pytest.approx(span / 3)
    assert times[3] == pytest.approx(span)
    assert times[-1] == pytest.approx((DATES[2] - DATES[0]).days / 365.25)
    with pytest.raises(ValueError, match="a value at every date"):
        daily_values([1.0], held, path, days_per_year=365.25)


def test_a_month_of_one_day_has_no_mark_inside_it():
    path = DailyPath(("A",), (DATES[0], DATES[1]), np.array([[0.0], [0.1]]), (1, 2))
    held = [Holding(cash=0.0, units={"A": 1.0}, prices={"A": 10.0})]
    values, times = daily_values([10.0, 11.0], held, path, days_per_year=365.25)
    assert values.tolist() == [10.0, 11.0]
    assert len(times) == 2


def test_drawdown_statistics_by_hand():
    values = np.array([100, 89, 69, 80, 101, 70, 102, 110], dtype=float)
    times = np.arange(8, dtype=float)
    found = drawdown_stats(values, times, [0.1, 0.3])
    assert found.worst == pytest.approx(-0.31)
    # 10 % is first reached at 89 (time 1); 30 % at 69 (time 2).
    assert found.first_hit == (1.0, 2.0)
    assert found.time_beyond == pytest.approx((4 / 8, 2 / 8))
    # Two falls from a peak reach 10 % (100 -> 69 and 101 -> 70); both reach 30 %.
    assert found.episodes == (2, 2)
    assert found.years == 7.0
    never = drawdown_stats(np.array([1.0, 2.0, 3.0]), np.arange(3.0), [0.1])
    assert (never.worst, never.first_hit, never.episodes) == (0.0, (math.inf,), (0,))
    with pytest.raises(ValueError, match="positive values"):
        drawdown_stats(np.array([1.0, 0.0]), np.arange(2.0), [0.1])
    with pytest.raises(ValueError, match="positive values"):
        drawdown_stats(np.array([1.0, 2.0]), np.arange(3.0), [0.1])
    with pytest.raises(ValueError, match="strictly between 0 and 1"):
        drawdown_stats(np.array([1.0]), np.zeros(1), [1.0])


def test_breaches_within_a_horizon_and_episodes_per_decade():
    hits = np.array([1.0, 5.0, math.inf, 19.9])
    assert breach_within(hits, 2.0) == 0.25
    assert breach_within(hits, 20.0) == 0.75
    assert breach_within(hits, None) == 0.75
    assert episodes_per_decade(np.array([1, 3]), np.array([20.0, 20.0])) == pytest.approx(1.0)
    with pytest.raises(ValueError, match="at least one path"):
        breach_within(np.array([]), 1.0)
    with pytest.raises(ValueError, match="positive length"):
        episodes_per_decade(np.array([1]), np.array([0.0]))
