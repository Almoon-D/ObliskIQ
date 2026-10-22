"""Tracking and partner correlations: the computation on hand-made series, and the research
store's reading in the base currency, point in time (Session 9; DECISIONS D64, D121).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest

from obliskiq.config import ObliskConfig
from obliskiq.domain.research import Frequency, Observation, PointInTime, ResearchSeries, Vendor
from obliskiq.quant.tracking import CorrelationReading, return_correlation
from obliskiq.research.data import universe_series_id
from obliskiq.research.tracking import ResearchCorrelations, pair_readings, tracking_readings
from obliskiq.store import Store
from obliskiq.store.research import replace_research_series

NOW = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)
AS_OF = date(2026, 10, 22)


def month_ends(count: int, last: date = date(2026, 9, 30)) -> list[date]:
    days: list[date] = []
    year, month = last.year, last.month
    for _ in range(count):
        first_next = date(year + (month == 12), month % 12 + 1, 1)
        days.append(first_next - timedelta(days=1))
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return sorted(days)


def levels(returns: list[float], start: float = 100.0) -> list[float]:
    out = [start]
    for r in returns:
        out.append(out[-1] * math.exp(r))
    return out


# ── the computation ──


def test_the_correlation_of_monthly_log_returns_is_hand_computed() -> None:
    a = [0.01, -0.02, 0.03, 0.00, 0.015, -0.01]
    b = [0.012, -0.018, 0.025, 0.004, 0.01, -0.012]
    days = month_ends(7)
    first = list(zip(days, levels(a), strict=True))
    second = list(zip(days, levels(b, 50.0), strict=True))
    reading = return_correlation(first, second, period="month", min_returns=6)
    assert reading.measured
    assert reading.value == pytest.approx(float(np.corrcoef(a, b)[0, 1]))
    assert (reading.returns, reading.first, reading.last) == (6, days[0], days[-1])
    assert "on 6 monthly returns" in reading.describe()


def test_a_missing_period_never_stretches_a_return(shipped_settings: ObliskConfig) -> None:
    days = month_ends(5)
    first = [(d, 100.0 + i) for i, d in enumerate(days)]
    second = [(d, 100.0 + i * i) for i, d in enumerate(days) if i != 2]  # month 2 missing
    reading = return_correlation(first, second, period="month", min_returns=2)
    assert reading.returns == 2  # 0->1 and 3->4; 1->3 spans two months and is skipped


def test_the_last_value_of_a_period_is_its_value() -> None:
    week = [(date(2026, 10, 5), 100.0), (date(2026, 10, 9), 101.0)]
    later = [(date(2026, 10, 16), 102.0), (date(2026, 10, 23), 103.0)]
    series = week + later
    other = [(date(2026, 10, 9), 10.0), (date(2026, 10, 16), 10.3), (date(2026, 10, 23), 10.1)]
    reading = return_correlation(series, other, period="week", min_returns=2)
    expected = np.corrcoef(
        [math.log(102 / 101), math.log(103 / 102)], [math.log(10.3 / 10), math.log(10.1 / 10.3)]
    )[0, 1]
    assert reading.value == pytest.approx(expected)
    assert "weekly returns" in reading.describe()


def test_iso_weeks_run_across_the_year_end() -> None:
    """2026 has 53 ISO weeks: week 53 is followed by week 1 of 2027, a week later."""
    days = [date(2026, 12, 25), date(2027, 1, 1), date(2027, 1, 8)]
    a = [(d, v) for d, v in zip(days, (100.0, 101.0, 99.0), strict=True)]
    b = [(d, v) for d, v in zip(days, (50.0, 50.4, 49.9), strict=True)]
    reading = return_correlation(a, b, period="week", min_returns=2)
    assert reading.returns == 2
    gap = [(date(2026, 12, 18), 100.0), (date(2027, 1, 1), 101.0), (date(2027, 1, 8), 99.0)]
    reading = return_correlation(gap, gap, period="week", min_returns=1)
    assert reading.returns == 1  # week 51 to week 53 is two weeks apart


def test_too_little_history_or_no_movement_is_no_reading() -> None:
    days = month_ends(4)
    flat = [(d, 100.0) for d in days]
    moving = [(d, 100.0 + i) for i, d in enumerate(days)]
    few = return_correlation(moving, moving, period="month", min_returns=36)
    assert not few.measured
    assert "3 common monthly returns, fewer than the 36" in (few.reason or "")
    still = return_correlation(flat, moving, period="month", min_returns=2)
    assert still.reason == "a series did not move over the window"
    none = return_correlation([], moving, period="month", min_returns=2)
    assert none.reason == "0 common month(s) of prices"
    bad = [(d, float("nan") if i == 1 else -1.0 if i == 2 else 100.0) for i, d in enumerate(days)]
    assert return_correlation(bad, moving, period="month", min_returns=1).value is None
    assert CorrelationReading.unmeasured("x").describe() == "unmeasured: x"


# ── the research store, point in time ──


def put(
    store: Store, series_id: str, points: list[tuple[date, float]], currency: str | None = "EUR"
) -> None:
    dataset, _, field = series_id.partition(".")
    series = ResearchSeries(
        series_id=series_id, dataset_id=dataset, vendor=Vendor.YAHOO, field=field, units="price",
        currency=currency, frequency=Frequency.DAILY, point_in_time=PointInTime.AS_OBSERVED,
        source_url="x", downloaded_at=NOW, content_sha256="a" * 64, bias_notes=("x",),
    )  # fmt: skip
    rows = [Observation(observed_on=d, available_on=d, value=v) for d, v in points]
    replace_research_series(store, series, rows)


def correlated(count: int) -> tuple[list[date], list[float], list[float]]:
    rng = np.random.default_rng(9)
    common = rng.normal(0, 0.03, count)
    a = list(common + rng.normal(0, 0.002, count))
    b = list(common + rng.normal(0, 0.002, count))
    return month_ends(count + 1, date(2026, 9, 30)), a, b


def test_two_series_correlate_in_the_base_currency(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    days, a, b = correlated(48)
    put(store, "universe_aaa.adj_close", list(zip(days, levels(a), strict=True)))
    put(store, "universe_bbb.adj_close", list(zip(days, levels(b), strict=True)), "USD")
    put(store, "ecb_fx.USD", [(d, 1.10) for d in days], "USD")
    readings = ResearchCorrelations(store, shipped_settings, as_of=AS_OF)
    found = readings.correlation("universe_aaa.adj_close", "universe_bbb.adj_close")
    assert found.value is not None
    assert found.value > 0.95  # a constant rate changes no return
    assert found.returns == 48


def test_a_reading_ignores_what_was_not_public_on_its_date(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    days, a, b = correlated(48)
    first = list(zip(days, levels(a), strict=True))
    second = list(zip(days, levels(b), strict=True))
    put(store, "universe_aaa.adj_close", first)
    put(store, "universe_bbb.adj_close", second)
    as_of = days[-6]
    before = ResearchCorrelations(store, shipped_settings, as_of=as_of).correlation(
        "universe_aaa.adj_close", "universe_bbb.adj_close"
    )
    later = [(d, v * (3.0 if d > as_of else 1.0)) for d, v in second]
    put(store, "universe_bbb.adj_close", [*later, (AS_OF, 1.0)])
    after = ResearchCorrelations(store, shipped_settings, as_of=as_of).correlation(
        "universe_aaa.adj_close", "universe_bbb.adj_close"
    )
    assert after == before


def test_the_window_bounds_the_history(store: Store, shipped_settings: ObliskConfig) -> None:
    lookback = shipped_settings.vehicles.correlation.lookback_days
    days, a, b = correlated(120)
    put(store, "universe_aaa.adj_close", list(zip(days, levels(a), strict=True)))
    put(store, "universe_bbb.adj_close", list(zip(days, levels(b), strict=True)))
    found = ResearchCorrelations(store, shipped_settings, as_of=AS_OF).correlation(
        "universe_aaa.adj_close", "universe_bbb.adj_close"
    )
    assert found.first is not None
    assert found.first >= AS_OF - timedelta(days=lookback)


def test_a_series_that_cannot_be_read_says_why(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    days, a, _ = correlated(48)
    put(store, "universe_aaa.adj_close", list(zip(days, levels(a), strict=True)))
    put(store, "universe_ccc.adj_close", list(zip(days, levels(a), strict=True)), None)
    put(store, "universe_ddd.adj_close", list(zip(days, levels(a), strict=True)), "GBP")
    put(store, "universe_old.adj_close", [(date(2001, 1, 31), 1.0), (date(2001, 2, 28), 1.1)])
    readings = ResearchCorrelations(store, shipped_settings, as_of=AS_OF)
    missing = readings.correlation("universe_aaa.adj_close", "universe_zzz.adj_close")
    assert missing.reason == "universe_zzz.adj_close is not in the research store"
    first_missing = readings.correlation("universe_zzz.adj_close", "universe_aaa.adj_close")
    assert first_missing.reason == "universe_zzz.adj_close is not in the research store"
    no_currency = readings.correlation("universe_aaa.adj_close", "universe_ccc.adj_close")
    assert no_currency.reason == "universe_ccc.adj_close states no currency to convert"
    no_rate = readings.correlation("universe_aaa.adj_close", "universe_ddd.adj_close")
    assert "no day with a fresh ECB rate for GBP" in (no_rate.reason or "")
    stale = readings.correlation("universe_aaa.adj_close", "universe_old.adj_close")
    assert "holds no value from" in (stale.reason or "")


def test_tracking_and_pair_readings_by_symbol(store: Store, shipped_settings: ObliskConfig) -> None:
    days, a, b = correlated(48)
    put(store, universe_series_id("AAA"), list(zip(days, levels(a), strict=True)))
    put(store, universe_series_id("BBB"), list(zip(days, levels(b), strict=True)))
    put(store, "index_x.level", list(zip(days, levels(a), strict=True)))
    readings = ResearchCorrelations(store, shipped_settings, as_of=AS_OF)
    series = {"AAA": universe_series_id("AAA"), "BBB": universe_series_id("BBB")}
    tracking = tracking_readings(readings, "index_x.level", series)
    assert tracking["AAA"].value == pytest.approx(1.0)
    unnamed = tracking_readings(readings, None, series)
    assert unnamed["BBB"].reason == "the exposure names no data series"
    pairs = pair_readings(readings, [("AAA", "BBB")], series)
    assert pairs[("AAA", "BBB")].value is not None
    assert readings.fx is not None
    assert universe_series_id("CSBGE7") == "universe_csbge7.adj_close"
