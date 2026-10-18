"""quant/covariance.py: weights, the second moment, Ledoit-Wolf shrinkage and the prior, held to
closed forms and to an independent implementation written with plain loops from the papers'
formulas (DECISIONS D90, D92).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from obliskiq.quant.covariance import (
    CovarianceSeries,
    EstimatorRule,
    check_target,
    effective_observations,
    estimate,
    observation_weights,
    portfolio_volatility,
    required,
)

TARGET = np.array([[4.0e-4, 1.0e-4], [1.0e-4, 2.0e-4]])


def rule(**overrides: object) -> EstimatorRule:
    fields: dict[str, object] = {
        "half_life": 10.0,
        "window": None,
        "ewma_share": 1.0,
        "shrinkage": False,
        "prior_observations": 0.0,
    }
    fields.update(overrides)
    return EstimatorRule(**fields)  # type: ignore[arg-type]


def returns(rows: int, assets: int = 2, seed: int = 7) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.0, 0.01, size=(rows, assets))


# ── an independent implementation: plain loops over the formulas ──


def naive_weights(count: int, half_life: float | None, window: int | None, share: float):
    weights = [0.0] * count
    if share > 0 and half_life is not None:
        decay = 2.0 ** (-1.0 / half_life)
        raw = [decay ** (count - 1 - t) for t in range(count)]
        total = sum(raw)
        for t in range(count):
            weights[t] += share * raw[t] / total
    if share < 1 and window is not None:
        held = min(window, count)
        for t in range(count - held, count):
            weights[t] += (1 - share) / held
    return weights


def naive_estimate(x: np.ndarray, target: np.ndarray, r: EstimatorRule):
    count, assets = x.shape
    w = naive_weights(count, r.half_life, r.window, r.ewma_share)
    s = [[sum(w[t] * x[t][i] * x[t][j] for t in range(count)) for j in range(assets)]
         for i in range(assets)]  # fmt: skip
    n = 1.0 / sum(v * v for v in w)
    sigma = [row[:] for row in s]
    intensity = None
    if r.shrinkage:
        pi = sum(
            w[t] ** 2 * (x[t][i] * x[t][j] - s[i][j]) ** 2
            for t in range(count) for i in range(assets) for j in range(assets)
        )  # fmt: skip
        gamma = sum((target[i][j] - s[i][j]) ** 2 for i in range(assets) for j in range(assets))
        intensity = min(1.0, pi / gamma)
        sigma = [[intensity * target[i][j] + (1 - intensity) * s[i][j] for j in range(assets)]
                 for i in range(assets)]  # fmt: skip
    n0 = r.prior_observations
    final = [[(n0 * target[i][j] + n * sigma[i][j]) / (n0 + n) for j in range(assets)]
             for i in range(assets)]  # fmt: skip
    return np.array(final), n, intensity


def ledoit_wolf_equal_weights(x: np.ndarray, target: np.ndarray) -> float:
    """Ledoit and Wolf's optimal intensity for a constant target, as their 2004 paper writes it:
    kappa / T with kappa = (pi - rho) / gamma, rho = 0 for a target not estimated from the data,
    pi = sum_ij (1/T) sum_t (x_ti x_tj - s_ij)^2 and gamma = sum_ij (f_ij - s_ij)^2."""
    count = x.shape[0]
    s = x.T @ x / count
    pi = float(sum(np.sum((np.outer(row, row) - s) ** 2) for row in x)) / count
    gamma = float(np.sum((target - s) ** 2))
    return max(0.0, min(1.0, pi / gamma / count))


# ── weights ──


def test_ewma_weights_halve_every_half_life_and_sum_to_one():
    weights = observation_weights(40, rule(half_life=10.0))
    assert weights.sum() == pytest.approx(1.0)
    assert weights[-11] == pytest.approx(weights[-1] / 2)
    assert weights[-21] == pytest.approx(weights[-1] / 4)
    assert np.all(np.diff(weights) > 0)


def test_window_weights_are_equal_on_the_last_observations_and_cover_a_short_history():
    weights = observation_weights(10, rule(ewma_share=0.0, window=4, half_life=None))
    assert weights.tolist() == pytest.approx([0.0] * 6 + [0.25] * 4)
    short = observation_weights(3, rule(ewma_share=0.0, window=4, half_life=None))
    assert short.tolist() == pytest.approx([1 / 3] * 3)


def test_a_blend_mixes_the_two_and_matches_the_loops():
    blended = rule(half_life=5.0, window=8, ewma_share=0.3)
    weights = observation_weights(20, blended)
    assert weights.sum() == pytest.approx(1.0)
    assert weights.tolist() == pytest.approx(naive_weights(20, 5.0, 8, 0.3))
    with pytest.raises(ValueError, match="at least one observation"):
        observation_weights(0, blended)


def test_effective_observations_of_equal_weights_is_their_count_and_of_an_ewma_its_limit():
    assert effective_observations(np.full(25, 1 / 25)) == pytest.approx(25.0)
    decay = 0.5 ** (1 / 10)
    long_run = (1 + decay) / (1 - decay)
    assert effective_observations(observation_weights(5000, rule())) == pytest.approx(long_run)


def test_a_rule_states_its_lookback_and_refuses_what_cannot_be():
    assert rule(half_life=21.0).lookback == 63.0
    assert rule(ewma_share=0.0, window=250, half_life=None).lookback == 250.0
    assert rule(half_life=21.0, window=40, ewma_share=0.5).lookback == 63.0
    assert rule(half_life=10.0, window=261, ewma_share=0.5).lookback == 261.0
    for bad, message in [
        ({"ewma_share": 1.5}, "share of the blend"),
        ({"half_life": None}, "positive half-life"),
        ({"half_life": 0.0}, "positive half-life"),
        ({"ewma_share": 0.5, "window": None}, "window with weight"),
        ({"ewma_share": 0.5, "window": 1}, "window with weight"),
        ({"prior_observations": -1.0}, "prior's weight"),
        ({"prior_observations": math.inf}, "prior's weight"),
    ]:
        with pytest.raises(ValueError, match=message):
            rule(**bad)


# ── the estimate ──


def test_equal_weights_give_the_second_moment_about_zero():
    x = returns(30)
    found = estimate(x, TARGET, rule(ewma_share=0.0, window=30, half_life=None))
    assert found is not None
    assert found.covariance == pytest.approx(x.T @ x / 30)
    assert found.observations == pytest.approx(30.0)
    assert found.intensity is None


def test_shrinkage_with_equal_weights_is_ledoit_and_wolfs_intensity():
    x = returns(60, assets=3, seed=3)
    target = np.diag([2e-4, 1e-4, 3e-4]) + 2e-5
    found = estimate(x, target, rule(ewma_share=0.0, window=60, half_life=None, shrinkage=True))
    assert found is not None
    assert found.intensity is not None
    expected = ledoit_wolf_equal_weights(x, target)
    assert found.intensity == pytest.approx(expected)
    s = x.T @ x / 60
    assert found.covariance == pytest.approx(expected * target + (1 - expected) * s)


def test_shrinkage_is_none_when_the_sample_has_no_spread_and_whole_when_it_equals_the_target():
    same = np.tile([0.01, -0.02], (12, 1))
    found = estimate(same, TARGET, rule(shrinkage=True))
    assert found is not None
    assert found.intensity == pytest.approx(0.0)
    assert found.covariance == pytest.approx(np.outer([0.01, -0.02], [0.01, -0.02]))
    one = np.array([[0.02, 0.0]])
    exact = estimate(one, np.diag([4e-4, 1e-12]), rule(shrinkage=True))
    assert exact is not None
    assert exact.intensity == pytest.approx(0.0)
    # A target equal to the sample: any intensity gives it; the estimate is that matrix.
    series = CovarianceSeries(np.array([[0.01, 0.0], [0.0, 0.02], [0.01, 0.01]]))
    plain = series.estimate(3, TARGET, rule())
    assert plain is not None
    ties = series.estimate(3, plain.covariance, rule(shrinkage=True))
    assert ties is not None
    assert ties.intensity == 1.0
    assert ties.covariance == pytest.approx(plain.covariance)


@pytest.mark.parametrize(
    "r",
    [
        rule(),
        rule(half_life=4.0, shrinkage=True),
        rule(half_life=6.0, window=15, ewma_share=0.4, shrinkage=True, prior_observations=20.0),
        rule(ewma_share=0.0, window=9, half_life=None, prior_observations=5.0),
    ],
)
def test_every_rule_matches_the_independent_loops(r):
    x = returns(40, assets=3, seed=11)
    target = np.array([[3e-4, 1e-4, 0.0], [1e-4, 2e-4, 5e-5], [0.0, 5e-5, 1e-4]])
    found = estimate(x, target, r)
    expected, n, intensity = naive_estimate(x, target, r)
    assert found is not None
    assert found.covariance == pytest.approx(expected, rel=1e-9)
    assert found.observations == pytest.approx(n)
    assert found.intensity == (None if intensity is None else pytest.approx(intensity))


def test_the_prior_is_the_estimate_before_any_data_and_weighs_as_its_observations():
    series = CovarianceSeries(returns(20))
    before = series.estimate(0, TARGET, rule(prior_observations=10.0))
    assert before is not None
    assert before.covariance == pytest.approx(TARGET)
    assert series.estimate(0, TARGET, rule()) is None
    equal = rule(ewma_share=0.0, window=20, half_life=None, prior_observations=20.0)
    half = series.estimate(20, TARGET, equal)
    sample = series.estimate(20, TARGET, rule(ewma_share=0.0, window=20, half_life=None))
    assert half is not None
    assert sample is not None
    assert half.covariance == pytest.approx((TARGET + sample.covariance) / 2)


def test_an_estimate_is_required_where_one_must_exist():
    found = estimate(returns(3), TARGET, rule())
    assert required(found) is found
    with pytest.raises(ValueError, match="nothing to estimate from"):
        required(None)


def test_a_prefix_of_a_series_is_the_estimate_of_that_prefix():
    x = returns(50)
    series = CovarianceSeries(x)
    r = rule(half_life=7.0, window=12, ewma_share=0.5, shrinkage=True, prior_observations=3.0)
    for count in (1, 13, 50):
        part = series.estimate(count, TARGET, r)
        whole = estimate(x[:count], TARGET, r)
        assert part is not None
        assert whole is not None
        assert part.covariance == pytest.approx(whole.covariance)


def test_bad_inputs_are_refused():
    with pytest.raises(ValueError, match="two-dimensional"):
        CovarianceSeries(np.zeros(3))
    with pytest.raises(ValueError, match="finite"):
        CovarianceSeries(np.array([[np.nan, 0.0]]))
    series = CovarianceSeries(returns(5))
    with pytest.raises(ValueError, match="6 observations asked of a history of 5"):
        series.estimate(6, TARGET, rule())
    with pytest.raises(ValueError, match="2x2 target"):
        check_target(np.eye(3), 2)
    with pytest.raises(ValueError, match="symmetric"):
        check_target(np.array([[1.0, 0.5], [0.0, 1.0]]), 2)
    with pytest.raises(ValueError, match="positive definite"):
        check_target(np.array([[1.0, 1.0], [1.0, 1.0]]), 2)


def test_portfolio_volatility_annualises_the_variance_of_the_weights():
    cov = np.array([[1.0e-4, 0.0], [0.0, 4.0e-4]])
    assert portfolio_volatility(cov, np.array([1.0, 0.0]), 256) == pytest.approx(0.16)
    two = portfolio_volatility(cov, np.array([0.5, 0.5]), 256)
    assert two == pytest.approx(math.sqrt(256 * (0.25e-4 + 1e-4)))
    with pytest.raises(ValueError, match="positive variance"):
        portfolio_volatility(cov, np.array([0.0, 0.0]), 256)
    with pytest.raises(ValueError, match="positive variance"):
        portfolio_volatility(cov, np.array([1.0, 0.0]), 0)


@settings(max_examples=60, deadline=None)
@given(
    rows=st.integers(min_value=1, max_value=40),
    seed=st.integers(min_value=0, max_value=10_000),
    share=st.sampled_from([0.0, 0.5, 1.0]),
    prior=st.sampled_from([0.0, 5.0]),
)
def test_every_estimate_is_symmetric_and_positive_semidefinite(rows, seed, share, prior):
    r = rule(half_life=5.0, window=10, ewma_share=share, shrinkage=True, prior_observations=prior)
    found = estimate(returns(rows, assets=3, seed=seed), np.diag([1e-4, 2e-4, 3e-4]), r)
    assert found is not None
    assert np.allclose(found.covariance, found.covariance.T)
    assert np.linalg.eigvalsh(found.covariance).min() >= -1e-15
    assert found.intensity is not None
    assert 0 <= found.intensity <= 1
    if prior > 0:
        assert np.linalg.eigvalsh(found.covariance).min() > 0
