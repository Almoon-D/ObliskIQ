"""The core's static weights by role (MASTER_PLAN §5.4, DECISIONS D80).

- **Risk balance** (A, B, C): each environment's risk budget is split equally among its roles, a
  role in two environments taking a share of each (:func:`environment_budgets`); the weights whose
  shares of variance equal those budgets are solved once on the **declared** long-run covariance
  (``quant.risk_budget``) and held fixed. They are kept to a millionth, and the rounding remainder
  goes to the largest weight so that they sum to exactly 1.
- **Equity overlay** (D): one equity role at a fraction, the rest in the cash role.
- **Banded weights** (the 60/40) and **buy and hold** (the equity reference) state theirs.

The research harness builds a pre-registration's strategies with these functions, and the live
engine builds the owner's variant with them, so a variant's weights are the same in both.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from ...quant.risk_budget import covariance_from, risk_budget_weights

ZERO = Decimal(0)
ONE = Decimal(1)
#: Static weights are kept to a millionth.
STATIC_QUANTUM: Final = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class RiskBalance:
    """A risk balance's weights by role, and how its solver ended."""

    weights: dict[str, Decimal]
    converged: bool
    iterations: int


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


def risk_roles(environments: Mapping[str, Sequence[str]]) -> list[str]:
    """The roles of the environments, in their first order of appearance (the solver's order)."""
    return list(dict.fromkeys(role for basket in environments.values() for role in basket))


def risk_balance(
    environments: Mapping[str, Sequence[str]],
    budgets: Mapping[str, Decimal],
    volatilities: Sequence[float],
    correlations: Sequence[Sequence[float]],
    *,
    tolerance: float,
    max_iterations: int,
) -> RiskBalance:
    """The static risk balance of ``budgets`` by environment, on the declared covariance whose
    volatilities and correlations are in :func:`risk_roles` order."""
    roles = risk_roles(environments)
    shares = environment_budgets(environments, budgets)
    solution = risk_budget_weights(
        covariance_from(volatilities, correlations),
        [float(shares[role]) for role in roles],
        tolerance=tolerance,
        max_iterations=max_iterations,
    )
    weights = [Decimal(repr(w)).quantize(STATIC_QUANTUM) for w in solution.weights]
    # The rounding remainder goes to the largest weight so that the weights sum to exactly 1.
    largest = max(range(len(weights)), key=lambda i: weights[i])
    weights[largest] += ONE - sum(weights, ZERO)
    return RiskBalance(
        weights=dict(zip(roles, weights, strict=True)),
        converged=solution.converged,
        iterations=solution.iterations,
    )


def equity_overlay(equity: str, fraction: Decimal, cash_role: str) -> dict[str, Decimal]:
    """One equity role at ``fraction``, the rest in the cash role."""
    return {equity: fraction, cash_role: ONE - fraction}
