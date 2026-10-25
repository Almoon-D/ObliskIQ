"""The core's target portfolio (MASTER_PLAN §5.4): what the core engine decided on a date, role by
role and vehicle by vehicle, and the trades that move the book there.

Weights are shares of the core's budget (its share of investable capital, ``capital.budgets``);
values and prices are in the base currency. A trade is whole units of one vehicle.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum

from .base import Conid, DomainModel, NonEmptyText
from .orders import OrderSide

ZERO = Decimal(0)


class Trigger(StrEnum):
    """Why the book trades now, or does not."""

    #: The book held nothing: every role is bought to its target.
    START = "start"
    #: A role left its outer band: every role goes back to its target.
    BAND = "band"
    #: Inside the bands, the book holds cash beyond its targets: it buys the roles below their
    #: targets with it and sells nothing.
    CASH = "cash"
    #: Inside the bands and no cash to deploy: nothing trades.
    HOLD = "hold"


class RoleStatus(StrEnum):
    #: A vehicle of the role may be bought.
    FILLED = "filled"
    #: The book holds the role, but none of its vehicles may be bought now: it keeps at most what
    #: it holds.
    HELD_ONLY = "held_only"
    #: Nothing held and nothing may be bought: its weight stays in the book's cash.
    UNFILLED = "unfilled"


class RoleTarget(DomainModel):
    """One role: its variant weight, its trend scale, its target and what is held, and why."""

    role: NonEmptyText
    status: RoleStatus
    static_weight: Decimal
    scale: Decimal
    target_weight: Decimal
    held_weight: Decimal
    reduction_held: bool
    vehicles: tuple[Conid, ...]
    reason: NonEmptyText


class VehicleTarget(DomainModel):
    """One vehicle: its role, its target weight of the budget, the units held and one unit's
    price."""

    conid: Conid
    role: NonEmptyText
    weight: Decimal
    held_units: Decimal
    price: Decimal


class PlannedTrade(DomainModel):
    """Whole units of one vehicle to buy or sell, and their value."""

    conid: Conid
    role: NonEmptyText
    side: OrderSide
    quantity: Decimal
    notional: Decimal


class WithheldTrade(DomainModel):
    """A trade the targets call for that is not made, and why (the cost model cannot price it,
    or a sale would cost more than it raises)."""

    conid: Conid
    side: OrderSide
    quantity: Decimal
    reason: NonEmptyText


class TargetPortfolio(DomainModel):
    """The core's decision on ``as_of``: the variant, the trend's month end, the budget, why it
    trades, every role and vehicle, the trades, those withheld, and what could not be decided."""

    variant: NonEmptyText
    as_of: date
    trend_as_of: date | None
    budget: Decimal
    trigger: Trigger
    roles: tuple[RoleTarget, ...]
    vehicles: tuple[VehicleTarget, ...]
    trades: tuple[PlannedTrade, ...]
    withheld: tuple[WithheldTrade, ...]
    notes: tuple[NonEmptyText, ...]

    def weight(self, conid: int) -> Decimal:
        """``conid``'s target weight of the budget (zero when the portfolio does not name it)."""
        return next((v.weight for v in self.vehicles if v.conid == conid), ZERO)

    def role(self, name: str) -> RoleTarget | None:
        return next((r for r in self.roles if r.role == name), None)

    def withheld_for(self, conid: int) -> WithheldTrade | None:
        return next((w for w in self.withheld if w.conid == conid), None)
