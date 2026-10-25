"""Risk budgeting against skfolio's ``RiskBudgeting``, a library we do not ship (a test-only
dependency): on one set of simulated returns, the weights of our coordinate descent and the
library's convex program agree, so the registered variants' risk balance is not an artefact of
our solver (MASTER_PLAN §5.4; DECISIONS D82, D140).

skfolio solves a conic program to its solver's tolerance, about 1e-5 in a weight; ours is exact
to 1e-12, so the comparison allows the library's looseness.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import numpy as np
import pytest
from skfolio import RiskMeasure
from skfolio.optimization import RiskBudgeting

from obliskiq.quant.risk_budget import risk_budget_weights

#: What the library's solver is accurate to in a weight, with margin.
LIBRARY_TOLERANCE = 1e-4


def simulated(n: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    factors = rng.normal(size=(n, n))
    cov = factors @ factors.T * 1e-4 + np.eye(n) * 1e-4
    return rng.multivariate_normal(np.zeros(n), cov, size=5000)


@pytest.mark.parametrize("n", [2, 4, 8])
@pytest.mark.parametrize("seed", [1, 2])
def test_the_weights_agree_with_skfolios_risk_budgeting(n: int, seed: int) -> None:
    returns = simulated(n, seed)
    budgets = np.random.default_rng(seed + 100).uniform(0.05, 1.0, size=n)
    budgets /= budgets.sum()
    ours = risk_budget_weights(
        np.cov(returns, rowvar=False, bias=True),
        budgets.tolist(),
        tolerance=1e-12,
        max_iterations=10_000,
    )
    library = RiskBudgeting(risk_measure=RiskMeasure.STANDARD_DEVIATION, risk_budget=budgets)
    library.fit(returns)
    assert ours.converged
    assert np.asarray(library.weights_) == pytest.approx(ours.weights, abs=LIBRARY_TOLERANCE)


def test_equal_budgets_are_the_equal_risk_contribution_portfolio() -> None:
    returns = simulated(5, 7)
    ours = risk_budget_weights(
        np.cov(returns, rowvar=False, bias=True), [0.2] * 5, tolerance=1e-12, max_iterations=10_000
    )
    library = RiskBudgeting(risk_measure=RiskMeasure.STANDARD_DEVIATION)  # equal by default
    library.fit(returns)
    assert np.asarray(library.weights_) == pytest.approx(ours.weights, abs=LIBRARY_TOLERANCE)
