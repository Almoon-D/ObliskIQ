"""The shape of the scoreboard's and the promotion ladder's configuration
(``config/defaults/scoreboard.yaml``; MASTER_PLAN §5.19).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from decimal import Decimal
from typing import Annotated, Self

from pydantic import Field, PositiveInt, model_validator

from ..domain.base import Conid, NonEmptyText
from .base import FrozenModel

#: The total weight of a reference's members, exactly.
WHOLE = Decimal(1)


class ReferenceMember(FrozenModel):
    conid: Conid
    weight: Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False)]


class ReferenceConfig(FrozenModel):
    """A bought-and-held mix of eligible vehicles, rebalanced to its weights each month end."""

    name: NonEmptyText
    members: list[ReferenceMember] = Field(min_length=1)

    @model_validator(mode="after")
    def _whole(self) -> Self:
        if sum((m.weight for m in self.members), Decimal(0)) != WHOLE:
            raise ValueError(f"the weights of {self.name} add up to 1")
        if len({m.conid for m in self.members}) != len(self.members):
            raise ValueError(f"{self.name} names a member twice")
        return self


class ScoreboardConfig(FrozenModel):
    references: list[ReferenceConfig] = Field(min_length=1)
    max_mark_age_days: PositiveInt


class PromotionConfig(FrozenModel):
    """The operational entry criteria of the promotion ladder (MASTER_PLAN §5.19)."""

    shadow_days: PositiveInt
    paper_weeks: PositiveInt
    live_small_months: PositiveInt
    order_success_min: Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False)]
    min_orders: PositiveInt
    slippage_excess_max: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    min_fills: PositiveInt
    stuck_after_hours: PositiveInt
