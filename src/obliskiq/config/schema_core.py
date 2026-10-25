"""The shape of the core's configuration (MASTER_PLAN §5.4, DECISIONS D80, D140).

Every constant of the variants Session 4c registered is here, as the pre-registration fixed it
(``research/preregistrations/core-allocation.md``; a test holds the two equal): the roles'
environments, the declared long-run risk the static weights are computed from, the outer bands,
the trend tilt, the tax-aware benefit and each variant's budgets or weights. ``variant`` names the
one that holds the account.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, PositiveInt, model_validator

from ..domain.base import NonEmptyText
from .base import FrozenModel, Identifier
from .schema_vehicles import CoreRoleConfig

type Quoted = Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False)]
type Volatility = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
type Correlation = Annotated[Decimal, Field(ge=-1, le=1, allow_inf_nan=False)]
type Weight = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]


class CoreRisk(FrozenModel):
    """The long-run risk the static weights come from, in the base currency, declared before
    the data: each role's yearly volatility and every pair's correlation. No covariance is
    estimated from data (DECISIONS D80)."""

    volatilities: dict[Identifier, Volatility] = Field(min_length=1)
    correlations: list[tuple[Identifier, Identifier, Correlation]]

    @model_validator(mode="after")
    def _every_pair_once(self) -> Self:
        roles = list(self.volatilities)
        if any(a == b for a, b, _ in self.correlations):
            raise ValueError("a correlation pairs two different roles")
        pairs = [frozenset((a, b)) for a, b, _ in self.correlations]
        expected = {frozenset((a, b)) for i, a in enumerate(roles) for b in roles[i + 1 :]}
        if len(pairs) != len(set(pairs)) or set(pairs) != expected:
            raise ValueError("every pair of roles with a volatility needs exactly one correlation")
        return self


class CoreBands(FrozenModel):
    """A role's outer band: the smaller of ``absolute`` weight and ``relative`` of its target."""

    absolute: Quoted
    relative: Quoted


class CoreTilt(FrozenModel):
    """The trend tilt (``quant.trend``): momentum over cash, a moving average, a hysteresis buffer
    and the scale floor above zero."""

    momentum_months: PositiveInt
    average_months: Annotated[int, Field(ge=2)]
    hysteresis: Annotated[Decimal, Field(ge=0, lt=1, allow_inf_nan=False)]
    floor: Quoted


class CoreTaxAware(FrozenModel):
    """A trend reduction is made only while its tax is at most ``benefit_fraction`` of the value
    sold: the benefit it is assumed to buy."""

    benefit_fraction: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class BuyAndHoldVariant(FrozenModel):
    name: NonEmptyText
    kind: Literal["buy_and_hold"]
    weights: dict[Identifier, Weight] = Field(min_length=1)


class BandedWeightsVariant(FrozenModel):
    name: NonEmptyText
    kind: Literal["banded_weights"]
    weights: dict[Identifier, Weight] = Field(min_length=1)


class RiskBalanceVariant(FrozenModel):
    name: NonEmptyText
    kind: Literal["risk_balance"]
    budgets: dict[Identifier, Quoted] = Field(min_length=1)
    tilt: bool


class EquityOverlayVariant(FrozenModel):
    name: NonEmptyText
    kind: Literal["equity_overlay"]
    equity: Identifier
    fraction: Quoted
    tilt: bool


type CoreVariant = Annotated[
    BuyAndHoldVariant | BandedWeightsVariant | RiskBalanceVariant | EquityOverlayVariant,
    Field(discriminator="kind"),
]


class CoreConfig(FrozenModel):
    #: The variant that holds the account: the owner's point on Session 4c's frontier
    #: (DECISIONS D91).
    variant: NonEmptyText
    #: The core's roles (MASTER_PLAN §5.4), by id: what ``exposures.core_roles`` names.
    roles: dict[Identifier, CoreRoleConfig] = Field(min_length=1)
    cash_role: Identifier
    #: The growth and inflation environments (Bridgewater 2012) and the roles in each; cash
    #: belongs to none.
    environments: dict[Identifier, list[Identifier]] = Field(min_length=1)
    risk: CoreRisk
    bands: CoreBands
    tilt: CoreTilt
    tax_aware: CoreTaxAware
    variants: list[CoreVariant] = Field(min_length=1)

    @model_validator(mode="after")
    def _a_core(self) -> Self:
        problems = self._role_problems() + self._variant_problems()
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _role_problems(self) -> list[str]:
        problems: list[str] = []
        members = {role for basket in self.environments.values() for role in basket}
        if self.cash_role not in self.roles:
            problems.append(f"core.cash_role {self.cash_role} is not one of core.roles")
        if self.cash_role in members:
            problems.append("the cash role belongs to no environment")
        unknown = sorted(members - set(self.roles))
        if unknown:
            problems.append(f"core.environments name roles core.roles does not declare: {unknown}")
        if any(not basket for basket in self.environments.values()):
            problems.append("every environment needs at least one role")
        if any(len(set(basket)) != len(basket) for basket in self.environments.values()):
            problems.append("an environment names a role twice")
        if set(self.risk.volatilities) != members:
            problems.append("core.risk must cover exactly the roles of the environments")
        return problems

    def _variant_problems(self) -> list[str]:
        problems: list[str] = []
        names = [variant.name for variant in self.variants]
        if len(set(names)) != len(names):
            problems.append("each core variant is registered once")
        if self.variant not in names:
            problems.append(f"core.variant {self.variant} is not a registered variant: {names}")
        for variant in self.variants:
            problems += self._one(variant)
        return problems

    def _one(self, variant: CoreVariant) -> list[str]:
        where = f"core.variants.{variant.name}"
        if isinstance(variant, BuyAndHoldVariant | BandedWeightsVariant):
            problems = []
            unknown = sorted(set(variant.weights) - set(self.roles))
            if unknown:
                problems.append(f"{where} weights roles core.roles does not declare: {unknown}")
            total = sum(variant.weights.values(), Decimal(0))
            if total != 1:
                problems.append(f"{where}'s weights must add up to 1, not {total}")
            return problems
        if isinstance(variant, RiskBalanceVariant):
            problems = []
            if set(variant.budgets) != set(self.environments):
                problems.append(f"{where} budgets every environment and only those")
            if sum(variant.budgets.values(), Decimal(0)) != 1:
                problems.append(f"{where}'s risk budgets must add up to 1")
            return problems
        if variant.equity not in self.roles or variant.equity == self.cash_role:
            return [f"{where} holds an equity role that is not a risky role of core.roles"]
        return []
