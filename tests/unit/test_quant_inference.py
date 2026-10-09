"""Hand-computed checks for the inference helpers: overlap-aware standard errors, the correction
for the number of looks, rank correlation, Brier skill and a seeded bootstrap.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from statistics import NormalDist

import pytest

from obliskiq.quant.inference import (
    benjamini_hochberg,
    bootstrap_mean_ci,
    brier_score,
    brier_skill,
    critical_value,
    newey_west_mean,
    normal_cdf,
    spearman_ic,
    two_sided_p_from_z,
)

ALPHA = 0.05
MIN_POINTS = 3
MIN_PAIRS = 5


def nw(values, lags):
    return newey_west_mean(values, lags, alpha=ALPHA, min_points=MIN_POINTS)


# ── the normal distribution ──


def test_normal_cdf_and_two_sided_p_known_values():
    assert normal_cdf(0.0) == pytest.approx(0.5)
    assert normal_cdf(1.96) == pytest.approx(0.975, abs=1e-4)
    assert two_sided_p_from_z(1.96) == pytest.approx(0.05, abs=1e-3)
    assert two_sided_p_from_z(float("nan")) == 1.0
    assert two_sided_p_from_z(float("inf")) == 1.0


def test_the_cdf_agrees_with_the_standard_library():
    for x in (-3.0, -0.5, 0.0, 0.7, 2.4):
        assert normal_cdf(x) == pytest.approx(NormalDist().cdf(x), abs=1e-12)


def test_the_critical_value_follows_the_level_asked_for():
    assert critical_value(0.05) == pytest.approx(1.959964, abs=1e-5)
    assert critical_value(0.01) == pytest.approx(2.575829, abs=1e-5)
    assert critical_value(0.10) < critical_value(0.05) < critical_value(0.01)


@pytest.mark.parametrize("alpha", [0.0, 1.0, -0.1, 1.5])
def test_a_level_outside_zero_and_one_is_refused(alpha):
    with pytest.raises(ValueError, match="alpha must lie strictly between 0 and 1"):
        critical_value(alpha)


# ── Newey-West ──


def test_newey_west_zero_lags_matches_plain_standard_error():
    out = nw([1.0, 2.0, 3.0, 4.0, 5.0], lags=0)
    # mean 3, population variance 2 -> se = sqrt(2/5)
    assert out.mean == pytest.approx(3.0)
    assert out.se == pytest.approx(math.sqrt(2.0 / 5.0))
    assert out.t == pytest.approx(3.0 / math.sqrt(0.4))
    lo, hi = out.ci
    assert lo < 3.0 < hi
    assert hi - 3.0 == pytest.approx(critical_value(ALPHA) * out.se)


def test_the_interval_widens_as_the_level_tightens():
    narrow = newey_west_mean([1.0, 2.0, 3.0, 4.0, 5.0], 0, alpha=0.10, min_points=MIN_POINTS)
    wide = newey_west_mean([1.0, 2.0, 3.0, 4.0, 5.0], 0, alpha=0.01, min_points=MIN_POINTS)
    assert wide.ci[1] - wide.ci[0] > narrow.ci[1] - narrow.ci[0]


def test_newey_west_widens_the_interval_for_positively_autocorrelated_series():
    # a slow-moving series: neighbouring values are alike, so an iid SE badly overstates precision
    xs = [0.0, 0.1, 0.3, 0.6, 1.0, 1.0, 0.6, 0.3, 0.1, 0.0, -0.1, -0.3, -0.6, -1.0, -1.0, -0.6]
    assert nw(xs, lags=3).se > nw(xs, lags=0).se


def test_newey_west_never_claims_more_precision_than_the_iid_estimate():
    """An alternating series has a near-zero kernel variance; for overlapping returns that is
    noise, not precision."""
    xs = [8.0, -8.0] * 16
    assert nw(xs, lags=3).se >= nw(xs, lags=0).se


def test_newey_west_constant_series_and_too_few_points():
    constant = nw([2.0, 2.0, 2.0, 2.0], lags=2)
    assert constant.se == 0.0
    assert constant.t is None
    assert constant.ci == (2.0, 2.0)
    short = nw([1.0, 2.0], lags=1)
    assert short.n == 2
    assert short.mean is None
    assert short.p is None


def test_newey_west_ignores_non_finite_values():
    out = nw([1.0, float("nan"), 2.0, None, 3.0], lags=0)
    assert out.n == 3
    assert out.mean == pytest.approx(2.0)


def test_more_lags_than_data_are_capped():
    xs = [1.0, 3.0, 2.0, 5.0]
    assert nw(xs, lags=100).se == nw(xs, lags=len(xs) - 1).se


@pytest.mark.parametrize("minimum", [0, -1, 1])
def test_a_minimum_too_small_for_a_standard_error_is_refused(minimum):
    with pytest.raises(ValueError, match="at least 2"):
        newey_west_mean([1.0, 2.0], 0, alpha=ALPHA, min_points=minimum)
    with pytest.raises(ValueError, match="at least 2"):
        bootstrap_mean_ci([1.0], block=1, n_boot=10, seed=1, alpha=ALPHA, min_points=minimum)


@pytest.mark.parametrize("minimum", [0, -1, 1, 2])
def test_a_minimum_too_small_for_a_rank_correlation_is_refused(minimum):
    with pytest.raises(ValueError, match="at least 3"):
        spearman_ic([1.0, 2.0], [1.0, 2.0], min_pairs=minimum)


@pytest.mark.parametrize("alpha", [0.0, 1.0, -0.2, 1.5])
def test_an_alpha_outside_the_open_unit_interval_is_refused_whatever_the_data(alpha):
    flat = [1.0, 1.0, 1.0]
    with pytest.raises(ValueError, match="alpha"):
        newey_west_mean(flat, 0, alpha=alpha, min_points=2)
    with pytest.raises(ValueError, match="alpha"):
        bootstrap_mean_ci(flat, block=1, n_boot=10, seed=1, alpha=alpha, min_points=2)


def test_zero_bootstrap_resamples_are_refused():
    with pytest.raises(ValueError, match="n_boot"):
        bootstrap_mean_ci([1.0, 2.0, 3.0], block=1, n_boot=0, seed=1, alpha=ALPHA, min_points=2)


# ── Benjamini-Hochberg ──


def test_bh_rejects_through_the_largest_rank_under_its_line():
    # m = 5, q = 0.05: thresholds 0.01, 0.02, 0.03, 0.04, 0.05 for ranks 1..5.
    p_values = [0.009, 0.019, 0.031, 0.5, 0.9]
    assert benjamini_hochberg(p_values, q=0.05) == [True, True, False, False, False]


def test_bh_step_up_rejects_a_rank_that_misses_its_own_line_if_a_later_one_clears_it():
    # rank 2 (0.03) is above its line 0.02, but rank 3 (0.03) is under its line 0.03: all three go.
    assert benjamini_hochberg([0.005, 0.03, 0.03, 0.8], q=0.05) == [True, True, True, False]


def test_bh_keeps_the_input_order_and_ignores_missing_and_non_finite():
    p_values = [0.9, None, 0.001, float("nan"), 0.04, float("inf")]
    flags = benjamini_hochberg(p_values, q=0.05)
    assert flags == [False, False, True, False, False, False]


def test_bh_with_no_finite_p_value_rejects_nothing():
    assert benjamini_hochberg([None, float("nan")], q=0.05) == [False, False]
    assert benjamini_hochberg([], q=0.05) == []


def test_bh_is_stricter_than_testing_each_at_q():
    p_values = [0.04, 0.045, 0.049]
    assert all(p < 0.05 for p in p_values)
    assert benjamini_hochberg(p_values, q=0.05) == [True, True, True]  # at the 3rd rank's line
    assert benjamini_hochberg([*p_values, 0.6, 0.7, 0.8, 0.9], q=0.05) == [False] * 7


# ── Spearman ──


def test_spearman_perfect_inverse_and_ties():
    ranks = {"min_pairs": MIN_PAIRS}
    assert spearman_ic([1, 2, 3, 4, 5], [10, 20, 30, 40, 50], **ranks) == pytest.approx(1.0)
    assert spearman_ic([1, 2, 3, 4, 5], [5, 4, 3, 2, 1], **ranks) == pytest.approx(-1.0)
    # monotone but nonlinear is still exactly 1 for a RANK correlation
    assert spearman_ic([1, 2, 3, 4, 5], [1, 4, 9, 16, 1000], **ranks) == pytest.approx(1.0)
    # ties get average ranks: x ranks (1.5,1.5,3,4,5) vs y ranks (1,2,3,4,5);
    # deviations from 3: dx = (-1.5,-1.5,0,1,2), dy = (-2,-1,0,1,2) -> cov 9.5, var 9.5 and 10
    tied = spearman_ic([1, 1, 2, 3, 4], [1, 2, 3, 4, 5], **ranks)
    assert tied == pytest.approx(9.5 / math.sqrt(95.0), abs=1e-9)


def test_spearman_undefined_cases_are_none_not_zero():
    assert spearman_ic([1, 1, 1, 1, 1], [1, 2, 3, 4, 5], min_pairs=MIN_PAIRS) is None
    assert spearman_ic([1, 2, 3], [1, 2, 3], min_pairs=MIN_PAIRS) is None  # too few pairs
    assert spearman_ic([1, 2, 3], [1, 2, 3], min_pairs=3) == pytest.approx(1.0)


def test_spearman_drops_a_pair_with_a_missing_member():
    signal = [1.0, 2.0, None, 4.0, 5.0, 6.0]
    outcome = [1.0, 2.0, 3.0, float("nan"), 5.0, 6.0]
    assert spearman_ic(signal, outcome, min_pairs=4) == pytest.approx(1.0)


# ── Brier ──


def test_brier_score_and_skill_hand_computed():
    probs = [0.8, 0.8, 0.2, 0.2]
    outcomes = [1, 1, 0, 0]
    assert brier_score(probs, outcomes) == pytest.approx(0.04)
    skill = brier_skill(probs, outcomes)
    # base rate 0.5 -> reference 0.25 -> skill 1 - 0.04/0.25 = 0.84
    assert skill.n == 4
    assert skill.base_rate == pytest.approx(0.5)
    assert skill.skill == pytest.approx(0.84)
    assert skill.mean_stated == pytest.approx(0.5)


def test_brier_skill_is_negative_for_confident_wrong_forecasts_and_zero_for_climatology():
    assert brier_skill([0.9, 0.9, 0.1, 0.1], [0, 0, 1, 1]).skill < 0
    assert brier_skill([0.5, 0.5, 0.5, 0.5], [1, 0, 1, 0]).skill == pytest.approx(0.0)


def test_brier_skill_undefined_when_outcome_never_varies():
    assert brier_skill([0.6, 0.7], [1, 1]).skill is None
    empty = brier_skill([], [])
    assert empty.n == 0
    assert empty.skill is None
    assert brier_score([], []) is None


def test_brier_ignores_a_missing_probability():
    assert brier_score([0.8, None, 0.2], [1, 1, 0]) == pytest.approx(0.04)


# ── bootstrap ──


def test_bootstrap_ci_is_reproducible_and_brackets_the_mean():
    xs = [0.5, -0.2, 0.8, 0.1, 0.4, -0.1, 0.6, 0.3, 0.2, 0.0, 0.7, -0.3]
    kwargs = {"block": 2, "n_boot": 500, "seed": 7, "alpha": ALPHA, "min_points": 5}
    first = bootstrap_mean_ci(xs, **kwargs)
    second = bootstrap_mean_ci(xs, **kwargs)
    assert first == second
    assert first.lo < first.mean < first.hi
    assert bootstrap_mean_ci(xs, **{**kwargs, "seed": 8}) != first


def test_bootstrap_needs_its_minimum_and_reports_what_it_has():
    kwargs = {"block": 1, "n_boot": 100, "seed": 1, "alpha": ALPHA, "min_points": 5}
    short = bootstrap_mean_ci([1.0, 2.0], **kwargs)
    assert (short.n, short.mean, short.lo, short.hi) == (2, 1.5, None, None)
    nothing = bootstrap_mean_ci([], **kwargs)
    assert (nothing.n, nothing.mean) == (0, None)


def test_a_block_longer_than_the_sample_is_capped():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = bootstrap_mean_ci(xs, block=100, n_boot=50, seed=3, alpha=ALPHA, min_points=5)
    assert out.mean == 3.0
    assert out.lo is not None


def test_a_narrower_level_gives_a_narrower_interval():
    xs = [0.5, -0.2, 0.8, 0.1, 0.4, -0.1, 0.6, 0.3, 0.2, 0.0, 0.7, -0.3]
    kwargs = {"block": 1, "n_boot": 2_000, "seed": 11, "min_points": 5}
    wide = bootstrap_mean_ci(xs, alpha=0.02, **kwargs)
    narrow = bootstrap_mean_ci(xs, alpha=0.40, **kwargs)
    assert narrow.hi - narrow.lo < wide.hi - wide.lo
