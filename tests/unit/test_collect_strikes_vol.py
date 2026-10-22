"""The strike band, the tenor window, realised volatility and the sigma ladder.

Ported from lite's ``tests/test_collect_strikes.py`` and the close-to-close and ladder halves of
``tests/test_collect_vol.py``. The band decides what is recorded and what is never recoverable
afterwards, so these pin what is most likely to go quietly wrong: the unit of tau, whether the
band responds to volatility, and that a fallback is never lost.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from datetime import date, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from obliskiq.config import ObliskConfig
from obliskiq.config.schema_collectors import ChainCollectorConfig, CollectorsConfig
from obliskiq.data.market.strikes import (
    StrikeBounds,
    TenorWindow,
    diffusion_tau_years,
    month_span,
    nearest_strike,
    select_strikes,
    strike_bounds,
)
from obliskiq.data.market.vol import Sigma, SigmaLadder, realised_vol

SESSION = date(2026, 1, 15)


def band(settings: ObliskConfig, *, spot: float, sigma: float, dte: int) -> StrikeBounds:
    tau = diffusion_tau_years(
        dte,
        trading_days_per_year=settings.collectors.vol.trading_days_per_year,
        calendar_days_per_year=settings.numerics.bsm.day_count_basis,
    )
    k = settings.collectors.chain.strike_bounds_sigma
    return strike_bounds(spot=spot, sigma=sigma, tau_years=tau, k=k)


def walk(count: int, *, step: float, start: float = 100.0) -> list[float]:
    """Closes alternating up and down by ``step`` in log terms."""
    return [start * math.exp(step * ((-1) ** i)) for i in range(count)]


# ── the unit of tau ──


def test_trading_day_tau_equals_calendar_tau_over_the_day_count_basis(
    shipped_settings: ObliskConfig,
) -> None:
    per_year = shipped_settings.collectors.vol.trading_days_per_year
    basis = shipped_settings.numerics.bsm.day_count_basis
    for dte in (90, 120, 155, 200):
        tau = diffusion_tau_years(dte, trading_days_per_year=per_year, calendar_days_per_year=basis)
        assert tau == pytest.approx(dte / basis)
        assert tau == pytest.approx(dte * (per_year / basis) / per_year)
    assert (
        diffusion_tau_years(-3, trading_days_per_year=per_year, calendar_days_per_year=basis) == 0
    )


def test_feeding_calendar_days_straight_into_252_would_overstate_the_band(
    shipped_settings: ObliskConfig,
) -> None:
    per_year = shipped_settings.collectors.vol.trading_days_per_year
    basis = shipped_settings.numerics.bsm.day_count_basis
    correct = diffusion_tau_years(120, trading_days_per_year=per_year, calendar_days_per_year=basis)
    naive = 120 / per_year  # the mistake
    assert math.sqrt(naive / correct) == pytest.approx(math.sqrt(basis / per_year))


# ── the band responds to volatility and time ──


def test_the_band_widens_for_a_high_sigma_name_and_is_never_a_fixed_percentage(
    shipped_settings: ObliskConfig,
) -> None:
    low = band(shipped_settings, spot=100.0, sigma=0.27, dte=120)
    high = band(shipped_settings, spot=100.0, sigma=0.90, dte=120)
    assert high.upper - high.lower > 2 * (low.upper - low.lower)
    widths = [
        (b.upper - b.lower) / b.spot
        for b in (band(shipped_settings, spot=100.0, sigma=s, dte=120) for s in (0.15, 0.3, 0.6))
    ]
    assert widths == sorted(set(widths))


def test_the_band_widens_with_time_and_is_log_symmetric(shipped_settings: ObliskConfig) -> None:
    near = band(shipped_settings, spot=100.0, sigma=0.35, dte=95)
    far = band(shipped_settings, spot=100.0, sigma=0.35, dte=200)
    assert far.upper > near.upper
    assert far.lower < near.lower
    assert far.upper / far.spot == pytest.approx(far.spot / far.lower)


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_a_non_positive_spot_or_sigma_is_refused(bad: float) -> None:
    with pytest.raises(ValueError, match="spot"):
        strike_bounds(spot=bad, sigma=0.3, tau_years=0.3, k=2.0)
    with pytest.raises(ValueError, match="sigma"):
        strike_bounds(spot=100.0, sigma=bad, tau_years=0.3, k=2.0)


# ── strike selection ──


def test_only_strikes_inside_the_band_are_selected(shipped_settings: ObliskConfig) -> None:
    b = band(shipped_settings, spot=100.0, sigma=0.27, dte=120)
    selected = select_strikes([50.0, 70.0, 80.0, 100.0, 120.0, 140.0, 200.0], b, cap=60)
    assert selected
    assert all(b.contains(s) for s in selected)
    assert 50.0 not in selected
    assert 200.0 not in selected


def test_the_cap_keeps_the_strikes_nearest_spot(shipped_settings: ObliskConfig) -> None:
    b = band(shipped_settings, spot=100.0, sigma=0.90, dte=120)
    ladder = [float(s) for s in range(40, 260, 5)]
    selected = select_strikes(ladder, b, cap=5)
    assert len(selected) == 5
    assert selected == sorted(selected)
    furthest = max(abs(s - b.spot) for s in selected)
    assert all(abs(s - b.spot) >= furthest for s in ladder if b.contains(s) and s not in selected)


def test_nearest_strike_anchors_the_expiry_resolution() -> None:
    assert nearest_strike([90.0, 100.0, 110.0], 104.0) == 100.0
    assert nearest_strike([90.0, 100.0, 110.0], 106.0) == 110.0
    assert nearest_strike([], 100.0) is None


# ── the tenor window ──


def test_only_expiries_inside_the_window_survive_with_both_edges_included(
    shipped_settings: ObliskConfig,
) -> None:
    window = TenorWindow(shipped_settings.tenor)
    expiries = [date(2026, 1, 16), date(2026, 4, 17), date(2026, 6, 19), date(2026, 12, 18)]
    assert window.expiries([*expiries, date(2026, 4, 17)], SESSION) == expiries[1:3]
    edges = [SESSION + timedelta(days=window.dte_min), SESSION + timedelta(days=window.dte_max)]
    assert window.expiries(edges, SESSION) == edges
    outside = [edges[0] - timedelta(days=1), edges[1] + timedelta(days=1)]
    assert window.expiries(outside, SESSION) == []


def test_option_months_are_narrowed_before_any_strike_call(shipped_settings: ObliskConfig) -> None:
    window = TenorWindow(shipped_settings.tenor)
    months = ("JAN26", "FEB26", "MAR26", "APR26", "JUN26", "DEC26", "NOTAMONTH", "13XX")
    assert window.months(months, SESSION) == ["APR26", "JUN26"]


@pytest.mark.parametrize(
    ("month", "first", "last"),
    [
        ("JAN26", date(2026, 1, 1), date(2026, 1, 31)),
        ("FEB26", date(2026, 2, 1), date(2026, 2, 28)),
        ("dec26", date(2026, 12, 1), date(2026, 12, 31)),
        ("FEB28", date(2028, 2, 1), date(2028, 2, 29)),
    ],
)
def test_month_span_covers_the_whole_month(month: str, first: date, last: date) -> None:
    assert month_span(month) == (first, last)


@pytest.mark.parametrize("bad", ["", "JAN", "XXX26", "JANXX", "1", "JAN2026"])
def test_an_unparseable_month_span_is_none(bad: str) -> None:
    assert month_span(bad) is None


# ── realised vol ──


def test_realised_vol_is_annualised_and_needs_n_plus_one_closes() -> None:
    closes = walk(60, step=0.01)
    daily = realised_vol(closes, tenor_days=30, trading_days_per_year=1.0)
    annual = realised_vol(closes, tenor_days=30, trading_days_per_year=252.0)
    assert daily is not None
    assert annual == pytest.approx(daily * math.sqrt(252.0))
    assert realised_vol(walk(30, step=0.01), tenor_days=30, trading_days_per_year=252.0) is None
    assert realised_vol(walk(31, step=0.01), tenor_days=30, trading_days_per_year=252.0)


def test_only_the_recent_window_counts_and_a_flat_or_bad_series_measures_nothing() -> None:
    combined = walk(200, step=0.001) + walk(20, step=0.05)
    recent = realised_vol(combined, tenor_days=10, trading_days_per_year=252.0)
    everything = realised_vol(combined, tenor_days=190, trading_days_per_year=252.0)
    assert recent is not None
    assert everything is not None
    assert recent > everything
    assert realised_vol([100.0] * 40, tenor_days=30, trading_days_per_year=252.0) is None
    closes = walk(40, step=0.01)
    dirty = [*closes[:20], 0.0, -5.0, *closes[20:]]
    assert realised_vol(dirty, tenor_days=30, trading_days_per_year=252.0) == pytest.approx(
        realised_vol(closes, tenor_days=30, trading_days_per_year=252.0)
    )


# ── the ladder ──


def collectors_with(settings: ObliskConfig, **chain: Any) -> CollectorsConfig:
    return settings.collectors.model_copy(
        update={"chain": settings.collectors.chain.model_copy(update=chain)}
    )


def resolve(ladder: SigmaLadder, closes: list[float], iv: float | None, **kw: Any) -> Sigma:
    return ladder.resolve(closes, vendor_iv=iv, newest_close=kw.get("newest"), session=SESSION)


def test_the_rungs_fall_through_in_order_and_label_the_tenor_that_ran(
    shipped_settings: ObliskConfig,
) -> None:
    vol = shipped_settings.collectors.vol
    ladder = SigmaLadder(shipped_settings.collectors)
    first = resolve(ladder, walk(vol.primary_tenor_days + 5, step=0.01), 0.99)
    assert (first.source, first.is_fallback) == (f"realised_{vol.primary_tenor_days}d", False)
    assert first.value != 0.99
    second = resolve(ladder, walk(vol.fallback_tenor_days + 2, step=0.01), 0.99)
    assert (second.source, second.is_fallback) == (f"realised_{vol.fallback_tenor_days}d", True)
    assert (resolve(ladder, [], 0.42).source, resolve(ladder, [], 0.42).value) == (
        "vendor_iv",
        0.42,
    )
    last = resolve(ladder, [], None)
    assert (last.source, last.value, last.is_fallback) == ("default", vol.sigma_default, True)
    for bad in (0.0, -0.1, math.nan):
        assert resolve(ladder, [], bad).source == "default"
    assert ladder.closes_needed == max(vol.primary_tenor_days, vol.fallback_tenor_days) + 1


def test_a_reordered_ladder_is_honoured(shipped_settings: ObliskConfig) -> None:
    ladder = SigmaLadder(
        collectors_with(shipped_settings, sigma_ladder=["vendor_iv", "realised_primary", "default"])
    )
    sigma = resolve(ladder, walk(60, step=0.01), 0.42)
    assert (sigma.source, sigma.is_fallback) == ("vendor_iv", False)


def test_a_realised_rung_on_old_bars_is_stale_and_the_others_never_are(
    shipped_settings: ObliskConfig,
) -> None:
    ladder = SigmaLadder(shipped_settings.collectors)
    days = shipped_settings.collectors.chain.bars_freshness_days
    old = SESSION - timedelta(days=days + 1)
    assert resolve(ladder, walk(60, step=0.01), None, newest=old).is_stale
    fresh = SESSION - timedelta(days=days)
    assert not resolve(ladder, walk(60, step=0.01), None, newest=fresh).is_stale
    assert not resolve(ladder, [], None, newest=old).is_stale


def test_a_ladder_that_cannot_terminate_or_repeats_a_rung_is_refused(
    shipped_settings: ObliskConfig,
) -> None:
    chain = shipped_settings.collectors.chain.model_dump()
    with pytest.raises(ValidationError, match="must end with 'default'"):
        ChainCollectorConfig.model_validate({**chain, "sigma_ladder": ["realised_primary"]})
    with pytest.raises(ValidationError, match="each rung once"):
        ChainCollectorConfig.model_validate({**chain, "sigma_ladder": ["default", "default"]})
    with pytest.raises(ValidationError, match=r"unknown|literal"):
        ChainCollectorConfig.model_validate({**chain, "sigma_ladder": ["vendor_ivv", "default"]})
    with pytest.raises(ValidationError, match="more strikes"):
        ChainCollectorConfig.model_validate({**chain, "max_strikes_per_expiry_on_fallback": 61})


def test_a_sigma_is_finite_and_positive() -> None:
    for bad in (0.0, -0.1, math.inf):
        with pytest.raises(ValueError, match="finite and positive"):
            Sigma(bad, "default", is_fallback=True, is_stale=False)
