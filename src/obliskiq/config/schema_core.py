"""The shape of the core's configuration (MASTER_PLAN §5.4).

``core.roles`` declares the roles a universe instrument may fill. ``core.variants`` holds every
variant Session 4c registered, with the constants its pre-registration fixed
(``research/preregistrations/core-allocation.md``): the environments and the declared long-run risk
a risk balance is solved on, the bands, the trend tilt and the benefit a tax-aware reduction must
buy. ``core.variant`` names the one the core runs: the variant the owner's point on Session 4c's
frontier selects (DECISIONS D60, D91).

Each variant states its strategy:

* ``buy_and_hold`` (the equity reference): its weights, bought once and held;
* ``banded_weights`` (the 60/40): its weights, traded back to them when a role leaves its band;
* ``risk_balance`` (A, B, C): risk budgets by environment, solved once on the declared risk;
* ``equity_overlay`` (D10 to D50): one equity role at a fraction, the rest in the cash role.

The last two say whether the trend tilt applies; a variant that tilts trades tax-aware (Session
4c's rule, ``research/strategies.py``).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, PositiveInt, model_validator

from ..domain.base import NonEmptyText
from .base import FrozenModel, Identifier
from .schema_vehicles import CoreRoleConfig

type Share = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
type Volatility = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
type Correlation = Annotated[Decimal, Field(ge=-1, le=1, allow_inf_nan=False)]


class BuyAndHoldVariant(FrozenModel):
    """Its weights bought once, then held (the equity reference)."""

    strategy: Literal["buy_and_hold"]
    weights: dict[Identifier, Share] = Field(min_length=1)

    def roles(self) -> set[str]:
        return set(self.weights)


class BandedWeightsVariant(FrozenModel):
    """Its weights, kept within the bands (the 60/40)."""

    strategy: Literal["banded_weights"]
    weights: dict[Identifier, Share] = Field(min_length=1)

    def roles(self) -> set[str]:
        return set(self.weights)


class RiskBalanceVariant(FrozenModel):
    """Static weights from risk budgets by environment (A, B, C), with or without the tilt."""

    strategy: Literal["risk_balance"]
    budgets: dict[Identifier, Share] = Field(min_length=1)
    tilt: bool

    def roles(self) -> set[str]:
        return set()


class EquityOverlayVariant(FrozenModel):
    """One equity role at ``fraction``, the rest in the cash role (D), with or without the
    tilt."""

    strategy: Literal["equity_overlay"]
    equity: Identifier
    fraction: Share
    tilt: bool

    def roles(self) -> set[str]:
        return {self.equity}


type VariantConfig = Annotated[
    BuyAndHoldVariant | BandedWeightsVariant | RiskBalanceVariant | EquityOverlayVariant,
    Field(discriminator="strategy"),
]


class CoreRiskConfig(FrozenModel):
    """The declared long-run risk in the base currency: each role's yearly volatility and every
    pair's correlation, as Session 4c declared them before its data."""

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

    def matrix(self, roles: list[str]) -> tuple[list[float], list[list[float]]]:
        """Volatilities and the correlation matrix in the order of ``roles``."""
        rho = {frozenset((a, b)): float(c) for a, b, c in self.correlations}
        vols = [float(self.volatilities[role]) for role in roles]
        corr = [[1.0 if a == b else rho[frozenset((a, b))] for b in roles] for a in roles]
        return vols, corr


class CoreBandsConfig(FrozenModel):
    """A role's outer band: the smaller of ``absolute`` weight and ``relative`` of its target."""

    absolute: Share
    relative: Share


class CoreTiltConfig(FrozenModel):
    """The trend tilt (``quant.trend``): momentum over cash, a moving average, the hysteresis
    buffer and the scale floor."""

    momentum_months: PositiveInt
    average_months: Annotated[int, Field(ge=2)]
    hysteresis: Annotated[Decimal, Field(ge=0, lt=1, allow_inf_nan=False)]
    floor: Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False)]


class CoreTaxAwareConfig(FrozenModel):
    """A trend reduction is made only while its tax is at most this share of the value sold."""

    benefit_fraction: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class CoreConfig(FrozenModel):
    #: The core's roles (MASTER_PLAN §5.4), by id: what ``exposures.core_roles`` and the variants
    #: name.
    roles: dict[Identifier, CoreRoleConfig] = Field(min_length=1)
    variant: NonEmptyText
    cash_role: Identifier
    environments: dict[Identifier, list[Identifier]] = Field(min_length=1)
    risk: CoreRiskConfig
    bands: CoreBandsConfig
    tilt: CoreTiltConfig
    tax_aware: CoreTaxAwareConfig
    variants: dict[NonEmptyText, VariantConfig] = Field(min_length=1)

    def risk_roles(self) -> list[str]:
        """The roles of the environments, in their first order of appearance."""
        return list(dict.fromkeys(r for basket in self.environments.values() for r in basket))

    def chosen(self) -> VariantConfig:
        """The variant the core runs."""
        return self.variants[self.variant]

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        problems: list[str] = []
        if self.variant not in self.variants:
            problems.append(f"core.variant: {self.variant!r} is not one of core.variants")
        named = {self.cash_role, *self.risk_roles()}
        named |= {role for variant in self.variants.values() for role in variant.roles()}
        problems += [
            f"core: {role} is not a role of core.roles" for role in sorted(named - set(self.roles))
        ]
        if any(not basket for basket in self.environments.values()):
            problems.append("core.environments: every environment needs at least one role")
        if self.cash_role in self.risk_roles():
            problems.append("core.cash_role: the cash role belongs to no environment")
        if set(self.risk.volatilities) != set(self.risk_roles()):
            problems.append("core.risk: the declared risk covers exactly the environments' roles")
        for name, variant in sorted(self.variants.items()):
            problems += self._variant_problems(name, variant)
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _variant_problems(self, name: str, variant: VariantConfig) -> list[str]:
        where = f"core.variants.{name}"
        if isinstance(variant, BuyAndHoldVariant | BandedWeightsVariant):
            if sum(variant.weights.values()) > 1:
                return [f"{where}.weights: they add up to more than the whole book"]
            return []
        if isinstance(variant, RiskBalanceVariant):
            problems: list[str] = []
            if set(variant.budgets) != set(self.environments):
                problems.append(f"{where}.budgets: one budget per environment of core.environments")
            if sum(variant.budgets.values()) != 1 or any(b <= 0 for b in variant.budgets.values()):
                problems.append(f"{where}.budgets: positive and summing to 1")
            return problems
        if variant.equity == self.cash_role:
            return [f"{where}.equity: the equity role is not the cash role"]
        return []
