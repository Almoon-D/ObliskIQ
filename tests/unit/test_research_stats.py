"""The research statistics against closed forms and hand-computed values.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from datetime import date
from statistics import NormalDist

import numpy as np
import pytest

from obliskiq.research.stats import (
    EULER_MASCHERONI,
    annualised_log_growth,
    block_bootstrap_indices,
    bonferroni_interval,
    bootstrap,
    breach_probability,
    breakeven_multiplier,
    critical_value,
    cross_sectional_slope,
    deflated_sharpe,
    detectable_effect,
    drawdowns,
    effective_n,
    expected_maximum_sharpe,
    fama_macbeth,
    max_drawdown,
    moments,
    newey_west,
    panel_correlation,
    percentile_interval,
    power,
    sharpe_ratio,
    years_between,
)

N = NormalDist()


# ── growth and drawdown ──


def test_the_annualised_log_growth_of_a_doubling_over_two_years():
    years = years_between(date(2020, 1, 1), date(2022, 1, 1), days_per_year=365.25)
    assert years == pytest.approx(731 / 365.25)
    assert annualised_log_growth(100.0, 200.0, 2.0) == pytest.approx(math.log(2) / 2)
    assert annualised_log_growth(100.0, 50.0, 1.0) == pytest.approx(math.log(0.5))
    for bad in ((0.0, 1.0, 1.0), (1.0, 0.0, 1.0), (1.0, 2.0, 0.0)):
        assert annualised_log_growth(*bad) is None


def test_drawdowns_run_from_the_peak():
    values = [100, 120, 90, 130, 117]
    assert drawdowns(values).tolist() == pytest.approx([0, 0, -0.25, 0, -0.1])
    assert max_drawdown(values) == pytest.approx(-0.25)
    assert max_drawdown([1, 2, 3]) == 0
    with pytest.raises(ValueError, match="positive values"):
        drawdowns([1, 0])
    with pytest.raises(ValueError, match="positive values"):
        drawdowns([])


def test_the_breach_probability_counts_paths_at_or_past_the_limit():
    assert breach_probability([-0.1, -0.2, -0.35, -0.5], 0.2) == 0.75
    assert breach_probability([-0.1], 0.2) == 0
    for limit in (0, 1, -0.1):
        with pytest.raises(ValueError, match="strictly between 0 and 1"):
            breach_probability([-0.1], limit)
    with pytest.raises(ValueError, match="at least one path"):
        breach_probability([], 0.2)


# ── the block bootstrap ──


def test_the_bootstrap_is_seeded_and_its_blocks_are_contiguous():
    first = block_bootstrap_indices(10, block=3, n_boot=50, seed=7)
    assert first.shape == (50, 10)
    assert np.array_equal(first, block_bootstrap_indices(10, block=3, n_boot=50, seed=7))
    assert not np.array_equal(first, block_bootstrap_indices(10, block=3, n_boot=50, seed=8))
    assert first.min() >= 0
    assert first.max() <= 9
    # Within each block of three the indices step by one.
    blocks = first[:, :9].reshape(50, 3, 3)
    assert np.all(np.diff(blocks, axis=2) == 1)
    whole = block_bootstrap_indices(4, block=10, n_boot=3, seed=1)
    assert whole.tolist() == [[0, 1, 2, 3]] * 3  # a block longer than the series is the series
    with pytest.raises(ValueError, match="must be positive"):
        block_bootstrap_indices(0, block=1, n_boot=1, seed=1)


def test_the_iid_bootstrap_variance_of_the_mean_matches_its_closed_form():
    values = np.array([1.0, 4.0, 2.0, 8.0, 5.0, 3.0, 9.0, 6.0])
    means = bootstrap(values, np.mean, block=1, n_boot=40000, seed=11)
    # With block 1 the bootstrap mean has expectation x-bar and variance sigma_pop^2 / n.
    assert means.mean() == pytest.approx(values.mean(), abs=0.03)
    assert means.var() == pytest.approx(values.var() / values.size, rel=0.03)


def test_a_percentile_interval_has_level_one_minus_two_alpha_over_the_tests():
    draws = np.arange(1, 1001, dtype=float)
    low, high = percentile_interval(draws, alpha=0.05, tests=1)
    assert (low, high) == pytest.approx((np.quantile(draws, 0.05), np.quantile(draws, 0.95)))
    narrow = percentile_interval(draws, alpha=0.05, tests=5)
    assert narrow == pytest.approx((np.quantile(draws, 0.01), np.quantile(draws, 0.99)))
    with pytest.raises(ValueError, match="at least one draw"):
        percentile_interval([], alpha=0.05, tests=1)


# ── intervals and power ──


def test_bonferroni_widens_each_end_to_alpha_over_the_tests():
    assert critical_value(0.05, 1) == pytest.approx(N.inv_cdf(0.95))
    assert critical_value(0.05, 4) == pytest.approx(N.inv_cdf(1 - 0.0125))
    low, high = bonferroni_interval(0.02, 0.01, alpha=0.05, tests=1)
    assert (low, high) == pytest.approx((0.02 - 1.6448536 * 0.01, 0.02 + 1.6448536 * 0.01))
    for alpha, tests in ((0, 1), (0.5, 1), (0.05, 0)):
        with pytest.raises(ValueError, match=r"alpha must lie|number of tests"):
            critical_value(alpha, tests)
    with pytest.raises(ValueError, match="standard error"):
        bonferroni_interval(0, -1, alpha=0.05, tests=1)


def test_power_and_the_detectable_effect_are_inverses():
    se = 0.01
    mde = detectable_effect(se, alpha=0.05, tests=1, target_power=0.8)
    # (z_0.95 + z_0.80) * se = (1.6449 + 0.8416) * 0.01
    assert mde == pytest.approx((1.6448536 + 0.8416212) * se)
    assert power(mde, se, alpha=0.05, tests=1) == pytest.approx(0.8)
    assert power(0.0, se, alpha=0.05, tests=1) == pytest.approx(0.05)
    assert power(0.02, se, alpha=0.05, tests=1) == pytest.approx(N.cdf(2 - 1.6448536))
    with pytest.raises(ValueError, match="positive standard error"):
        power(0.01, 0, alpha=0.05, tests=1)
    with pytest.raises(ValueError, match="strictly between 0 and 1"):
        detectable_effect(se, alpha=0.05, tests=1, target_power=1)


# ── dependence ──


def test_newey_west_matches_a_hand_computation():
    # x = 1..4: gamma_0 = 1.25, gamma_1 = 0.3125; with one lag the long-run variance is
    # 1.25 + 2 * (1 - 1/2) * 0.3125 = 1.5625 and the standard error sqrt(1.5625 / 4) = 0.625.
    estimate = newey_west([1, 2, 3, 4], lags=1, alpha=0.05, tests=1, min_points=2)
    assert estimate.mean == 2.5
    assert estimate.se == pytest.approx(0.625)
    assert estimate.ci == pytest.approx((2.5 - 1.6448536 * 0.625, 2.5 + 1.6448536 * 0.625))
    assert newey_west([1], lags=1, alpha=0.05, tests=1, min_points=2).mean is None


def test_fama_macbeth_recovers_a_planted_slope_in_every_period():
    signals = [[1, 2, 3, 4], [0, 1, 0, 1], [2, 2, 3, None]]
    slopes = [0.5, -1.0, 2.0]
    outcomes = [
        [3 + slope * s if s is not None else None for s in row]
        for slope, row in zip(slopes, signals, strict=True)
    ]
    result = fama_macbeth(outcomes, signals, lags=1, alpha=0.05, tests=1, min_periods=2)
    assert result.slopes == pytest.approx(tuple(slopes))
    assert result.estimate.mean == pytest.approx(sum(slopes) / 3)
    with pytest.raises(ValueError, match="same periods"):
        fama_macbeth(outcomes, signals[:2], lags=1, alpha=0.05, tests=1, min_periods=2)


def test_a_cross_sectional_slope_needs_three_names_and_spread():
    assert cross_sectional_slope([1, 2], [1, 2]) is None
    assert cross_sectional_slope([1, 2, 3], [5, 5, 5]) is None
    assert cross_sectional_slope([2, 4, 6], [1, 2, 3]) == pytest.approx(2)


def test_effective_n_follows_its_closed_form():
    assert effective_n(188, 0.2) == pytest.approx(188 / (1 + 187 * 0.2))  # about five
    assert effective_n(188, 0.05) == pytest.approx(188 / (1 + 187 * 0.05))  # about eighteen
    assert effective_n(10, 0) == 10
    assert effective_n(10, 1) == 1
    assert effective_n(1, 0.5) == 1
    with pytest.raises(ValueError, match="at least 1"):
        effective_n(0, 0.1)
    with pytest.raises(ValueError, match="rho must lie"):
        effective_n(3, -0.6)


# Columns of a Hadamard matrix: zero mean, mutually orthogonal, equal norm.
H = np.array(
    [
        [1, 1, 1, 1, 1, 1, 1, 1],
        [1, -1, 1, -1, 1, -1, 1, -1],
        [1, 1, -1, -1, 1, 1, -1, -1],
        [1, -1, -1, 1, 1, -1, -1, 1],
        [1, 1, 1, 1, -1, -1, -1, -1],
        [1, -1, 1, -1, -1, 1, -1, 1],
        [1, 1, -1, -1, -1, -1, 1, 1],
        [1, -1, -1, 1, -1, 1, 1, -1],
    ],
    dtype=float,
)


def test_rho_estimated_from_a_panel_matches_its_construction():
    common, noise = H[:, 1], H[:, 2:6]
    identical = np.column_stack([common] * 4)
    assert panel_correlation(identical.tolist(), min_overlap=3) == pytest.approx(1.0)
    assert panel_correlation(noise.tolist(), min_overlap=3) == pytest.approx(0.0)
    # x_i = f + e_i with f, e_i orthogonal and of equal variance: every pair correlates 1/2.
    equi = noise + common[:, None]
    rho = panel_correlation(equi.tolist(), min_overlap=3)
    assert rho == pytest.approx(0.5)
    assert effective_n(4, rho) == pytest.approx(1.6)
    sparse = equi.tolist()
    sparse[0][0] = None
    assert panel_correlation(sparse, min_overlap=8) == pytest.approx(0.5)  # pairs without 0
    assert panel_correlation([[1.0], [2.0]], min_overlap=1) is None
    flat = [[1.0, 2.0], [1.0, 3.0], [1.0, 1.0]]
    assert panel_correlation(flat, min_overlap=2) is None


# ── the Sharpe ratio across trials ──


def test_the_expected_maximum_sharpe_and_the_deflated_sharpe_follow_the_paper():
    variance = 0.5
    trials = 100
    expected = math.sqrt(variance) * (
        (1 - EULER_MASCHERONI) * N.inv_cdf(1 - 1 / trials)
        + EULER_MASCHERONI * N.inv_cdf(1 - 1 / (trials * math.e))
    )
    assert expected_maximum_sharpe(trials, variance) == pytest.approx(expected)
    assert expected_maximum_sharpe(1, variance) == 0
    result = deflated_sharpe(
        0.3, observations=120, skew=-0.5, kurtosis=4.0, trials=trials, sharpe_variance=variance
    )
    statistic = (0.3 - expected) * math.sqrt(119) / math.sqrt(1 + 0.5 * 0.3 + 3 / 4 * 0.09)
    assert result.probability == pytest.approx(N.cdf(statistic))
    single = deflated_sharpe(0.1, observations=101, skew=0, kurtosis=3, trials=1, sharpe_variance=0)
    # One trial: the probabilistic Sharpe ratio against zero.
    assert single.probability == pytest.approx(N.cdf(0.1 * 10 / math.sqrt(1 + 0.5 * 0.01)))
    with pytest.raises(ValueError, match="two observations"):
        deflated_sharpe(0.1, observations=1, skew=0, kurtosis=3, trials=1, sharpe_variance=0)
    with pytest.raises(ValueError, match="non-positive variance"):
        deflated_sharpe(2.0, observations=10, skew=5, kurtosis=3, trials=1, sharpe_variance=0)
    with pytest.raises(ValueError, match="at least 1"):
        expected_maximum_sharpe(0, 1)


def test_sharpe_and_moments_of_a_series():
    returns = [0.01, 0.03, -0.01, 0.05]
    assert sharpe_ratio(returns) == pytest.approx(np.mean(returns) / np.std(returns, ddof=1))
    assert sharpe_ratio([0.01]) is None
    assert sharpe_ratio([0.01, 0.01]) is None
    skew, kurt = moments([-1.0, 1.0])
    assert (skew, kurt) == pytest.approx((0.0, 1.0))
    assert moments([1.0, 1.0]) is None


# ── the break-even cost multiplier ──


def test_the_breakeven_multiplier_of_a_linear_effect_matches_its_closed_form():
    gross, drag, theta = 0.05, 0.01, 0.02
    found = breakeven_multiplier(lambda k: gross - k * drag, theta=theta, upper=20, tolerance=1e-6)
    assert found.status == "found"
    assert found.multiplier == pytest.approx((gross - theta) / drag, abs=1e-5)  # 3.0
    below = breakeven_multiplier(lambda k: 0.01 - k, theta=0.02, upper=20, tolerance=1e-6)
    assert (below.multiplier, below.status) == (None, "below_theta_without_costs")
    above = breakeven_multiplier(lambda _: 1.0, theta=0.02, upper=20, tolerance=1e-6)
    assert (above.multiplier, above.status, above.bound) == (None, "above_search", 20)
    with pytest.raises(ValueError, match="must be positive"):
        breakeven_multiplier(lambda _: 1.0, theta=0.02, upper=0, tolerance=1e-6)
