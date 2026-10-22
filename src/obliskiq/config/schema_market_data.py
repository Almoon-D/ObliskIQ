"""The shape of the market-data configuration: the IBKR subscriptions on offer and the owner's
plan (MASTER_PLAN §5.13).

``market_data.feeds`` lists the subscriptions with their non-professional price and the venues
and asset classes each streams in real time; ``market_data.plan`` names the ones the owner's main
username holds (shared with its paper account). A venue and asset class no plan feed covers is
read delayed, where the venue serves delayed data at all.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from decimal import Decimal
from typing import Annotated, Self

from pydantic import Field, model_validator

from ..domain.base import CurrencyCode, NonEmptyText
from ..domain.calendar import Mic
from ..domain.enums import AssetClass
from .base import FrozenModel, Identifier
from .schema_calendars import Provenance


class FeedCost(FrozenModel):
    """A monthly price, in the currency IBKR bills it in."""

    amount: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    currency: CurrencyCode


class MarketDataFeed(FrozenModel):
    """One IBKR market-data subscription."""

    id: Identifier
    name: NonEmptyText
    venues: list[Mic] = Field(min_length=1)
    asset_classes: list[AssetClass] = Field(min_length=1)
    monthly_cost: FeedCost
    #: When IBKR waives the fee (a commission threshold), in words; ``None`` when it does not.
    waiver: NonEmptyText | None
    source: Provenance


class MarketDataConfig(FrozenModel):
    feeds: list[MarketDataFeed] = Field(min_length=1)
    plan: list[Identifier]

    @model_validator(mode="after")
    def _plan_names_feeds(self) -> Self:
        ids = [feed.id for feed in self.feeds]
        if len(set(ids)) != len(ids):
            raise ValueError("market_data.feeds lists a feed id twice")
        if len(set(self.plan)) != len(self.plan):
            raise ValueError("market_data.plan lists a feed twice")
        unknown = sorted(set(self.plan) - set(ids))
        if unknown:
            raise ValueError(f"market_data.plan names feeds that are not offered: {unknown}")
        return self
