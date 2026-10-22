"""The shape of the core's roles, the vehicle selector and the broker's capabilities.

* ``core.roles``: the core's roles (MASTER_PLAN §5.4), each with the research series it was priced
  on in Session 4c's pre-registration, the exposure a vehicle's tracking is measured against.
* ``vehicles``: the vehicle selector (MASTER_PLAN §5.8): the owner's ruling on currency-hedged share
  classes, the ranking's weights and scales, how a correlation is measured on research history and
  the swap-partner rule.
* ``broker_capabilities``: what the broker can do through the Web API per asset class and venue
  (search, quote, an order accepted by whatif; the order types and algos its contract rules list),
  each reading with its route, date and evidence. A check is verified only on the Client Portal
  route: a reading through IBKR's connector is evidence about IBKR, not about the API this package
  speaks (docs/knowledge/ibkr.md, "Routes and tags").

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from datetime import date
from decimal import Decimal
from typing import Annotated, Final, Literal, Self

from pydantic import Field, PositiveInt, model_validator

from ..domain.base import NonEmptyText
from ..domain.calendar import Mic
from ..domain.enums import AssetClass
from .base import FrozenModel
from .schema_universe import Symbol

type QuotedFraction = Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False)]
type QuotedWeight = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
type QuotedPositive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]

#: The route a capability is verified on: the Client Portal Web API.
VERIFYING_ROUTE: Final = "CP"


# ── the core's roles ──


class CoreRoleConfig(FrozenModel):
    """One role of the core: its name and the research series (``<dataset>.<field>``) it was priced
    on, which a vehicle's tracking is measured against."""

    name: NonEmptyText
    exposure_series: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")]

    @property
    def exposure_dataset(self) -> str:
        return self.exposure_series.partition(".")[0]


# ── the vehicle selector ──


class RankingWeights(FrozenModel):
    """How much each criterion counts in a vehicle's score; they add up to one."""

    cost: QuotedWeight
    tracking: QuotedWeight
    liquidity: QuotedWeight
    tax: QuotedWeight
    currency: QuotedWeight
    trading_window: QuotedWeight
    market_data: QuotedWeight

    @model_validator(mode="after")
    def _a_whole(self) -> Self:
        if sum(self.model_dump().values(), Decimal(0)) != 1:
            raise ValueError("vehicles.ranking.weights add up to 1")
        return self


class RankingConfig(FrozenModel):
    weights: RankingWeights
    #: One order's size and the position it builds, in the base currency, at which a vehicle is
    #: costed.
    reference_amount_base: QuotedPositive
    #: Years a round trip and its conversions are spread over beside the fund's yearly fee.
    holding_years: QuotedPositive
    #: An all-in cost a year at or above this scores 0.
    cost_ceiling: QuotedFraction
    #: Liquidity scores log10(average daily traded value / reference amount) over this many
    #: decades, capped at 1.
    liquidity_decades: QuotedPositive
    #: Stored daily sessions the average daily traded value is read over.
    liquidity_sessions: PositiveInt
    #: A tracking correlation at or below this scores 0; a perfect one scores 1.
    tracking_floor: Annotated[Decimal, Field(ge=0, lt=1, allow_inf_nan=False)]


class CorrelationConfig(FrozenModel):
    """Log returns of ``period`` in the base currency over ``lookback_days`` before the reading's
    date; fewer than ``min_returns`` common returns is no measurement."""

    period: Literal["month", "week"]
    lookback_days: PositiveInt
    min_returns: Annotated[int, Field(ge=2)]
    #: The research dataset of ECB reference rates a series is converted to the base currency with.
    fx_dataset: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]


class SwapPartnerConfig(FrozenModel):
    #: The owner's switch: a partner on the same tracked index from another issuer.
    same_index: bool
    #: A partner's returns correlate with the vehicle it replaces at least this much.
    min_correlation: Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False)]


class VehiclesConfig(FrozenModel):
    #: Whether a currency-hedged share class may fill an exposure: the owner's ruling (MASTER_PLAN
    #: §11). Until ``allowed``, a hedged class is ranked as its own vehicle and refused.
    hedged_share_classes: Literal["undecided", "allowed", "refused"]
    ranking: RankingConfig
    correlation: CorrelationConfig
    swap_partners: SwapPartnerConfig


# ── broker capabilities ──


class CapabilityReading(FrozenModel):
    """One check of a capability: ``verified`` or ``refused`` as read on the Client Portal route
    on ``observed_on``; ``unverified`` otherwise, with whatever evidence there is."""

    status: Literal["verified", "refused", "unverified"]
    route: Literal["CP", "MCP"] | None
    observed_on: date | None
    evidence: NonEmptyText

    @property
    def verified(self) -> bool:
        return self.status == "verified"

    @model_validator(mode="after")
    def _read_on_the_api(self) -> Self:
        if self.status != "unverified" and (
            self.route != VERIFYING_ROUTE or self.observed_on is None
        ):
            raise ValueError(
                f"a check is {self.status} only as read on the Client Portal route on a date "
                "(route: CP, observed_on); any other reading leaves it unverified"
            )
        if (self.route is None) != (self.observed_on is None):
            raise ValueError("a reading names its route and its date together")
        return self


class ListReading(CapabilityReading):
    """What the contract rules list (order types, algos): the values as read."""

    values: list[NonEmptyText]

    @model_validator(mode="after")
    def _values_when_read(self) -> Self:
        if self.verified and not self.values:
            raise ValueError("a verified list names what it listed")
        if len(set(self.values)) != len(self.values):
            raise ValueError("a value is listed twice")
        return self


class CapabilityCheck(FrozenModel):
    """What the Web API does for ``asset_class`` on ``venue``. ``probe`` names the universe
    instrument the probe reads (an option underlying for options); ``null`` when none lists
    there."""

    asset_class: AssetClass
    venue: Mic
    probe: Symbol | None
    search: CapabilityReading
    quote: CapabilityReading
    order: CapabilityReading
    order_types: ListReading
    algos: ListReading


class CapabilityProbeConfig(FrozenModel):
    """``obliskiq universe capabilities --probe``: whatif only, nothing placed."""

    #: The previewed buy's limit sits this fraction below the bid, rounded down to a tick.
    limit_offset_fraction: QuotedFraction
    #: IBKR's snapshot field for the account's trading permission on the contract.
    permission_field_id: Annotated[str, Field(pattern=r"^[0-9]+$")]


class BrokerCapabilitiesConfig(FrozenModel):
    checks: list[CapabilityCheck]
    probe: CapabilityProbeConfig

    @model_validator(mode="after")
    def _one_check_each(self) -> Self:
        keys = [(check.asset_class, check.venue) for check in self.checks]
        twice = sorted({f"{c.value}@{v}" for c, v in keys if keys.count((c, v)) > 1})
        if twice:
            raise ValueError(f"broker_capabilities.checks lists twice: {', '.join(twice)}")
        return self

    def check(self, asset_class: AssetClass, venue: str) -> CapabilityCheck | None:
        return next(
            (c for c in self.checks if c.asset_class is asset_class and c.venue == venue), None
        )


__all__ = [
    "BrokerCapabilitiesConfig",
    "CapabilityCheck",
    "CapabilityProbeConfig",
    "CapabilityReading",
    "CoreRoleConfig",
    "CorrelationConfig",
    "ListReading",
    "RankingConfig",
    "RankingWeights",
    "SwapPartnerConfig",
    "VehiclesConfig",
]
