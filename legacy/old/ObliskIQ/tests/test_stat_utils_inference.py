"""Hand-computed checks for the scoreboard's inference helpers (stat_utils, v6.16.0)."""

import math

import pytest

from modules.analytics.stat_utils import (
    bootstrap_mean_ci,
    brier_score,
    brier_skill,
    newey_west_mean,
    normal_cdf,
    spearman_ic,
    two_sided_p_from_z,
)


def test_normal_cdf_and_two_sided_p_known_values():
    assert normal_cdf(0.0) == pytest.approx(0.5)
    assert normal_cdf(1.96) == pytest.approx(0.975, abs=1e-4)
    assert two_sided_p_from_z(1.96) == pytest.approx(0.05, abs=1e-3)
    assert two_sided_p_from_z(float("nan")) == 1.0


def test_newey_west_zero_lags_matches_plain_standard_error():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = newey_west_mean(xs, lags=0)
    # mean 3, population variance 2 -> se = sqrt(2/5)
    assert out["mean"] == pytest.approx(3.0)
    assert out["se"] == pytest.approx(math.sqrt(2.0 / 5.0))
    assert out["t"] == pytest.approx(3.0 / math.sqrt(0.4))
    lo, hi = out["ci95"]
    assert lo < 3.0 < hi


def test_newey_west_widens_the_interval_for_positively_autocorrelated_series():
    # a slow-moving series: neighbouring values are alike, so an iid SE badly overstates precision
    xs = [0.0, 0.1, 0.3, 0.6, 1.0, 1.0, 0.6, 0.3, 0.1, 0.0, -0.1, -0.3, -0.6, -1.0, -1.0, -0.6]
    iid = newey_west_mean(xs, lags=0)
    nw = newey_west_mean(xs, lags=3)
    assert nw["se"] > iid["se"]


def test_newey_west_never_claims_more_precision_than_the_iid_estimate():
    """An alternating series has a near-zero kernel variance; for overlapping returns that is noise, not precision."""
    xs = [8.0, -8.0] * 16
    assert newey_west_mean(xs, lags=3)["se"] >= newey_west_mean(xs, lags=0)["se"]


def test_newey_west_constant_series_and_too_few_points():
    assert newey_west_mean([2.0, 2.0, 2.0, 2.0], lags=2)["t"] is None
    short = newey_west_mean([1.0, 2.0], lags=1)
    assert short["mean"] is None and short["p"] is None


def test_newey_west_ignores_non_finite_values():
    out = newey_west_mean([1.0, float("nan"), 2.0, None, 3.0], lags=0)  # type: ignore[list-item]
    assert out["n"] == 3 and out["mean"] == pytest.approx(2.0)


def test_spearman_perfect_inverse_and_ties():
    assert spearman_ic([1, 2, 3, 4, 5], [10, 20, 30, 40, 50]) == pytest.approx(1.0)
    assert spearman_ic([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == pytest.approx(-1.0)
    # monotone but nonlinear is still exactly 1 for a RANK correlation
    assert spearman_ic([1, 2, 3, 4, 5], [1, 4, 9, 16, 1000]) == pytest.approx(1.0)
    # ties get average ranks: x ranks (1.5,1.5,3,4,5) vs y ranks (1,2,3,4,5);
    # deviations from 3: dx = (-1.5,-1.5,0,1,2), dy = (-2,-1,0,1,2) -> cov 9.5, var 9.5 and 10
    tied = spearman_ic([1, 1, 2, 3, 4], [1, 2, 3, 4, 5])
    assert tied == pytest.approx(9.5 / math.sqrt(95.0), abs=1e-9)


def test_spearman_undefined_cases_are_none_not_zero():
    assert spearman_ic([1, 1, 1, 1, 1], [1, 2, 3, 4, 5]) is None
    assert spearman_ic([1, 2, 3], [1, 2, 3]) is None  # fewer than 5 pairs


def test_brier_score_and_skill_hand_computed():
    probs = [0.8, 0.8, 0.2, 0.2]
    outcomes = [1, 1, 0, 0]
    assert brier_score(probs, outcomes) == pytest.approx(0.04)
    skill = brier_skill(probs, outcomes)
    # base rate 0.5 -> reference 0.25 -> skill 1 - 0.04/0.25 = 0.84
    assert skill["base_rate"] == pytest.approx(0.5)
    assert skill["skill"] == pytest.approx(0.84)


def test_brier_skill_is_negative_for_confident_wrong_forecasts_and_zero_for_climatology():
    wrong = brier_skill([0.9, 0.9, 0.1, 0.1], [0, 0, 1, 1])
    assert wrong["skill"] < 0
    climatology = brier_skill([0.5, 0.5, 0.5, 0.5], [1, 0, 1, 0])
    assert climatology["skill"] == pytest.approx(0.0)


def test_brier_skill_undefined_when_outcome_never_varies():
    assert brier_skill([0.6, 0.7], [1, 1])["skill"] is None
    assert brier_skill([], [])["n"] == 0


def test_bootstrap_ci_is_reproducible_and_brackets_the_mean():
    xs = [0.5, -0.2, 0.8, 0.1, 0.4, -0.1, 0.6, 0.3, 0.2, 0.0, 0.7, -0.3]
    first = bootstrap_mean_ci(xs, block=2)
    second = bootstrap_mean_ci(xs, block=2)
    assert first == second
    assert first["lo"] < first["mean"] < first["hi"]
    assert bootstrap_mean_ci([1.0, 2.0])["lo"] is None
