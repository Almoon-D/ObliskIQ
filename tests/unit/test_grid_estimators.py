"""The range estimators and the non-overlapping rolling readings (``grid/estimators.py``).

Ported from lite's ``tests/test_collect_vol.py`` (the range-estimator half): Parkinson and
Garman-Klass are annualised on one basis, need one row fewer than close-to-close, drop a bar
without splicing, refuse a corrupt or rangeless window, and Garman-Klass's open is tested before
it is trusted.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math

import pytest

from obliskiq.data.market.vol import realised_vol
from obliskiq.grid import estimators
from obliskiq.grid.estimators import (
    close_to_close,
    garman_klass,
    ohlc_bars,
    parkinson,
    range_bars,
    restated_open_share,
    rolling,
    rolling_close_to_close,
)

BASIS = 252.0


def walk(count: int, *, step: float, start: float = 100.0) -> list[float]:
    """Closes that alternate up and down by ``step`` in log terms."""
    return [start * math.exp(step * ((-1) ** i)) for i in range(count)]


def ohlc_walk(
    count: int, *, step: float, range_step: float, start: float = 100.0
) -> tuple[list[float], list[float], list[float], list[float]]:
    """Bars with a genuine open (never the previous close) and a real range."""
    closes = walk(count, step=step, start=start)
    opens = [close * math.exp(-step / 2) for close in closes]
    highs = [max(o, c) * math.exp(range_step) for o, c in zip(opens, closes, strict=True)]
    lows = [min(o, c) * math.exp(-range_step) for o, c in zip(opens, closes, strict=True)]
    return opens, highs, lows, closes


def parkinson_over(highs: list[float], lows: list[float], tenor: int, basis: float) -> float | None:
    bars = range_bars(highs, lows)
    return None if len(bars) < tenor else parkinson(bars[-tenor:], trading_days_per_year=basis)


def test_parkinson_is_annualised_on_the_configured_trading_year() -> None:
    _, highs, lows, _ = ohlc_walk(60, step=0.01, range_step=0.005)
    daily = parkinson_over(highs, lows, 30, 1.0)
    annual = parkinson_over(highs, lows, 30, BASIS)
    assert daily is not None
    assert annual == pytest.approx(daily * math.sqrt(BASIS))


def test_a_range_estimator_needs_one_fewer_row_than_close_to_close() -> None:
    _, highs, lows, closes = ohlc_walk(30, step=0.01, range_step=0.005)
    assert close_to_close(closes, 30, trading_days_per_year=BASIS) is None
    assert parkinson_over(highs, lows, 30, BASIS) is not None
    assert parkinson_over(highs, lows, 31, BASIS) is None


def test_a_bar_with_no_high_or_low_is_dropped_rather_than_truncating_the_series() -> None:
    _, highs, lows, _ = ohlc_walk(40, step=0.01, range_step=0.005)
    holed_highs: list[float | None] = list(highs)
    holed_lows: list[float | None] = list(lows)
    holed_highs[5] = None
    holed_lows[9] = None
    assert len(range_bars(holed_highs, holed_lows)) == len(highs) - 2


def test_an_inverted_or_non_positive_bar_is_refused() -> None:
    assert range_bars([101.0, 100.0, 101.0], [100.0, 101.0, 0.0]) == [(101.0, 100.0)]


def test_a_window_with_no_range_at_all_has_no_measurable_vol() -> None:
    flat = [100.0] * 40
    assert parkinson_over(flat, flat, 30, BASIS) is None


def test_one_zero_range_bar_is_kept_because_a_halted_session_really_had_none() -> None:
    _, highs, lows, _ = ohlc_walk(40, step=0.01, range_step=0.005)
    with_halt = parkinson_over(
        [*highs[:20], 100.0, *highs[20:]], [*lows[:20], 100.0, *lows[20:]], 30, BASIS
    )
    without = parkinson_over(highs, lows, 30, BASIS)
    assert with_halt is not None
    assert without is not None
    assert with_halt < without


def test_a_close_or_open_outside_its_own_bar_is_a_corrupt_row() -> None:
    assert ohlc_bars([105.0], [101.0], [99.0], [100.0]) == []
    assert ohlc_bars([100.0], [101.0], [99.0], [95.0]) == []
    assert ohlc_bars([100.0], [101.0], [99.0], [100.5]) == [(100.0, 101.0, 99.0, 100.5)]


def test_restated_opens_are_measured_as_a_share_and_compared_exactly() -> None:
    _, highs, lows, closes = ohlc_walk(10, step=0.01, range_step=0.02)
    honest = [close * math.exp(-0.005) for close in closes]
    restated = [closes[0], *closes[:-1]]
    assert restated_open_share(ohlc_bars(honest, highs, lows, closes)) == 0.0
    assert restated_open_share(ohlc_bars(restated, highs, lows, closes)) == pytest.approx(1.0)
    mixed = [*honest[:5], closes[4], *honest[6:]]
    assert restated_open_share(ohlc_bars(mixed, highs, lows, closes)) == pytest.approx(1 / 9)


def test_a_share_needs_two_bars_to_have_a_gap_between_them() -> None:
    assert restated_open_share([]) is None
    assert restated_open_share(ohlc_bars([100.0], [101.0], [99.0], [100.0])) is None


def test_the_ordering_invariant_is_what_keeps_garman_klass_non_negative() -> None:
    worst = ohlc_bars([100.0], [130.0], [100.0], [130.0])
    assert garman_klass(worst, trading_days_per_year=BASIS) is not None
    # Bypassing the filter is the only way to a negative sum; the backstop refuses it.
    assert garman_klass([(100.0, 101.0, 100.0, 130.0)], trading_days_per_year=BASIS) is None


def test_garman_klass_refuses_a_window_with_no_range_in_it() -> None:
    flat = [100.0] * 40
    assert (
        garman_klass(ohlc_bars(flat, flat, flat, flat)[-30:], trading_days_per_year=BASIS) is None
    )


def test_a_restated_open_moves_garman_klass_without_looking_wrong() -> None:
    """Why the guard tests the open first: the number stays positive and plausible."""
    opens, highs, lows, closes = ohlc_walk(40, step=0.01, range_step=0.005)
    restated = [closes[0], *closes[:-1]]
    restated_highs = [max(o, c) * math.exp(0.005) for o, c in zip(restated, closes, strict=True)]
    restated_lows = [min(o, c) * math.exp(-0.005) for o, c in zip(restated, closes, strict=True)]
    restated_bars = ohlc_bars(restated, restated_highs, restated_lows, closes)
    assert len(restated_bars) == len(closes)
    honest = garman_klass(ohlc_bars(opens, highs, lows, closes)[-30:], trading_days_per_year=BASIS)
    moved = garman_klass(restated_bars[-30:], trading_days_per_year=BASIS)
    assert honest is not None
    assert moved is not None
    assert moved > 0
    assert abs(moved - honest) > honest / 2
    assert restated_open_share(restated_bars) == 1.0


def test_close_to_close_is_the_collectors_implementation() -> None:
    """One implementation: the strike band and the cell cannot disagree."""
    closes = walk(40, step=0.01)
    assert close_to_close(closes, 30, trading_days_per_year=BASIS) == realised_vol(
        closes, tenor_days=30, trading_days_per_year=BASIS
    )
    assert estimators.close_to_close(
        [*closes[:20], 0.0, -5.0, *closes[20:]], 30, trading_days_per_year=BASIS
    ) == pytest.approx(close_to_close(closes, 30, trading_days_per_year=BASIS))


# ── rolling readings ──


def test_rolling_readings_stepped_by_the_tenor_share_no_data() -> None:
    """Sixty values hold two non-overlapping thirty-item windows, not thirty-one readings."""
    values = list(range(60))
    spans: list[tuple[int, int]] = []

    def measure(window: object) -> float:
        items = list(window)  # type: ignore[call-overload]
        spans.append((items[0], items[-1]))
        return float(len(items))

    assert len(rolling(values, 30, measure, step=30)) == 2
    assert spans == [(0, 29), (30, 59)]
    assert len(rolling(values, 30, measure, step=1)) == 31


def test_the_last_window_always_ends_on_the_newest_value() -> None:
    values = list(range(50))
    ends: list[int] = []

    def measure(window: object) -> float:
        ends.append(list(window)[-1])  # type: ignore[call-overload]
        return 0.0

    rolling(values, 20, measure, step=20)
    assert ends[-1] == 49
    assert ends == [29, 49]


def test_a_declined_window_contributes_nothing_never_a_zero() -> None:
    assert rolling([1, 2, 3, 4], 2, lambda _: None, step=1) == []


def test_close_to_close_readings_step_by_the_tenor_and_share_a_boundary_close() -> None:
    """Windows of tenor + 1 closes stepped by the tenor: consecutive readings share one close
    and no return."""
    closes = walk(10 * 30 + 1, step=0.01)
    assert len(rolling_close_to_close(closes, 30, trading_days_per_year=BASIS)) == 10


def test_a_bar_whose_open_or_close_leaves_its_range_is_corrupt_not_wide() -> None:
    assert ohlc_bars([105.0, 100.0], [104.0, 104.0], [99.0, 99.0], [100.0, 106.0]) == []
    assert ohlc_bars([None, 100.0], [104.0, 99.0], [99.0, 104.0], [100.0, 100.0]) == []


def test_no_bars_is_no_range_estimate() -> None:
    assert parkinson([], trading_days_per_year=252) is None
    assert garman_klass([], trading_days_per_year=252) is None
