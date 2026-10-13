"""The shape of the order path's configuration: lite's ``discipline`` keys and ``execution``.

The quote policy and the reply whitelist are lists of records: an owner override replaces the
whole list, and the unread-key scan reads each list as one key.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from typing import Annotated, Literal, Self

from pydantic import Field, NonNegativeInt, PositiveFloat, PositiveInt, model_validator

from ..domain.base import NonEmptyText
from ..domain.enums import AssetClass
from ..domain.orders import ExecutionMode
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
    sigma_multiplier: PositiveFloat
    sessions_per_year: PositiveInt
    session_hours: Annotated[float, Field(gt=0, le=HOURS_PER_DAY)]


class KillSwitchConfig(FrozenModel):
    sweep_interval_seconds: PositiveFloat
    store_timeout_seconds: PositiveFloat


class ExecutionConfig(FrozenModel):
    modes: ModesConfig
    orders: OrdersConfig
    reply_policy: ReplyPolicyConfig
    quote_policy: list[QuotePolicyRule]
    collar: CollarConfig
    kill_switch: KillSwitchConfig

    @model_validator(mode="after")
    def _one_rule_per_class(self) -> Self:
        classes = [cls for rule in self.quote_policy for cls in rule.asset_classes]
        repeated = sorted({cls.value for cls in classes if classes.count(cls) > 1})
        if repeated:
            raise ValueError(f"quote_policy names {', '.join(repeated)} in more than one rule")
        return self
