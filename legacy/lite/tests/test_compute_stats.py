"""The pure numerics, against hand-computed values.

Every engine above these functions is only as trustworthy as they are, and the
one behaviour that matters most is what they do with too little data: `None`,
never a number nobody could defend.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import math

import pytest

from obliskiq.compute import stats


def test_log_returns_skip_a_non_positive_print():
    """A zero print is bad data. Clamping it would invent a return."""
    assert stats.log_returns([100.0, 110.0]) == [math.log(1.1)]
    assert stats.log_returns([100.0, 0.0, 110.0]) == []


def test_log_returns_drops_a_pair_spanning_an_infinite_bar_rather_than_raising():
    """Both endpoints can be positive while their RATIO is not: a finite price
    following an infinite one divides to exactly 0.0, which math.log refuses
    with a ValueError rather than returning a number nobody could defend."""
    assert stats.log_returns([1.0, float("inf"), 2.0]) == []
    assert stats.log_returns([1.0, float("inf")]) == []
    # An infinite bar surrounded by ordinary ones only loses the two pairs that
    # actually touch it.
    assert stats.log_returns([100.0, float("inf"), 110.0, 121.0]) == [pytest.approx(math.log(1.1))]


def test_simple_return_is_what_the_account_earns():
    assert stats.simple_return(100.0, 125.0) == 0.25
    assert stats.simple_return(0.0, 125.0) is None
    assert stats.simple_return(None, 125.0) is None


def test_a_percentile_counts_ties_as_below():
    """A series pinned at one level has never been higher; that is the 100th."""
    assert stats.percentile_of([1.0, 2.0, 3.0, 4.0], 3.0) == 0.75
    assert stats.percentile_of([5.0, 5.0, 5.0], 5.0) == 1.0
    assert stats.percentile_of([], 5.0) is None


def test_drawdown_and_distances_are_signed_consistently():
    values = [100.0, 150.0, 120.0]
    assert stats.drawdown_from_peak(values) == pytest.approx(-0.2)
    assert stats.distance_from_low(values) == pytest.approx(0.2)


def test_correlation_of_a_flat_series_is_none_not_zero():
    """Zero would read as decoupling. A halted quote is not a decoupling."""
    assert stats.correlation([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == 1.0
    assert stats.correlation([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == -1.0
    assert stats.correlation([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None
    assert stats.correlation([1.0], [1.0]) is None


def test_correlation_with_an_infinite_reading_is_none_not_nan():
    """An infinite value makes its own mean and variance infinite or nan, which
    the plain `<= 0` flat-series guard does not catch: `inf <= 0` is False, so
    a refusal built for the zero case let an infinite one through as `nan` — a
    confident-looking violation of the function's own documented contract."""
    assert stats.correlation([1.0, float("inf"), 2.0], [1.0, 2.0, 3.0]) is None
    assert stats.correlation([1.0, float("-inf"), 2.0], [1.0, 2.0, 3.0]) is None


def test_ols_recovers_a_known_slope_and_reports_its_sample_size():
    fit = stats.ols([3.0, 5.0, 7.0, 9.0], [1.0, 2.0, 3.0, 4.0])
    assert fit is not None
    assert fit.beta == 2.0
    assert fit.alpha == 1.0
    assert fit.r2 == 1.0
    assert fit.observations == 4


def test_ols_r2_falls_when_the_relationship_is_noise():
    clean = stats.ols([2.0, 4.0, 6.0, 8.0], [1.0, 2.0, 3.0, 4.0])
    noisy = stats.ols([2.0, 9.0, 1.0, 8.0], [1.0, 2.0, 3.0, 4.0])
    assert clean is not None and noisy is not None
    assert clean.r2 == 1.0
    assert noisy.r2 < clean.r2


def test_ols_on_a_constant_regressor_is_undefined():
    assert stats.ols([1.0, 2.0, 3.0], [5.0, 5.0, 5.0]) is None


def test_ols_with_an_infinite_reading_is_none_not_a_confident_zero():
    """`r2 = max(0.0, 1.0 - residual / total)` collapses a nan residual to a
    confident-looking 0.0 — "measured, no relationship" — when the honest
    answer is "not measurable". A commodity proxy's whole wrapper
    recommendation reads r2 as whether the proxy has any explanatory power at
    all, so this is not a cosmetic difference."""
    assert stats.ols([1.0, float("inf"), 3.0], [1.0, 2.0, 3.0]) is None
    assert stats.ols([1.0, 2.0, 3.0], [1.0, float("inf"), 3.0]) is None


def test_true_range_includes_the_overnight_gap():
    """A name that gaps six percent has a true range far wider than its span."""
    highs = [100.0, 95.0]
    lows = [99.0, 94.0]
    closes = [100.0, 94.5]
    # Intraday span is 1.0; the gap from the prior close of 100 makes it 6.0.
    assert stats.true_ranges(highs, lows, closes) == [6.0]


def test_average_true_range_needs_a_full_period():
    highs = [10.0, 11.0, 12.0, 13.0]
    lows = [9.0, 10.0, 11.0, 12.0]
    closes = [9.5, 10.5, 11.5, 12.5]
    assert stats.average_true_range(highs, lows, closes, period=3) is not None
    assert stats.average_true_range(highs, lows, closes, period=10) is None


def test_annualisation_uses_the_configured_year(config):
    trading_days = config.require_float("data.trading_days_per_year")
    assert stats.annualise(0.01, trading_days_per_year=trading_days) == 0.01 * math.sqrt(
        trading_days
    )


def test_a_total_loss_has_no_annualised_rate():
    assert stats.annualised_from_period(0.21, years=2.0) is not None
    assert stats.annualised_from_period(-1.0, years=2.0) is None
    assert stats.annualised_from_period(0.5, years=0.0) is None


def test_three_year_depreciation_compounds_correctly():
    """The trailing figure the high-depreciation flag reads."""
    annual = stats.annualised_from_period(-0.271, years=3.0)
    assert annual is not None
    assert annual == pytest.approx(-0.10, abs=1e-3)


# ── paired returns must stay in lockstep ──


def test_paired_returns_drop_a_period_from_both_sides_or_neither():
    """Filtering each series separately silently pairs a Tuesday with a Thursday."""
    own_levels = [100.0, 101.0, 0.0, 103.0, 104.0]
    other_levels = [50.0, 50.5, 51.0, 51.5, 52.0]

    separately = (stats.log_returns(own_levels), stats.log_returns(other_levels))
    assert len(separately[0]) != len(separately[1]), "the defect this guards against"

    own, other = stats.paired_log_returns(own_levels, other_levels)

    assert len(own) == len(other) == 2
    # The two surviving periods are 100->101 and 103->104, and their partners are
    # the SAME periods on the other side.
    assert own == pytest.approx([math.log(101 / 100), math.log(104 / 103)])
    assert other == pytest.approx([math.log(50.5 / 50), math.log(52 / 51.5)])


def test_paired_returns_refuse_unaligned_inputs():
    with pytest.raises(ValueError, match="aligned"):
        stats.paired_log_returns([1.0, 2.0], [1.0, 2.0, 3.0])


def test_paired_returns_of_clean_series_match_the_unpaired_ones():
    left = [100.0, 101.0, 102.0]
    right = [50.0, 51.0, 52.0]
    own, other = stats.paired_log_returns(left, right)
    assert own == stats.log_returns(left)
    assert other == stats.log_returns(right)


def test_a_correlation_over_paired_returns_is_computable(config):
    """The end the fix exists for: a bad print must not corrupt the pairing."""
    own_levels = [100.0 * (1.01**n) for n in range(20)]
    other_levels = [50.0 * (1.01**n) for n in range(20)]
    other_levels[7] = 0.0  # one bad print on one side

    own, other = stats.paired_log_returns(own_levels, other_levels)
    assert stats.correlation(own, other) == pytest.approx(1.0)


# ── a rate is not a price, and its change is a DIFFERENCE ──
#
# `paired_changes` takes a transform per side because the two legs of a
# cross-asset correlation need not be the same kind of thing. A log return on a
# rate near zero is not a return and on a rate AT zero does not exist — ECB_DFR
# has sat at zero — so a rate's change is the first difference of its level, in
# the series' own units.
#
# None of that arithmetic was tested. The only level-change test in the suite
# asserted the transform LABEL on a correlation cell and that its status was
# "ok"; flipping `current - previous` to `previous - current` here — which
# inverts every equity-against-rates reading in the matrix, the signal the
# cross-asset work exists for — passed the entire suite. These pin the value and
# the SIGN against hand-computed arrays.


def test_a_level_change_is_the_first_difference_in_the_series_own_units():
    """Percentage points for a rate, not a ratio and not a percent."""
    rates = [5.00, 4.75, 4.75, 4.50]
    _, changes = stats.paired_changes(
        [1.0, 1.0, 1.0, 1.0],
        rates,
        right_transform=stats.TRANSFORM_LEVEL_CHANGE,
    )
    assert changes == pytest.approx([-0.25, 0.0, -0.25])


def test_a_level_change_keeps_the_direction_of_the_move():
    """The sign IS the signal. A cut is negative and a hike is positive, and a
    transform that returned `previous - current` would report every easing cycle
    as a tightening one while every status stayed `ok`."""
    _, cuts = stats.paired_changes(
        [1.0, 1.0], [5.0, 4.0], right_transform=stats.TRANSFORM_LEVEL_CHANGE
    )
    _, hikes = stats.paired_changes(
        [1.0, 1.0], [4.0, 5.0], right_transform=stats.TRANSFORM_LEVEL_CHANGE
    )
    assert cuts == pytest.approx([-1.0])
    assert hikes == pytest.approx([+1.0])


def test_a_rate_at_or_below_zero_still_has_a_change():
    """The reason the transform exists. Under log returns none of these periods
    survives: log(0) is undefined and a negative policy rate is real."""
    _, changes = stats.paired_changes(
        [1.0, 1.0, 1.0, 1.0],
        [0.25, 0.0, -0.10, 0.0],
        right_transform=stats.TRANSFORM_LEVEL_CHANGE,
    )
    assert changes == pytest.approx([-0.25, -0.10, +0.10])
    assert len(changes) == 3, "no period is dropped for touching or crossing zero"

    # The same series log-returned keeps nothing, which is the defect being avoided.
    _, as_returns = stats.paired_changes([1.0, 1.0, 1.0, 1.0], [0.25, 0.0, -0.10, 0.0])
    assert as_returns == []


def test_a_level_change_drops_a_period_touching_an_infinite_reading():
    """`current - previous` is itself infinite or nan against an infinite rate,
    which is the same silent-non-finite-value failure the log-return branch has,
    reached through the sibling transform the cross-asset matrix uses for a
    rate leg."""
    left, right = stats.paired_changes(
        [1.0, 1.0, 1.0],
        [0.25, float("inf"), 0.30],
        right_transform=stats.TRANSFORM_LEVEL_CHANGE,
    )
    assert left == []
    assert right == []


def test_an_equity_leg_and_a_rate_leg_are_transformed_independently():
    """The cross-asset case, end to end: a price against a rate, one log-returned
    and one differenced, in lockstep."""
    prices = [100.0, 101.0, 102.01]
    rates = [3.0, 2.5, 2.5]

    equity, rate = stats.paired_changes(prices, rates, right_transform=stats.TRANSFORM_LEVEL_CHANGE)

    assert equity == pytest.approx([math.log(1.01), math.log(1.01)])
    assert rate == pytest.approx([-0.5, 0.0])
    assert len(equity) == len(rate)


def test_a_mixed_pair_drops_a_period_from_BOTH_sides_or_neither():
    """Lockstep is not a property of the log-return path; it is the contract.

    The rate leg can always be differenced, so only the price leg can reject a
    period — and when it does the rate's change for that period has to go too, or
    the two series slip by one and every later pairing is off by a day.
    """
    prices = [100.0, 0.0, 102.0, 103.0]
    rates = [3.0, 2.9, 2.8, 2.7]

    equity, rate = stats.paired_changes(prices, rates, right_transform=stats.TRANSFORM_LEVEL_CHANGE)

    assert len(equity) == len(rate) == 1, "only 102 -> 103 has two usable prices"
    assert equity == pytest.approx([math.log(103 / 102)])
    assert rate == pytest.approx([-0.1]), "the surviving period's rate change, not the first"


def test_both_legs_may_be_level_changes():
    """Not a pair the matrix computes today — rate x rate is deliberately absent —
    but the function takes two transforms and both settings have to work."""
    left, right = stats.paired_changes(
        [5.0, 4.0],
        [2.0, 2.5],
        left_transform=stats.TRANSFORM_LEVEL_CHANGE,
        right_transform=stats.TRANSFORM_LEVEL_CHANGE,
    )
    assert left == pytest.approx([-1.0])
    assert right == pytest.approx([+0.5])


def test_an_unknown_transform_is_refused_rather_than_defaulted():
    """Falling back to log returns would silently take the logarithm of a rate."""
    with pytest.raises(ValueError, match="unknown transform"):
        stats.paired_changes([1.0, 2.0], [1.0, 2.0], right_transform="percent_change")


def test_the_declared_transforms_are_the_ones_the_matrix_can_ask_for():
    """The names travel from CorrelationLeg.transform into this module, so a
    rename in one place and not the other is a ValueError at build time."""
    assert set(stats.TRANSFORMS) == {
        stats.TRANSFORM_LOG_RETURN,
        stats.TRANSFORM_LEVEL_CHANGE,
    }


# ── the pair's own norm ──


def test_rolling_correlations_walk_the_window_across_the_sample():
    left = [float(n) for n in range(20)]
    right = [float(n) * 2 for n in range(20)]
    history = stats.rolling_correlations(left, right, window=5)
    assert len(history) == len(left) - 5 + 1
    assert all(value == pytest.approx(1.0) for value in history)


def test_rolling_correlations_respect_the_sample_bound():
    left = [float(n % 7) for n in range(100)]
    right = [float((n + 3) % 5) for n in range(100)]
    full = stats.rolling_correlations(left, right, window=10)
    bounded = stats.rolling_correlations(left, right, window=10, sample=30)
    assert len(bounded) < len(full)
    assert len(bounded) == 30 - 10 + 1


def test_the_current_reading_is_excluded_from_its_own_baseline():
    """Including it drags the mean toward whatever is being tested."""
    history = [0.10, 0.12, 0.11, 0.13, 0.90]
    deviation = stats.norm_deviation(history)
    assert deviation is not None
    assert deviation.current == 0.90
    assert deviation.mean == pytest.approx(stats.mean(history[:-1]))
    assert deviation.z_score > 3


def test_a_history_with_no_dispersion_has_nothing_to_deviate_from():
    """Zero would read as unremarkable rather than unmeasurable."""
    assert stats.norm_deviation([0.5, 0.5, 0.5, 0.5]) is None
    assert stats.norm_deviation([0.5, 0.9]) is None
    assert stats.norm_deviation([]) is None


def test_a_stepped_history_is_independent_and_ends_on_the_newest_reading():
    """The norm behind a z-score has to be readings that do not share their data.

    Sampled every session, two consecutive windows differ by one observation, so
    the series is smooth by construction and its standard deviation measures
    that smoothness — which inflates every z-score divided by it.
    """
    left = [float((n * 7) % 11) for n in range(100)]
    right = [float((n * 3) % 13) for n in range(100)]

    overlapping = stats.rolling_correlations(left, right, window=10)
    independent = stats.rolling_correlations(left, right, window=10, step=10)

    assert len(overlapping) == 91
    assert len(independent) == 10, "100 observations hold ten non-overlapping 10-day windows"

    # However the history is sampled, the value being TESTED is the current one.
    assert independent[-1] == pytest.approx(overlapping[-1])


def test_a_stepped_history_keeps_the_most_recent_sessions():
    """Walking forwards would drop the newest ones when the stride does not divide.

    103 observations and a stride of 10: something has to be left out, and it
    must not be the reading the shift is about.
    """
    left = [float((n * 7) % 11) for n in range(103)]
    right = [float((n * 3) % 13) for n in range(103)]

    independent = stats.rolling_correlations(left, right, window=10, step=10)
    newest = stats.correlation(left[-10:], right[-10:])

    assert newest is not None
    assert independent[-1] == pytest.approx(newest)


def test_the_smoothness_of_an_overlapping_history_understates_dispersion():
    """The defect, as a number: the same data, two standard deviations.

    A slow regime change read every session looks calm; read at intervals that
    do not share data, it shows the move. The z-score divides by this.
    """
    left = [float(n % 5) for n in range(200)]
    right = [float(n % 5) if n < 100 else float((-n) % 5) for n in range(200)]

    overlapping = stats.rolling_correlations(left, right, window=20)
    independent = stats.rolling_correlations(left, right, window=20, step=20)

    assert stats.stdev(independent) is not None
    assert stats.stdev(overlapping) is not None
    assert stats.stdev(independent) > stats.stdev(overlapping)


def test_max_drawdown_measures_the_fall_lived_through_not_the_position_today():
    """The two were one computation under two names, so one family measured nothing new.

    A series fully recovered from a collapse sits at its high — zero distance
    from it — and still lived through the collapse. Only the second is a
    statement about the risk in holding it.
    """
    recovered = [100.0, 60.0, 100.0]
    assert stats.drawdown_from_peak(recovered) == pytest.approx(0.0)
    assert stats.max_drawdown(recovered) == pytest.approx(-0.40)


def test_max_drawdown_takes_the_worst_of_several_falls():
    values = [100.0, 90.0, 100.0, 50.0, 80.0, 70.0]
    assert stats.max_drawdown(values) == pytest.approx(-0.50)


def test_max_drawdown_of_a_monotone_series_is_zero_and_of_nothing_is_none():
    assert stats.max_drawdown([1.0, 2.0, 3.0]) == pytest.approx(0.0)
    assert stats.max_drawdown([]) is None, "no drawdown measured is not a drawdown of nothing"
