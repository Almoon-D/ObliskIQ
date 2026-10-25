"""The core's target portfolio: what the account should hold and whether to trade to get there
(MASTER_PLAN §5.4, DECISIONS D140).

The core engine produces it from the variant the owner chose and the account's book; the decision
layer turns it into sized, gated orders. A vehicle's key is its contract id as a string, the name
the tax lots give the security.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import model_validator

from .base import DomainModel, NonEmptyText


class TargetStatus(StrEnum):
    """Whether the engine could form a target."""

    #: Targets are formed; ``rebalance`` says whether to trade every vehicle to them.
    READY = "ready"
    #: Something the variant reads is missing; nothing is bought or sold for the core, and
    #: ``reasons`` says what.
    NOT_EVALUABLE = "not_evaluable"


class RoleStatus(StrEnum):
    FILLED = "filled"
    #: No vehicle is proposable for the role, so its weight is held in the cash role.
    UNFILLED = "unfilled"


class TrendReading(DomainModel):
    """A tilted role's two signals at the evaluation month end."""

    momentum_positive: bool
    average_positive: bool
    momentum: float
    average_gap: float


class RoleTarget(DomainModel):
    """One role's target: the variant's weight, the trend's scale, the weight it comes to."""

    role: NonEmptyText
    status: RoleStatus
    static_weight: Decimal
    scale: Decimal
    weight: Decimal
    vehicle: NonEmptyText | None
    partner: NonEmptyText | None
    trend: TrendReading | None
    #: A trend reduction kept because its tax exceeds the benefit it is assumed to buy.
    reduction_held: bool
    reason: NonEmptyText


class VehicleTarget(DomainModel):
    """One vehicle's share of the core's investable value."""

    key: NonEmptyText
    role: NonEmptyText
    weight: Decimal


class TargetPortfolio(DomainModel):
    """The core's target on ``as_of``: the vehicle weights of its investable value, the role
    targets behind them, and whether a role left its outer band (``rebalance``)."""

    variant: NonEmptyText
    as_of: date
    #: The month end the tilt was read at (``None``: the variant does not tilt).
    trend_as_of: date | None
    status: TargetStatus
    rebalance: bool
    roles: tuple[RoleTarget, ...]
    vehicles: tuple[VehicleTarget, ...]
    reasons: tuple[NonEmptyText, ...]

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        total = sum((v.weight for v in self.vehicles), Decimal(0))
        if total > 1 or any(v.weight < 0 for v in self.vehicles):
            raise ValueError("vehicle weights are at least 0 and add up to 1 at most: no leverage")
        if self.status is TargetStatus.NOT_EVALUABLE and (self.vehicles or self.rebalance):
            raise ValueError("a target that is not evaluable holds no vehicle and trades nothing")
        if self.status is TargetStatus.NOT_EVALUABLE and not self.reasons:
            raise ValueError("a target that is not evaluable says why")
        return self

    def weight_of(self, key: str) -> Decimal:
        """``key``'s target weight (zero for a vehicle the target does not name)."""
        return sum((v.weight for v in self.vehicles if v.key == key), Decimal(0))
