"""Risk budgeting: long-only weights whose risk contributions match given budgets
(MASTER_PLAN §5.4).

The share of portfolio variance an asset carries is ``w_i (Cw)_i / w'Cw`` for the covariance
matrix ``C``. Weights whose shares equal budgets ``b`` (positive, summing to 1) solve the convex
problem

    minimise  y'Cy / 2 - sum_i b_i ln y_i   over y > 0,   then  w = y / sum_i y_i,

whose first-order condition ``y_i (Cy)_i = b_i`` makes every contribution proportional to its
budget (Roncalli 2013, *Introduction to Risk Parity and Budgeting*, ch. 2). It is solved by
cyclical coordinate descent: each ``y_i`` in turn is the positive root of
``C_ii y_i^2 + c_i y_i - b_i = 0`` with ``c_i = sum_{j != i} C_ij y_j`` (Griveau-Billion, Richard
and Roncalli 2013), which converges for any positive-definite ``C``. This fixes the legacy
``_risk_parity``, which compared absolute contributions with ``1/n`` without dividing by the
portfolio's variance.

Closed forms the tests hold it to: a diagonal ``C`` gives ``w_i`` proportional to
``sqrt(b_i) / sigma_i`` (inverse volatility at equal budgets), and two assets at equal budgets get
inverse-volatility weights at any correlation.

Derived from the risk-parity optimiser of Almoon-D/Inversor-Inteligente
(``modules/analytics/opportunistic_optimizer.py`` @68280e1, Apache License 2.0,
https://www.apache.org/licenses/LICENSE-2.0), modified and used here under AGPL-3.0-or-later as
the Apache License permits.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np

#: The 4 of the quadratic formula's discriminant, b^2 - 4ac.
QUADRATIC_FOUR: Final = 4


@dataclass(frozen=True, slots=True)
class RiskBudgetSolution:
    """Weights summing to 1, each asset's share of variance, and how the solver ended."""

    weights: tuple[float, ...]
    contributions: tuple[float, ...]
    iterations: int
    converged: bool


def covariance_from(
    volatilities: Sequence[float], correlations: Sequence[Sequence[float]]
) -> np.ndarray:
    """``C = D R D`` from volatilities and a correlation matrix; refuses one that is not a
    symmetric, unit-diagonal, positive-definite correlation matrix."""
    sigma = np.asarray(volatilities, dtype=float)
    rho = np.asarray(correlations, dtype=float)
    n = sigma.size
    if n == 0 or rho.shape != (n, n):
        raise ValueError(f"{n} volatilities need an {n}x{n} correlation matrix, got {rho.shape}")
    if np.any(~np.isfinite(sigma)) or np.any(sigma <= 0):
        raise ValueError("every volatility must be positive and finite")
    if not np.allclose(rho, rho.T) or not np.allclose(np.diag(rho), 1.0):
        raise ValueError("a correlation matrix is symmetric with ones on its diagonal")
    if np.any(np.abs(rho) > 1):
        raise ValueError("a correlation lies between -1 and 1")
    if np.linalg.eigvalsh(rho).min() <= 0:
        raise ValueError("the correlation matrix is not positive definite")
    return rho * np.outer(sigma, sigma)


def risk_contributions(weights: Sequence[float], covariance: np.ndarray) -> tuple[float, ...]:
    """Each asset's share of portfolio variance, ``w_i (Cw)_i / w'Cw``."""
    w = np.asarray(weights, dtype=float)
    marginal = covariance @ w
    variance = float(w @ marginal)
    if variance <= 0:
        raise ValueError("a portfolio with no variance has no risk contributions")
    return tuple(float(x) for x in w * marginal / variance)


def risk_budget_weights(
    covariance: np.ndarray,
    budgets: Sequence[float],
    *,
    tolerance: float,
    max_iterations: int,
) -> RiskBudgetSolution:
    """Long-only, fully invested weights whose variance shares equal ``budgets``.

    ``tolerance`` bounds the largest change of a scaled weight in one sweep at convergence; a
    run that reaches ``max_iterations`` sweeps first is returned with ``converged`` false.
    """
    cov = np.asarray(covariance, dtype=float)
    b = np.asarray(budgets, dtype=float)
    n = b.size
    if n == 0 or cov.shape != (n, n):
        raise ValueError(f"{n} budgets need an {n}x{n} covariance matrix, got {cov.shape}")
    if not np.allclose(cov, cov.T) or np.linalg.eigvalsh(cov).min() <= 0:
        raise ValueError("the covariance matrix must be symmetric and positive definite")
    if np.any(~np.isfinite(b)) or np.any(b <= 0) or not math.isclose(float(b.sum()), 1.0):
        raise ValueError("risk budgets are positive and sum to 1")
    if tolerance <= 0 or max_iterations < 1:
        raise ValueError("the tolerance must be positive and the iterations at least one")
    diagonal = np.diag(cov)
    y = np.sqrt(b) / np.sqrt(diagonal)  # the diagonal solution starts the descent
    converged = False
    sweeps = 0
    while sweeps < max_iterations:
        sweeps += 1
        largest = 0.0
        for i in range(n):
            others = float(cov[i] @ y) - diagonal[i] * y[i]
            root = (-others + math.sqrt(others * others + QUADRATIC_FOUR * diagonal[i] * b[i])) / (
                2 * diagonal[i]
            )
            largest = max(largest, abs(root - y[i]) / root)
            y[i] = root
        if largest <= tolerance:
            converged = True
            break
    weights = y / y.sum()
    return RiskBudgetSolution(
        weights=tuple(float(x) for x in weights),
        contributions=risk_contributions(weights.tolist(), cov),
        iterations=sweeps,
        converged=converged,
    )
