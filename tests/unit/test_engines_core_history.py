"""The tilt's history on the live book: month-end closes from the market store's bars in the base
currency, each at its own session's rate, a gap restarting the run, the evaluation month, and
the trend and its settledness over them.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date

import pytest
from core_rig import eurusd, month_ends, path, seed_closes, seed_vehicle, weekdays

from obliskiq.config import ObliskConfig
from obliskiq.engines.core.history import (
    HistoryView,
    MonthEnds,
    VehicleHistory,
    last_month_end,
    read_month_ends,
)
from obliskiq.quant.trend import TrendRule
from obliskiq.store import Store, repos

RULE = TrendRule(momentum_months=12, average_months=10, hysteresis=0.02, floor=0.25)
FUND, DOLLAR_FUND = 501, 502


def test_the_tilt_reads_the_last_month_end_before_the_day() -> None:
    assert last_month_end(date(2026, 10, 25)) == date(2026, 9, 30)
    assert last_month_end(date(2026, 10, 1)) == date(2026, 9, 30)
    assert last_month_end(date(2026, 3, 31)) == date(2026, 2, 28)
    assert last_month_end(date(2027, 1, 5)) == date(2026, 12, 31)


def test_month_end_closes_are_the_last_bar_of_each_month_in_the_base_currency(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_vehicle(store, FUND, "FUND")
    seed_vehicle(store, DOLLAR_FUND, "USDF", "USD")
    days = weekdays(date(2025, 1, 2), date(2026, 9, 30))
    seed_closes(store, FUND, {day: 100.0 + i for i, day in enumerate(days)})
    seed_closes(store, DOLLAR_FUND, dict.fromkeys(days, 125.0))
    repos.upsert_macro(store, [eurusd(day, 1.25) for day in days])
    read = read_month_ends(
        store,
        shipped_settings,
        {"f": VehicleHistory(FUND, "EUR"), "d": VehicleHistory(DOLLAR_FUND, "USD")},
        as_of=date(2026, 9, 30),
    )
    fund = read["f"]
    assert fund.note is None
    assert [day for day, _ in fund.closes] == month_ends(date(2026, 9, 30), 21)
    # January 2025's last session is the 31st, its 22nd weekday from the 2nd (the 21st after).
    assert fund.closes[0] == (date(2025, 1, 31), 100.0 + 21)
    assert read["d"].closes[-1] == (date(2026, 9, 30), 100.0)  # 125 dollars at 1.25


def test_a_month_without_a_fresh_close_or_rate_restarts_the_run(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    seed_vehicle(store, FUND, "FUND")
    seed_vehicle(store, DOLLAR_FUND, "USDF", "USD")
    before = weekdays(date(2025, 1, 2), date(2025, 5, 15))
    after = weekdays(date(2025, 7, 1), date(2026, 9, 30))
    seed_closes(store, FUND, dict.fromkeys(before + after, 100.0))
    seed_closes(store, DOLLAR_FUND, dict.fromkeys(before + after, 100.0))
    repos.upsert_macro(store, [eurusd(day, 1.25) for day in after if day < date(2026, 3, 1)])
    read = read_month_ends(
        store,
        shipped_settings,
        {"f": VehicleHistory(FUND, "EUR"), "d": VehicleHistory(DOLLAR_FUND, "USD")},
        as_of=date(2026, 9, 30),
    )
    fund = read["f"]
    assert fund.closes[0][0] == date(2025, 7, 31)  # May's last close was the 15th: a gap
    assert fund.note is not None
    assert "within 7 days of 2025-06-30" in fund.note
    dollars = read["d"]
    assert dollars.closes == ()  # no rate after February 2026: every month since is a gap
    assert dollars.note is not None
    assert "EURUSD" in dollars.note


def test_a_vehicle_without_bars_has_no_history(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    read = read_month_ends(
        store, shipped_settings, {"x": VehicleHistory(FUND, "EUR")}, as_of=date(2026, 9, 30)
    )
    assert read["x"] == MonthEnds("x", (), f"conid {FUND} has no stored daily bar")


def test_the_view_reads_the_trend_on_shared_months_and_says_when_it_is_settled() -> None:
    month = date(2026, 9, 30)
    rising = path(100, 0.01, 24)
    cash = path(100, 0.001, 24)
    view = HistoryView(
        {"a": MonthEnds("a", rising.closes, None), "c": MonthEnds("c", cash.closes, None)}, month
    )
    state = view.trend("a", "c", RULE)
    assert state is not None
    assert state.scale == 1
    assert view.months("a", "c") == 24
    assert view.settled("a", "c", RULE)
    assert view.trend("a", "c", RULE) is state  # read once
    # A flat history never crosses the buffer after its first month: unsettled.
    flat = HistoryView({"a": MonthEnds("a", path(100, 0.0, 24).closes, None), "c": cash}, month)
    assert not flat.settled("a", "c", RULE)
    assert not view.settled("a", "missing", RULE)
    assert view.closes("missing").note == "missing was not read"
    aliased = HistoryView(view.history, month, {"primary": "a"})
    assert aliased.trend("primary", "c", RULE) == state
    assert aliased.as_of == month


@pytest.mark.parametrize("months", [12, 13])
def test_the_trend_needs_its_month_ends(months: int) -> None:
    month = date(2026, 9, 30)
    view = HistoryView(
        {
            "a": MonthEnds("a", path(100, 0.01, months).closes, None),
            "c": MonthEnds("c", path(100, 0.001, months).closes, None),
        },
        month,
    )
    assert (view.trend("a", "c", RULE) is None) == (months < RULE.months_needed)
