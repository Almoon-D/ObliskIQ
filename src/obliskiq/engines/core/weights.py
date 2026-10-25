"""The core's static weights: risk budgets by environment, solved once on the declared covariance
(MASTER_PLAN §5.4, DECISIONS D80).

Each environment's risk budget is split equally among its roles, a role in two environments
taking a share of each; the weights whose shares of variance equal those budgets are the static
risk balance (``quant.risk_budget``). The research harness and the live engine both call
:func:`risk_balanced`, so the weights a test priced are the weights the account holds.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Final

from ...errors import CoreError
from ...quant.risk_budget import covariance_from, risk_budget_weights

ZERO = Decimal(0)
ONE = Decimal(1)
#: Static weights are kept to a millionth.
WEIGHT_QUANTUM: Final = Decimal("0.000001")


def environment_budgets(
    environments: Mapping[str, Sequence[str]], budgets: Mapping[str, Decimal]
) -> dict[str, Decimal]:
    """Each role's share of risk: every environment's budget split equally among its roles,
    a role in two environments taking a share of each."""
    shares: dict[str, Decimal] = {}
    for environment, roles in environments.items():
        for role in roles:
            shares[role] = shares.get(role, ZERO) + budgets[environment] / len(roles)
    return shares


def risk_matrix(
    volatilities: Mapping[str, Decimal],
    correlations: Sequence[tuple[str, str, Decimal]],
    roles: Sequence[str],
) -> tuple[list[float], list[list[float]]]:
    """Volatilities and the correlation matrix in the order of ``roles``, from the declared
    volatility of each role and the correlation of each pair."""
    rho = {frozenset((a, b)): float(c) for a, b, c in correlations}
    vols = [float(volatilities[role]) for role in roles]
    corr = [[1.0 if a == b else rho[frozenset((a, b))] for b in roles] for a in roles]
    return vols, corr


def risk_balanced(
    roles: Sequence[str],
    shares: Mapping[str, Decimal],
    volatilities: Sequence[float],
    correlations: Sequence[Sequence[float]],
    *,
    tolerance: float,
    max_iterations: int,
    name: str,
) -> dict[str, Decimal]:
    """The weights, by role in the order of ``roles``, whose variance shares equal ``shares``
    on the covariance the volatilities and correlations declare; they sum to exactly 1."""
    solution = risk_budget_weights(
        covariance_from(volatilities, correlations),
        [float(shares[role]) for role in roles],
        tolerance=tolerance,
        max_iterations=max_iterations,
    )
    if not solution.converged:
        raise CoreError(
            f"strategy {name}: the risk budget did not converge in {solution.iterations} sweeps"
        )
    weights = [Decimal(repr(w)).quantize(WEIGHT_QUANTUM) for w in solution.weights]
    # The rounding remainder goes to the largest weight so that the weights sum to exactly 1.
    largest = max(range(len(weights)), key=lambda i: weights[i])
    weights[largest] += ONE - sum(weights, ZERO)
    return dict(zip(roles, weights, strict=True))
