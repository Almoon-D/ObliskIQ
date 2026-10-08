"""Realised volatility and the sigma fallback ladder.

The ladder exists so a snapshot is never lost for want of history. What it must
never do is lose the fact that it fell back — the strike band would then be wider
and weaker with nothing on the row or the run saying so.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import copy
import math

import pytest

from obliskiq.collect.vol import (
    FREQ_DAILY,
    SigmaLadder,
    garman_klass_from_bars,
    garman_klass_vol,
    ohlc_bars,
    parkinson_from_bars,
    parkinson_vol,
    range_bars,
    realised_vol,
    recent_daily_closes,
    restated_open_share,
)
from obliskiq.config import Config
from obliskiq.errors import ConfigError

TRADING_DAYS_PER_YEAR = 252.0


def walk(count: int, *, step: float, start: float = 100.0) -> list[float]:
    """Closes that alternate up and down by ``step`` in log terms.

    Deterministic, and its realised vol is analytically ``step * sqrt(252)`` on a
    long enough sample, which is what makes the annualisation assertion exact.
    """
    return [start * math.exp(step * ((-1) ** i)) for i in range(count)]


# ── realised vol ──


def test_realised_vol_is_annualised_on_the_configured_trading_year(config):
    """Every rung of the ladder is annualised — vendor IV and sigma_default are —
    so a ladder mixing units would rescale the band depending on which rung fired."""
    closes = walk(60, step=0.01)
    daily_sigma = realised_vol(closes, tenor_days=30, trading_days_per_year=1.0)
    annual_sigma = realised_vol(closes, tenor_days=30, trading_days_per_year=TRADING_DAYS_PER_YEAR)
    assert annual_sigma == pytest.approx(daily_sigma * math.sqrt(TRADING_DAYS_PER_YEAR))


def test_n_returns_need_n_plus_one_closes(config):
    """The off-by-one that would silently shorten every window."""
    assert realised_vol(walk(30, step=0.01), tenor_days=30, trading_days_per_year=252.0) is None
    assert realised_vol(walk(31, step=0.01), tenor_days=30, trading_days_per_year=252.0) is not None


def test_only_the_most_recent_window_is_used():
    """A long history must not dilute a short tenor."""
    quiet, loud = walk(200, step=0.001), walk(20, step=0.05)
    combined = quiet + loud
    recent = realised_vol(combined, tenor_days=10, trading_days_per_year=252.0)
    everything = realised_vol(combined, tenor_days=190, trading_days_per_year=252.0)
    assert recent is not None and everything is not None
    assert recent > everything


def test_a_flat_series_has_no_measurable_vol_and_returns_none():
    """Zero is not a volatility, it is the absence of one. Returning 0.0 would
    collapse the strike band onto spot and record a single strike as if that were
    the answer."""
    assert realised_vol([100.0] * 40, tenor_days=30, trading_days_per_year=252.0) is None


def test_non_positive_and_missing_closes_are_dropped_not_logged():
    """log(0) is not a return. A bad print must not become a volatility spike."""
    closes = walk(40, step=0.01)
    contaminated = [*closes[:20], 0.0, None, -5.0, *closes[20:]]  # type: ignore[list-item]
    assert realised_vol(contaminated, tenor_days=30, trading_days_per_year=252.0) == pytest.approx(
        realised_vol(closes, tenor_days=30, trading_days_per_year=252.0)
    )


# ── the range-based estimators ──


def ohlc_walk(count: int, *, step: float, range_step: float, start: float = 100.0):
    """Bars that alternate up and down, with a genuine open and a real range.

    The open is deliberately NOT the previous close: the whole point of the
    Garman-Klass guard is telling a real opening print from a restated one, and a
    generator that produced restated opens would make every test here exercise
    the refusal path by accident.
    """
    closes = walk(count, step=step, start=start)
    opens = [close * math.exp(-step / 2) for close in closes]
    highs = [max(o, c) * math.exp(range_step) for o, c in zip(opens, closes, strict=True)]
    lows = [min(o, c) * math.exp(-range_step) for o, c in zip(opens, closes, strict=True)]
    return opens, highs, lows, closes


def test_parkinson_is_annualised_on_the_configured_trading_year():
    """Every volatility in this build is annualised on one basis, these included.

    A range estimator quoted per session beside a close-to-close one quoted per
    year is the unit mismatch the sigma ladder's docstring already warns about,
    one estimator further along.
    """
    _, highs, lows, _ = ohlc_walk(60, step=0.01, range_step=0.005)
    daily = parkinson_vol(highs, lows, tenor_days=30, trading_days_per_year=1.0)
    annual = parkinson_vol(highs, lows, tenor_days=30, trading_days_per_year=TRADING_DAYS_PER_YEAR)
    assert daily is not None and annual is not None
    assert annual == pytest.approx(daily * math.sqrt(TRADING_DAYS_PER_YEAR))


def test_a_range_estimator_needs_one_fewer_row_than_close_to_close():
    """n returns need n + 1 prices; n range terms need n bars. Both are n sessions.

    The asymmetry is real and is why the two leaves at the same tenor carry
    different row counts. Asserting it here stops a later "tidy-up" aligning the
    two on the wrong one.
    """
    _, highs, lows, closes = ohlc_walk(30, step=0.01, range_step=0.005)
    assert realised_vol(closes, tenor_days=30, trading_days_per_year=252.0) is None
    assert parkinson_vol(highs, lows, tenor_days=30, trading_days_per_year=252.0) is not None
    assert parkinson_vol(highs, lows, tenor_days=31, trading_days_per_year=252.0) is None


def test_a_bar_with_no_high_or_low_is_dropped_rather_than_truncating_the_series():
    """Every term is a property of one bar, so a hole leaves no spliced return.

    That is the difference from a close-to-close series, where dropping a bar
    silently makes a two-day move look like a one-day one.
    """
    _, highs, lows, _ = ohlc_walk(40, step=0.01, range_step=0.005)
    contaminated_highs = list(highs)
    contaminated_lows = list(lows)
    contaminated_highs[5] = None  # type: ignore[call-overload]
    contaminated_lows[9] = None  # type: ignore[call-overload]
    assert len(range_bars(contaminated_highs, contaminated_lows)) == len(highs) - 2
    assert (
        parkinson_vol(
            contaminated_highs, contaminated_lows, tenor_days=30, trading_days_per_year=252.0
        )
        is not None
    )


def test_an_inverted_or_non_positive_bar_is_refused_not_logged():
    """high < low is a corrupt row, and log of a non-positive price is not a range."""
    highs = [101.0, 100.0, 101.0]
    lows = [100.0, 101.0, 0.0]
    assert range_bars(highs, lows) == [(101.0, 100.0)]


def test_a_window_with_no_range_at_all_has_no_measurable_vol():
    """Zero is not a volatility here either, for the same reason as close-to-close."""
    flat = [100.0] * 40
    assert parkinson_vol(flat, flat, tenor_days=30, trading_days_per_year=252.0) is None


def test_one_zero_range_bar_is_kept_because_a_halted_session_really_had_none():
    """Dropping the quietest observations would raise the estimate by selection."""
    _, highs, lows, _ = ohlc_walk(40, step=0.01, range_step=0.005)
    halted_highs = [*highs[:20], 100.0, *highs[20:]]
    halted_lows = [*lows[:20], 100.0, *lows[20:]]
    with_halt = parkinson_vol(halted_highs, halted_lows, tenor_days=30, trading_days_per_year=252.0)
    without = parkinson_vol(highs, lows, tenor_days=30, trading_days_per_year=252.0)
    assert with_halt is not None and without is not None
    assert with_halt < without


def test_a_close_or_open_outside_its_own_bar_is_a_corrupt_row():
    """Garman-Klass would take the logarithm of it regardless, so ohlc_bars refuses."""
    assert ohlc_bars([105.0], [101.0], [99.0], [100.0]) == []
    assert ohlc_bars([100.0], [101.0], [99.0], [95.0]) == []
    assert ohlc_bars([100.0], [101.0], [99.0], [100.5]) == [(100.0, 101.0, 99.0, 100.5)]


def test_restated_opens_are_measured_as_a_share_and_compared_exactly():
    """The case NO fixture in this repository provides, so it is constructed here.

    Some venues publish no opening auction print and serve the previous close in
    its place. Compared with ``==`` rather than a tolerance: a restatement is a
    copied value and bit-identical, while a tolerance would additionally catch
    every instrument that genuinely opened unchanged.
    """
    _, highs, lows, closes = ohlc_walk(10, step=0.01, range_step=0.02)
    honest = [close * math.exp(-0.005) for close in closes]
    restated = [closes[0], *closes[:-1]]

    assert restated_open_share(ohlc_bars(honest, highs, lows, closes)) == 0.0
    assert restated_open_share(ohlc_bars(restated, highs, lows, closes)) == pytest.approx(1.0)
    # A single restated bar among nine gaps is a ninth, not a verdict. What to do
    # about it is data.garman_klass_max_restated_open_share's decision, at the
    # caller: how much is too much is policy and not arithmetic.
    mixed = [*honest[:5], closes[4], *honest[6:]]
    assert restated_open_share(ohlc_bars(mixed, highs, lows, closes)) == pytest.approx(1 / 9)


def test_a_share_needs_two_bars_to_have_a_gap_between_them():
    assert restated_open_share([]) is None
    assert restated_open_share(ohlc_bars([100.0], [101.0], [99.0], [100.0])) is None


def test_the_ordering_invariant_is_what_keeps_garman_klass_non_negative():
    """The subtracted term cannot win, and the reason is the FILTER, not the formula.

    ``0.5 (ln H/L)^2 - (2 ln2 - 1)(ln C/O)^2`` looks as though it could go
    negative, and it can — but only on a bar whose open or close sits outside its
    own range, because otherwise ``|ln C/O| <= ln H/L`` and the weight is under a
    half. That is exactly the row ``ohlc_bars`` refuses, which makes the filter
    load-bearing rather than tidy. Asserting it here stops the invariant being
    relaxed later on the grounds that it looked like belt and braces.
    """
    worst = ohlc_bars([100.0], [130.0], [100.0], [130.0])  # close AT the high
    assert garman_klass_from_bars(worst, trading_days_per_year=252.0) is not None

    # Bypassing the filter is the only way to reach a negative sum, and the
    # backstop refuses rather than square-rooting it.
    assert garman_klass_from_bars([(100.0, 101.0, 100.0, 130.0)], trading_days_per_year=252.0) is (
        None
    )


def test_garman_klass_refuses_a_window_with_no_range_in_it():
    """The only way a well-formed window sums to zero."""
    flat = [100.0] * 40
    assert (
        garman_klass_vol(flat, flat, flat, flat, tenor_days=30, trading_days_per_year=252.0) is None
    )


def test_a_restated_open_moves_garman_klass_without_looking_wrong():
    """Why the guard cannot be "refuse a bad-looking number".

    Restating the close as the open makes ln(C/O) carry the whole close-to-close
    return where the estimator's derivation expects the within-session move. The
    result is a different quantity, and the DIRECTION is not fixed — it depends
    on whether the restated open falls inside the range the venue served, which
    is a fact about that instrument's gaps rather than about the formula. What is
    fixed is that it stays positive, stays plausible, and betrays nothing. Only
    restated_open_share can see it, which is why the caller tests that BEFORE
    computing rather than sanity-checking the output afterwards.
    """
    opens, highs, lows, closes = ohlc_walk(40, step=0.01, range_step=0.005)

    # A venue that restates publishes the previous close as the open, with highs
    # and lows that still bracket the bar — so the rows are well formed and
    # ohlc_bars has no reason to drop them. Built that way deliberately: a series
    # the filter rejected would prove nothing about the estimator.
    restated = [closes[0], *closes[:-1]]
    restated_highs = [max(o, c) * math.exp(0.005) for o, c in zip(restated, closes, strict=True)]
    restated_lows = [min(o, c) * math.exp(-0.005) for o, c in zip(restated, closes, strict=True)]
    assert len(ohlc_bars(restated, restated_highs, restated_lows, closes)) == len(closes)

    honest_value = garman_klass_vol(
        opens, highs, lows, closes, tenor_days=30, trading_days_per_year=252.0
    )
    restated_value = garman_klass_vol(
        restated, restated_highs, restated_lows, closes, tenor_days=30, trading_days_per_year=252.0
    )
    assert honest_value is not None and restated_value is not None
    assert restated_value > 0, "it does not fail loudly; that is the whole problem"
    assert abs(restated_value - honest_value) > honest_value / 2, (
        "the two are far apart, and nothing in either number says which is which"
    )
    assert restated_open_share(ohlc_bars(restated, restated_highs, restated_lows, closes)) == 1.0


def test_on_a_gapping_instrument_a_restated_open_falls_outside_its_own_bar():
    """The other way the restatement shows up, and it is the filter that catches it.

    A venue serving a real intraday high and low with a fake open contradicts
    itself the moment the instrument gaps: yesterday's close is not inside
    today's range. ohlc_bars drops those rows, so the Garman-Klass leaf can end
    up refusing for want of usable bars before the share test is ever reached —
    a different reason code for the same underlying defect, and an honest one.
    Parkinson reads the same series unaffected: it never looks at the open.
    """
    _, highs, lows, closes = ohlc_walk(40, step=0.01, range_step=0.002)
    restated = [closes[0], *closes[:-1]]

    assert len(ohlc_bars(restated, highs, lows, closes)) < len(closes) / 2
    assert (
        garman_klass_vol(restated, highs, lows, closes, tenor_days=30, trading_days_per_year=252.0)
        is None
    )
    assert parkinson_vol(highs, lows, tenor_days=30, trading_days_per_year=252.0) is not None


def test_the_estimators_agree_with_their_own_window_helpers():
    """The tenor-taking wrappers are the same computation over the last n bars.

    One implementation per estimator: the rolling percentile in the grid calls
    the ``_from_bars`` form directly, and if the two diverged a cell's level and
    its own percentile would be measured differently.
    """
    opens, highs, lows, closes = ohlc_walk(60, step=0.01, range_step=0.005)
    hl = range_bars(highs, lows)
    ohlc = ohlc_bars(opens, highs, lows, closes)
    assert parkinson_vol(highs, lows, tenor_days=30, trading_days_per_year=252.0) == pytest.approx(
        parkinson_from_bars(hl[-30:], trading_days_per_year=252.0)
    )
    assert garman_klass_vol(
        opens, highs, lows, closes, tenor_days=30, trading_days_per_year=252.0
    ) == pytest.approx(garman_klass_from_bars(ohlc[-30:], trading_days_per_year=252.0))


def test_recent_daily_closes_returns_oldest_first(seeded_store):
    """Order matters: the return series is built by walking forward."""
    with seeded_store.transaction() as connection:
        connection.executemany(
            "INSERT INTO bars (contract_id, ts, freq, close) VALUES (?, ?, ?, ?)",
            [(1, f"2026-01-{day:02d}T00:00:00Z", FREQ_DAILY, 100.0 + day) for day in range(1, 11)],
        )
    closes = recent_daily_closes(seeded_store, 1, limit=5)
    assert closes == [106.0, 107.0, 108.0, 109.0, 110.0]


# ── the ladder ──


def ladder_with(config, **chain_overrides) -> SigmaLadder:
    data = copy.deepcopy(config.as_dict())
    data["collectors"]["chain"].update(chain_overrides)
    return SigmaLadder(Config(data))


def test_rung_one_is_realised_vol_over_the_configured_tenor(config):
    ladder = SigmaLadder(config)
    tenor = config.require_int("data.strike_bounds_rv_tenor_days")
    sigma = ladder.resolve(walk(tenor + 5, step=0.01), vendor_iv=0.99)

    assert sigma.source == f"realised_{tenor}d"
    assert sigma.is_fallback is False
    # Vendor IV was available and deliberately ignored: rung order is order.
    assert sigma.value != 0.99


def test_rung_two_fires_when_there_is_too_little_history_for_rung_one(config):
    ladder = SigmaLadder(config)
    fallback_tenor = config.require_int("collectors.chain.realised_fallback_tenor_days")
    primary_tenor = config.require_int("data.strike_bounds_rv_tenor_days")

    sigma = ladder.resolve(walk(fallback_tenor + 2, step=0.01), vendor_iv=0.99)
    assert sigma.source == f"realised_{fallback_tenor}d"
    assert sigma.is_fallback is True
    assert fallback_tenor < primary_tenor


def test_rung_three_is_the_vendor_iv(config):
    sigma = SigmaLadder(config).resolve([], vendor_iv=0.42)
    assert sigma.source == "vendor_iv"
    assert sigma.value == 0.42
    assert sigma.is_fallback is True


def test_rung_four_is_the_configured_default(config):
    sigma = SigmaLadder(config).resolve([], vendor_iv=None)
    assert sigma.source == "default"
    assert sigma.value == config.require_float("data.sigma_default")
    assert sigma.is_fallback is True


def test_a_non_positive_vendor_iv_falls_through_rather_than_being_used(config):
    """A zero IV is an absent quote, not a volatility of nothing."""
    for bad in (0.0, -0.1):
        assert SigmaLadder(config).resolve([], vendor_iv=bad).source == "default"


def test_the_ladder_always_terminates(config):
    """Whatever the inputs, a sigma comes back. Losing a snapshot for want of a
    volatility estimate is the one outcome that cannot be undone tomorrow."""
    for closes, vendor in (([], None), ([], 0.0), ([100.0], None), ([100.0] * 50, None)):
        assert SigmaLadder(config).resolve(closes, vendor_iv=vendor).value > 0


def test_the_stored_label_names_the_tenor_that_actually_ran(config):
    """A retuned tenor must not leave rows claiming a window nobody used."""
    data = copy.deepcopy(config.as_dict())
    data["data"]["strike_bounds_rv_tenor_days"] = 21
    sigma = SigmaLadder(Config(data)).resolve(walk(30, step=0.01), vendor_iv=None)
    assert sigma.source == "realised_21d"


def test_rung_order_is_honoured_when_the_ladder_is_reordered(config):
    """The ladder is config, not a hardcoded sequence."""
    ladder = ladder_with(config, sigma_ladder=["vendor_iv", "realised_30d", "default"])
    sigma = ladder.resolve(walk(60, step=0.01), vendor_iv=0.42)
    assert sigma.source == "vendor_iv"
    assert sigma.is_fallback is False  # first rung, whatever it happens to be


def test_an_empty_ladder_is_refused(config):
    with pytest.raises(ConfigError, match="sigma_ladder is empty"):
        ladder_with(config, sigma_ladder=[])


def test_an_unknown_rung_is_refused_at_construction_not_at_use(config):
    """On a VM with nobody watching, a typo must fail at startup rather than at
    the moment a band is needed."""
    with pytest.raises(ConfigError, match="unknown rungs"):
        ladder_with(config, sigma_ladder=["realised_30d", "vendor_ivv"])


def test_a_ladder_that_cannot_terminate_says_so(config):
    ladder = ladder_with(config, sigma_ladder=["realised_30d"])
    with pytest.raises(ConfigError, match="always terminates"):
        ladder.resolve([], vendor_iv=None)


def test_max_bars_needed_covers_the_most_demanding_realised_rung(config):
    ladder = SigmaLadder(config)
    assert ladder.max_bars_needed == config.require_int("data.strike_bounds_rv_tenor_days") + 1
