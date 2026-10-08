"""Volatility-scaled strike bounds and tenor filtering.

The band is the single most consequential number the chain collector computes: it
decides what gets recorded, and what is never recoverable afterwards. These pin
the two things most likely to go quietly wrong — the unit of tau, and whether the
band actually responds to volatility rather than being a percentage in disguise.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import math
from datetime import date

import pytest

from obliskiq.collect.strikes import (
    TenorWindow,
    diffusion_tau_years,
    month_span,
    nearest_strike,
    select_strikes,
    strike_bounds,
)

SESSION = date(2026, 1, 15)


def band(config, *, spot: float, sigma: float, dte: int):
    tau = diffusion_tau_years(
        dte,
        trading_days_per_year=config.require_float("data.trading_days_per_year"),
        calendar_days_per_year=config.require_float("numerics.bsm.day_count_basis"),
    )
    return strike_bounds(
        spot=spot, sigma=sigma, tau_years=tau, k=config.require_float("data.strike_bounds_sigma")
    )


# ── the unit of tau ──


def test_trading_day_tau_equals_calendar_tau_over_the_day_count_basis(config):
    """The equivalence the whole formula rests on.

    docs/COLLECTORS.md writes sqrt(T/252) with T in TRADING days. Converting calendar DTE
    at 252/365 makes that identical to dte/365. Getting this wrong is invisible:
    the band still looks plausible, it is just 20% too wide.
    """
    trading_days_per_year = config.require_float("data.trading_days_per_year")
    calendar_days_per_year = config.require_float("numerics.bsm.day_count_basis")
    for dte in (90, 120, 155, 200):
        tau = diffusion_tau_years(
            dte,
            trading_days_per_year=trading_days_per_year,
            calendar_days_per_year=calendar_days_per_year,
        )
        assert tau == pytest.approx(dte / calendar_days_per_year)

        # And the long way round: T trading days over 252 gives the same answer.
        trading_days = dte * (trading_days_per_year / calendar_days_per_year)
        assert tau == pytest.approx(trading_days / trading_days_per_year)


def test_feeding_calendar_days_straight_into_252_would_overstate_the_band(config):
    """The bug this function exists to make impossible.

    Recorded as a test rather than a comment because the wrong version produces a
    perfectly reasonable-looking number.
    """
    trading_days_per_year = config.require_float("data.trading_days_per_year")
    calendar_days_per_year = config.require_float("numerics.bsm.day_count_basis")
    dte = 120

    correct = diffusion_tau_years(
        dte,
        trading_days_per_year=trading_days_per_year,
        calendar_days_per_year=calendar_days_per_year,
    )
    naive = dte / trading_days_per_year  # the mistake

    assert naive > correct
    # Variance inflated by 365/252, so the band WIDTH by the square root of that.
    assert math.sqrt(naive / correct) == pytest.approx(
        math.sqrt(calendar_days_per_year / trading_days_per_year)
    )


# ── the band responds to volatility ──


def test_the_band_widens_for_a_high_sigma_name_and_narrows_for_a_low_one(config):
    """A fixed +/-25% is far too narrow for a 90%-IV name and far too wide for a
    27%-IV one. That is the entire reason the band is volatility-scaled."""
    low = band(config, spot=100.0, sigma=0.27, dte=120)
    high = band(config, spot=100.0, sigma=0.90, dte=120)

    assert high.upper > low.upper
    assert high.lower < low.lower
    assert (high.upper - high.lower) > 2 * (low.upper - low.lower)


def test_the_band_is_never_a_fixed_percentage_of_spot(config):
    """Two names at the same spot and different vols must not get the same band."""
    widths = {
        sigma: (b.upper - b.lower) / b.spot
        for sigma in (0.15, 0.30, 0.60, 0.90)
        for b in [band(config, spot=100.0, sigma=sigma, dte=120)]
    }
    assert len(set(widths.values())) == len(widths)
    assert sorted(widths.values()) == list(widths.values())


def test_the_band_widens_with_time_to_expiry(config):
    """Even on the fixed-sigma fallback rung, a longer expiry gets a wider band."""
    near = band(config, spot=100.0, sigma=0.35, dte=95)
    far = band(config, spot=100.0, sigma=0.35, dte=200)
    assert far.upper > near.upper
    assert far.lower < near.lower


def test_the_band_is_log_symmetric_around_spot(config):
    """exp(+x) and exp(-x): the ratio to spot is the same on both sides, which is
    what a lognormal terminal distribution actually implies."""
    b = band(config, spot=100.0, sigma=0.40, dte=120)
    assert (b.upper / b.spot) == pytest.approx(b.spot / b.lower)
    assert b.lower < b.spot < b.upper


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_a_non_positive_spot_or_sigma_is_refused(bad):
    with pytest.raises(ValueError):
        strike_bounds(spot=bad, sigma=0.3, tau_years=0.3, k=2.0)
    with pytest.raises(ValueError):
        strike_bounds(spot=100.0, sigma=bad, tau_years=0.3, k=2.0)


# ── strike selection ──


def test_only_strikes_inside_the_band_are_selected(config):
    b = band(config, spot=100.0, sigma=0.27, dte=120)
    ladder = [50.0, 70.0, 80.0, 100.0, 120.0, 140.0, 200.0]
    selected = select_strikes(ladder, b, cap=0)
    assert all(b.lower <= s <= b.upper for s in selected)
    assert 200.0 not in selected and 50.0 not in selected


def test_the_cap_keeps_the_strikes_nearest_spot(config):
    """When the cap bites, the wings are what a truncated chain can afford to lose:
    the near strikes carry the volume, the open interest and the useful smile."""
    b = band(config, spot=100.0, sigma=0.90, dte=120)
    ladder = [float(s) for s in range(40, 260, 5)]
    selected = select_strikes(ladder, b, cap=5)
    assert len(selected) == 5
    assert selected == sorted(selected)
    furthest = max(abs(s - b.spot) for s in selected)
    dropped = [s for s in ladder if b.contains(s) and s not in selected]
    assert all(abs(s - b.spot) >= furthest for s in dropped)


def test_a_cap_of_zero_means_no_cap(config):
    b = band(config, spot=100.0, sigma=0.90, dte=120)
    ladder = [float(s) for s in range(40, 260, 5)]
    assert len(select_strikes(ladder, b, cap=0)) > 5


def test_nearest_strike_anchors_the_expiry_resolution():
    assert nearest_strike([90.0, 100.0, 110.0], 104.0) == 100.0
    assert nearest_strike([90.0, 100.0, 110.0], 106.0) == 110.0
    assert nearest_strike([], 100.0) is None


# ── the tenor window ──


def test_only_expiries_inside_the_configured_dte_window_survive(config):
    window = TenorWindow(config)
    # 90-200 DTE from 2026-01-15 is 2026-04-15 to 2026-08-03.
    expiries = ["2026-01-16", "2026-02-20", "2026-04-17", "2026-06-19", "2026-12-18"]
    assert window.expiries(expiries, SESSION) == ["2026-04-17", "2026-06-19"]


def test_an_expiry_exactly_on_each_boundary_is_included(config):
    from datetime import timedelta

    window = TenorWindow(config)
    edges = [
        (SESSION + timedelta(days=window.dte_min)).isoformat(),
        (SESSION + timedelta(days=window.dte_max)).isoformat(),
    ]
    assert window.expiries(edges, SESSION) == sorted(edges)


def test_an_expiry_one_day_outside_either_boundary_is_excluded(config):
    from datetime import timedelta

    window = TenorWindow(config)
    outside = [
        (SESSION + timedelta(days=window.dte_min - 1)).isoformat(),
        (SESSION + timedelta(days=window.dte_max + 1)).isoformat(),
    ]
    assert window.expiries(outside, SESSION) == []


def test_option_months_are_narrowed_before_any_strike_call(config):
    """Collecting the whole chain instead of the 2-4 in-window expiries is roughly
    a tenfold volume increase for data the mandate cannot trade."""
    window = TenorWindow(config)
    kept = window.months(("JAN26", "FEB26", "MAR26", "APR26", "JUN26", "DEC26"), SESSION)
    assert kept == ["APR26", "JUN26"]


def test_an_unparseable_month_is_skipped_not_guessed(config):
    window = TenorWindow(config)
    assert window.months(("NOTAMONTH", "APR26", "13XX"), SESSION) == ["APR26"]


def test_expiries_are_deduplicated_and_unparseable_dates_dropped(config):
    window = TenorWindow(config)
    assert window.expiries(["2026-04-17", "20260417", "not-a-date", ""], SESSION) == ["2026-04-17"]


@pytest.mark.parametrize(
    ("month", "first", "last"),
    [
        ("JAN26", date(2026, 1, 1), date(2026, 1, 31)),
        ("FEB26", date(2026, 2, 1), date(2026, 2, 28)),
        ("DEC26", date(2026, 12, 1), date(2026, 12, 31)),
        ("FEB28", date(2028, 2, 1), date(2028, 2, 29)),  # leap year, derived not hardcoded
    ],
)
def test_month_span_covers_the_whole_month(month, first, last):
    assert month_span(month) == (first, last)


@pytest.mark.parametrize("bad", ["", "JAN", "XXX26", "JANXX", "1"])
def test_an_unparseable_month_span_is_none(bad):
    assert month_span(bad) is None
