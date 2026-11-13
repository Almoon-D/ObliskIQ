"""The shape of the thesis book's configuration (``config/defaults/thesis.yaml``; MASTER_PLAN
§5.5, DECISIONS D156, D159).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from decimal import Decimal
from typing import Annotated, Self

from pydantic import Field, PositiveInt, model_validator

from .base import FrozenModel, Identifier

#: A share of the book's capital or of investable capital, quoted, inside [0, 1].
type QuotedShare = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
#: A share that must be something, inside (0, 1].
type QuotedFraction = Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False)]
#: A multiple, quoted, finite and positive.
type QuotedMultiple = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]


class ConvictionConfig(FrozenModel):
    """What each conviction level multiplies the base size by."""

    normal: QuotedMultiple
    high: QuotedMultiple
    max: QuotedMultiple

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not self.normal <= self.high <= self.max:
            raise ValueError("a stronger conviction never sizes below a weaker one")
        return self


class ThesisCapsConfig(FrozenModel):
    """The most of the book's capital one position, sector, country or foreign currency takes."""

    position: QuotedFraction
    sector: QuotedFraction
    country: QuotedFraction
    foreign_currency: QuotedFraction


class ThesisLiquidityConfig(FrozenModel):
    """The liquidity floor: a position against the instrument's average daily traded value."""

    sessions: PositiveInt
    max_share_of_daily_value: QuotedFraction


class ThesisBookConfig(FrozenModel):
    """The thesis book: its capital, its slots, its sizes and its gate's numbers."""

    capital_share: QuotedShare
    max_positions: PositiveInt
    conviction: ConvictionConfig
    caps: ThesisCapsConfig
    min_gain_to_loss: QuotedMultiple
    liquidity: ThesisLiquidityConfig
    entry_window_days: PositiveInt
    defensive_roles: list[Identifier] = Field(min_length=1)

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        if len(set(self.defensive_roles)) != len(self.defensive_roles):
            raise ValueError("defensive_roles lists a role twice")
        return self
