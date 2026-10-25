"""Risk budgeting against closed forms (a diagonal covariance, two assets at equal budgets), an
independent Newton solve and an independent library (skfolio's ``RiskBudgeting``, a convex
solver; test-only, never imported by the package), with the still-valid cases of the legacy
optimiser's risk-parity tests (``legacy/old/ObliskIQ/tests/test_opportunistic_optimizer.py``
@68280e1: its sample data, equal contributions, a lower weight for the more volatile asset, one
asset, ten assets, a near-zero variance, weights non-negative and finite for any positive-definite
covariance). Its other methods (equal weight, Kelly, maximum Sortino, Black-Litterman), the crisis
switch, leverage and the position cap are not ported (``legacy/MANIFEST.md``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from obliskiq.config import ObliskConfig
from obliskiq.engines.core.weights import environment_budgets, risk_roles
from obliskiq.quant.risk_budget import covariance_from, risk_budget_weights, risk_contributions

TOLERANCE = 1e-12
SWEEPS = 10_000


def solve(cov, budgets):
    return risk_budget_weights(
        np.asarray(cov, dtype=float), budgets, tolerance=TOLERANCE, max_iterations=SWEEPS
    )


def newton(cov: np.ndarray, budgets: np.ndarray) -> np.ndarray:
    """An independent solve: Newton's method on the log-barrier problem, with backtracking."""
    y = np.ones(budgets.size)

    def objective(z: np.ndarray) -> float:
        return float(z @ cov @ z / 2 - budgets @ np.log(z))

    for _ in range(200):
        gradient = cov @ y - budgets / y
        hessian = cov + np.diag(budgets / y**2)
        step = np.linalg.solve(hessian, gradient)
        size = 1.0
        while np.any(y - size * step <= 0) or objective(y - size * step) > objective(y):
            size /= 2
        y = y - size * step
        if np.abs(step).max() < 1e-14:
            break
    return y / y.sum()


def test_a_diagonal_covariance_gives_the_square_root_of_the_budget_over_volatility():
    vols = np.array([0.04, 0.10, 0.16, 0.20])
    budgets = np.array([0.1, 0.2, 0.3, 0.4])
    solution = solve(np.diag(vols**2), budgets.tolist())
    expected = np.sqrt(budgets) / vols
    assert solution.converged
    assert solution.weights == pytest.approx((expected / expected.sum()).tolist(), rel=1e-9)
    assert solution.contributions == pytest.approx(budgets.tolist(), rel=1e-9)


@pytest.mark.parametrize("rho", [-0.6, 0.0, 0.3, 0.9])
def test_two_assets_at_equal_budgets_hold_inverse_volatility_at_any_correlation(rho):
    # 16 % and 5 %: inverse volatility puts 5 / 21 in the first (about 24 %, DECISIONS D60).
    cov = covariance_from([0.16, 0.05], [[1, rho], [rho, 1]])
    solution = solve(cov, [0.5, 0.5])
    assert solution.weights == pytest.approx([0.05 / 0.21, 0.16 / 0.21], rel=1e-9)
    assert solution.contributions == pytest.approx([0.5, 0.5], rel=1e-9)


def test_the_more_volatile_asset_gets_the_lower_weight_and_every_contribution_is_equal():
    cov = covariance_from([0.10, 0.20, 0.30], [[1, 0.2, 0.1], [0.2, 1, 0.4], [0.1, 0.4, 1]])
    solution = solve(cov, [1 / 3] * 3)
    first, second, third = solution.weights
    assert first > second > third > 0
    assert sum(solution.weights) == pytest.approx(1)
    assert solution.contributions == pytest.approx([1 / 3] * 3, rel=1e-9)


@settings(max_examples=60, deadline=None)
@given(
    n=st.integers(min_value=2, max_value=7),
    seed=st.integers(min_value=0, max_value=10_000),
)
def test_contributions_match_the_budgets_and_an_independent_solver(n, seed):
    rng = np.random.default_rng(seed)
    factors = rng.normal(size=(n, n))
    cov = factors @ factors.T + np.eye(n) * 0.05
    budgets = rng.uniform(0.05, 1.0, size=n)
    budgets /= budgets.sum()
    solution = solve(cov, budgets.tolist())
    weights = np.array(solution.weights)
    assert solution.converged
    assert np.all(weights > 0)
    assert weights.sum() == pytest.approx(1)
    assert solution.contributions == pytest.approx(budgets.tolist(), rel=1e-7, abs=1e-10)
    assert weights == pytest.approx(newton(cov, budgets), rel=1e-6, abs=1e-9)


def test_a_run_out_of_sweeps_is_reported_unconverged():
    cov = covariance_from([0.10, 0.20, 0.30], [[1, 0.5, 0.1], [0.5, 1, 0.4], [0.1, 0.4, 1]])
    solution = risk_budget_weights(cov, [0.2, 0.3, 0.5], tolerance=TOLERANCE, max_iterations=1)
    assert (solution.converged, solution.iterations) == (False, 1)


@pytest.mark.parametrize(
    ("cov", "budgets", "message"),
    [
        (np.eye(2), [0.5, 0.3, 0.2], "need an 3x3"),
        (np.array([[1.0, 2.0], [2.0, 1.0]]), [0.5, 0.5], "positive definite"),
        (np.array([[1.0, 0.1], [0.2, 1.0]]), [0.5, 0.5], "symmetric"),
        (np.eye(2), [0.6, 0.6], "sum to 1"),
        (np.eye(2), [1.5, -0.5], "positive"),
    ],
)
def test_bad_inputs_are_refused(cov, budgets, message):
    with pytest.raises(ValueError, match=message):
        risk_budget_weights(cov, budgets, tolerance=TOLERANCE, max_iterations=SWEEPS)


def test_the_solver_needs_a_tolerance_and_a_sweep():
    with pytest.raises(ValueError, match="tolerance"):
        risk_budget_weights(np.eye(2), [0.5, 0.5], tolerance=0, max_iterations=SWEEPS)
    with pytest.raises(ValueError, match="at least one"):
        risk_budget_weights(np.eye(2), [0.5, 0.5], tolerance=TOLERANCE, max_iterations=0)


@pytest.mark.parametrize(
    ("vols", "corr", "message"),
    [
        ([0.1], [[1, 0], [0, 1]], "need an 1x1"),
        ([0.1, -0.2], [[1, 0], [0, 1]], "positive and finite"),
        ([0.1, 0.2], [[1, 0.3], [0.2, 1]], "symmetric"),
        ([0.1, 0.2], [[1, 1.2], [1.2, 1]], "between -1 and 1"),
        ([0.1, 0.2, 0.3], [[1, 0.9, -0.9], [0.9, 1, 0.9], [-0.9, 0.9, 1]], "positive definite"),
    ],
)
def test_a_covariance_needs_a_valid_correlation_matrix(vols, corr, message):
    with pytest.raises(ValueError, match=message):
        covariance_from(vols, corr)


def test_covariance_is_d_r_d_and_contributions_need_variance():
    cov = covariance_from([0.1, 0.2], [[1, 0.5], [0.5, 1]])
    assert cov.ravel().tolist() == pytest.approx([0.01, 0.01, 0.01, 0.04])
    assert sum(risk_contributions([0.3, 0.7], cov)) == pytest.approx(1)
    with pytest.raises(ValueError, match="no variance"):
        risk_contributions([0.0, 0.0], cov)
    assert math.isclose(risk_contributions([1.0, 0.0], cov)[0], 1.0)


# ── an independent library: skfolio ──


def skfolio_weights(cov: np.ndarray, budgets: np.ndarray) -> np.ndarray:
    """skfolio's risk budgeting on returns whose sample covariance is ``cov`` exactly (the
    weights do not depend on the covariance's scale, so its estimator's denominator is moot)."""
    from skfolio import RiskMeasure
    from skfolio.optimization import RiskBudgeting

    rng = np.random.default_rng(0)
    n = budgets.size
    draws = rng.normal(size=(50 * n, n))
    draws -= draws.mean(axis=0)
    draws = draws @ np.linalg.inv(np.linalg.cholesky(np.cov(draws.T))).T
    returns = draws @ np.linalg.cholesky(cov).T
    model = RiskBudgeting(risk_measure=RiskMeasure.VARIANCE, risk_budget=budgets)
    model.fit(returns)
    return np.asarray(model.weights_, dtype=float)


@pytest.mark.parametrize("variant", ["A", "C"])
def test_the_cores_risk_balances_match_skfolio(
    variant: str, shipped_settings: ObliskConfig
) -> None:
    core = shipped_settings.core
    roles = risk_roles(core.environments)
    budgets = core.variants[variant].budgets  # type: ignore[union-attr]
    shares = environment_budgets(core.environments, budgets)
    vols, corr = core.risk.matrix(roles)
    cov = covariance_from(vols, corr)
    b = np.array([float(shares[role]) for role in roles])
    ours = np.array(solve(cov, b.tolist()).weights)
    assert ours == pytest.approx(skfolio_weights(cov, b), abs=1e-5)


@pytest.mark.parametrize("seed", range(8))
def test_random_budgets_match_skfolio(seed: int) -> None:
    rng = np.random.default_rng(seed)
    n = 2 + seed % 6
    factors = rng.normal(size=(n, n))
    cov = factors @ factors.T + np.eye(n) * 0.05
    budgets = rng.uniform(0.05, 1.0, size=n)
    budgets /= budgets.sum()
    ours = np.array(solve(cov, budgets.tolist()).weights)
    assert ours == pytest.approx(skfolio_weights(cov, budgets), abs=1e-5)


# ── the legacy optimiser's still-valid cases ──


def annualised(raw_returns: np.ndarray) -> np.ndarray:
    """The legacy helper's covariance: daily returns' sample covariance times 252."""
    return np.cov(raw_returns.T) * 252


def test_the_legacy_sample_data_get_equal_risk_and_valid_weights() -> None:
    np.random.seed(42)  # the legacy fixture's draws, reproduced
    tech = np.random.normal(0.15 / 252, 0.30 / np.sqrt(252), 100)
    spy = np.random.normal(0.10 / 252, 0.15 / np.sqrt(252), 100)
    agg = np.random.normal(0.05 / 252, 0.08 / np.sqrt(252), 100)
    cov = annualised(np.column_stack([tech, spy, agg]))
    solution = solve(cov, [1 / 3] * 3)
    weights = np.array(solution.weights)
    assert len(weights) == 3
    assert np.all(weights >= 0)
    assert np.all(np.isfinite(weights))
    assert weights.sum() == pytest.approx(1)
    # The legacy test allowed a spread of 0.15 between contributions; the fixed solver has none.
    assert solution.contributions == pytest.approx([1 / 3] * 3, rel=1e-9)
    assert weights[0] < weights[1] < weights[2]  # the most volatile holds the least


def test_one_asset_holds_everything() -> None:
    solution = solve(np.array([[0.04]]), [1.0])
    assert solution.weights == (1.0,)
    assert solution.contributions == (1.0,)


def test_ten_assets_from_the_legacy_draws_balance_their_risk() -> None:
    np.random.seed(42)
    cov = annualised(np.random.normal(0.001, 0.02, size=(100, 10)))
    solution = solve(cov, [0.1] * 10)
    assert len(solution.weights) == 10
    assert sum(solution.weights) == pytest.approx(1)
    assert solution.contributions == pytest.approx([0.1] * 10, rel=1e-8)


def test_a_near_zero_variance_asset_gets_a_finite_weight() -> None:
    np.random.seed(42)
    low = np.random.normal(0.001, 0.0001, size=(100, 1))
    normal = np.random.normal(0.001, 0.02, size=(100, 1))
    cov = annualised(np.column_stack([low, normal])) + np.eye(2) * 1e-8
    weights = solve(cov, [0.5, 0.5]).weights
    assert all(math.isfinite(w) and w > 0 for w in weights)
    assert weights[0] > weights[1]  # the quiet asset carries most of the money


@settings(max_examples=50, deadline=None)
@given(
    n=st.integers(min_value=2, max_value=10),
    seed=st.integers(min_value=0, max_value=1000),
)
def test_weights_are_never_negative_nor_invalid_for_the_legacy_covariances(
    n: int, seed: int
) -> None:
    rng = np.random.default_rng(seed)
    factors = rng.normal(size=(n, n))
    cov = factors @ factors.T + np.eye(n) * 0.01  # the legacy property tests' construction
    weights = np.array(solve(cov, [1 / n] * n).weights)
    assert np.all(weights >= 0)
    assert np.all(np.isfinite(weights))
    assert weights.sum() == pytest.approx(1)
