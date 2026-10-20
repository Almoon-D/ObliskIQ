"""The shape of the order path's configuration: lite's ``discipline`` keys and ``execution``.

The quote policy and the reply whitelist are lists of records: an owner override replaces the
whole list, and the unread-key scan reads each list as one key.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, NonNegativeInt, PositiveFloat, PositiveInt, model_validator

from ..domain.base import Conid, CurrencyCode, NonEmptyText
from ..domain.enums import AssetClass
from ..domain.orders import ExecutionMode, Sleeve, TimeInForce
from .base import FrozenModel

#: IBKR serves trades for the current day and six before it.
IBKR_MAX_TRADE_DAYS = 7
#: The 36 characters of a uuid; a shorter cOID is a short form of it.
UUID_TEXT_LENGTH = 36
#: The shortest cOID short form allowed: 16 base-32 characters keep 80 of the uuid's bits.
MIN_COID_LENGTH = 16
HOURS_PER_DAY = 24

type MessageId = Annotated[str, Field(pattern=r"^[op][0-9]+$")]


class DisciplineConfig(FrozenModel):
    """lite's ``discipline`` keys the order path reads."""

    entry_order_type: Literal["limit_only"]
    exit_reasons_allowed: list[NonEmptyText] = Field(min_length=1)


class ModesConfig(FrozenModel):
    initial: ExecutionMode

    @model_validator(mode="after")
    def _not_live(self) -> Self:
        if self.initial not in (ExecutionMode.SHADOW, ExecutionMode.PAPER):
            raise ValueError(
                "a deployment starts in shadow or paper; the live levels are reached by the "
                "promotion gate and an owner command"
            )
        return self


class OrdersConfig(FrozenModel):
    coid_max_length: Annotated[int, Field(ge=MIN_COID_LENGTH, le=UUID_TEXT_LENGTH)]
    max_reply_rounds: PositiveInt
    trades_lookup_days: Annotated[int, Field(ge=1, le=IBKR_MAX_TRADE_DAYS)]


class ConfirmedReply(FrozenModel):
    message_id: MessageId
    when: Literal["any", "stop_limit", "cash_quantity", "conversion"]


class ReplyPolicyConfig(FrozenModel):
    confirm: list[ConfirmedReply]
    no_market_data_message_id: MessageId

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        ids = [entry.message_id for entry in self.confirm]
        if len(ids) != len(set(ids)):
            raise ValueError("each message id is listed once")
        if self.no_market_data_message_id in ids:
            raise ValueError(
                "the no-market-data message is confirmed only under the quote policy, never "
                "unconditionally"
            )
        return self


class RealtimeRequired(FrozenModel):
    policy: Literal["realtime_required"]
    asset_classes: list[AssetClass] = Field(min_length=1)
    max_quote_age_seconds: PositiveInt


class RealtimePreferred(FrozenModel):
    policy: Literal["realtime_preferred"]
    asset_classes: list[AssetClass] = Field(min_length=1)
    max_quote_age_seconds: PositiveInt
    max_delay_seconds: NonNegativeInt


class NavPriced(FrozenModel):
    policy: Literal["nav_priced"]
    asset_classes: list[AssetClass] = Field(min_length=1)


type QuotePolicyRule = Annotated[
    RealtimeRequired | RealtimePreferred | NavPriced, Field(discriminator="policy")
]


class CollarConfig(FrozenModel):
    base_fraction: Annotated[float, Field(ge=0, lt=1)]
    max_fraction: Annotated[float, Field(gt=0, lt=1)]
    sigma_multiplier: PositiveFloat
    sessions_per_year: PositiveInt
    session_hours: Annotated[float, Field(gt=0, le=HOURS_PER_DAY)]

    @model_validator(mode="after")
    def _ceiling_above_base(self) -> Self:
        if self.max_fraction < self.base_fraction:
            raise ValueError("collar.max_fraction must not be below base_fraction")
        return self


class KillSwitchConfig(FrozenModel):
    sweep_interval_seconds: PositiveFloat
    store_timeout_seconds: PositiveFloat


type Fraction = Annotated[Decimal, Field(gt=0, le=1)]


class CapsConfig(FrozenModel):
    """What orders that add risk may commit, as fractions of NAV and a count, per 24 hours."""

    max_order_fraction_of_nav: Fraction
    max_daily_fraction_of_nav: Fraction
    max_orders_per_day: PositiveInt

    @model_validator(mode="after")
    def _order_within_day(self) -> Self:
        if self.max_order_fraction_of_nav > self.max_daily_fraction_of_nav:
            raise ValueError("one order's cap cannot exceed the daily cap")
        return self


class PretradeConfig(FrozenModel):
    caps: CapsConfig
    cash_floor: Annotated[Decimal, Field(ge=0)]


class ApprovalConfig(FrozenModel):
    """The sleeves whose intents wait for the owner's approval (none: the queue is off)."""

    sleeves: list[Sleeve]

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        if len(set(self.sleeves)) != len(self.sleeves):
            raise ValueError("each sleeve is listed once")
        return self


class AlgoParameterConfig(FrozenModel):
    name: NonEmptyText
    value: NonEmptyText


class FlattenConfig(FrozenModel):
    intent_ttl_minutes: PositiveInt
    tif: TimeInForce


type Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
type Share = Annotated[Decimal, Field(gt=0, lt=1, allow_inf_nan=False)]


class CurrencyPair(FrozenModel):
    """One of IBKR's spot pairs (secType ``CASH``): buying it sells ``quote`` for ``base``."""

    symbol: NonEmptyText
    conid: Conid
    base: CurrencyCode
    quote: CurrencyCode

    @model_validator(mode="after")
    def _two_currencies(self) -> Self:
        if self.base == self.quote:
            raise ValueError("a currency pair names two currencies")
        return self


class CurrencyConfig(FrozenModel):
    """The currency manager: the pairs it trades, the buffer, batching and repatriation."""

    pairs: list[CurrencyPair]
    buffer_base: Money
    min_conversion_base: Money
    repatriate_above_base: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    limit_fraction: Share
    intent_ttl_minutes: PositiveInt
    fill_timeout_seconds: PositiveFloat
    fill_poll_seconds: PositiveFloat

    @model_validator(mode="after")
    def _one_pair_each(self) -> Self:
        conids = [pair.conid for pair in self.pairs]
        couples = [frozenset((pair.base, pair.quote)) for pair in self.pairs]
        if len(set(conids)) != len(conids) or len(set(couples)) != len(couples):
            raise ValueError("each pair, and each pair of currencies, is listed once")
        return self


class AlgoRule(FrozenModel):
    """An IBKR algo for orders on these listing venues and asset classes, when enabled."""

    venues: list[NonEmptyText] = Field(min_length=1)
    asset_classes: list[AssetClass] = Field(min_length=1)
    strategy: NonEmptyText
    parameters: list[AlgoParameterConfig]
    enabled: bool


class PricingConfig(FrozenModel):
    algos: list[AlgoRule]
    start_ticks_inside_mid: NonNegativeInt
    max_reprices: NonNegativeInt
    reprice_interval_minutes: PositiveInt


class LimitOffsetConfig(FrozenModel):
    """How far below its stop a protective limit sits (DECISIONS D67)."""

    floor_fraction: Share
    sigma_multiple: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    max_fraction: Share

    @model_validator(mode="after")
    def _floor_within_max(self) -> Self:
        if self.floor_fraction > self.max_fraction:
            raise ValueError("limit_offset.floor_fraction must not exceed max_fraction")
        return self


class EmergencyLimitConfig(FrozenModel):
    enabled: bool
    fraction_below_bid: Share


class GapConfig(FrozenModel):
    """Repricing a triggered stop that did not fill."""

    step_fraction: Share
    max_steps: PositiveInt
    step_interval_minutes: PositiveInt
    emergency: EmergencyLimitConfig


class ProtectiveConfig(FrozenModel):
    tif: TimeInForce
    intent_ttl_days: PositiveInt
    limit_offset: LimitOffsetConfig
    gap: GapConfig

    @model_validator(mode="after")
    def _rests(self) -> Self:
        if self.tif is TimeInForce.IOC:
            raise ValueError("a protective stop rests at the broker: IOC never rests")
        return self


class ReconcileConfig(FrozenModel):
    grace_seconds: PositiveInt
    cash_tolerance_base: Money


class ExecutionConfig(FrozenModel):
    modes: ModesConfig
    orders: OrdersConfig
    reply_policy: ReplyPolicyConfig
    quote_policy: list[QuotePolicyRule]
    collar: CollarConfig
    kill_switch: KillSwitchConfig
    pretrade: PretradeConfig
    approval: ApprovalConfig
    flatten: FlattenConfig
    currency: CurrencyConfig
    pricing: PricingConfig
    protective: ProtectiveConfig
    reconcile: ReconcileConfig

    @model_validator(mode="after")
    def _one_rule_per_class(self) -> Self:
        classes = [cls for rule in self.quote_policy for cls in rule.asset_classes]
        repeated = sorted({cls.value for cls in classes if classes.count(cls) > 1})
        if repeated:
            raise ValueError(f"quote_policy names {', '.join(repeated)} in more than one rule")
        return self
