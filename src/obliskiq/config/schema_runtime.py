"""The shape of the service, capital, the skeleton core, trading and reporting configuration.

``runtime`` schedules the service (MASTER_PLAN §5.14): a heartbeat, four decision cycles a trading
day anchored to venue sessions, a daily digest. ``capital`` is Session 6's minimal managed capital
(MASTER_PLAN §5.15). ``core.skeleton_targets`` is the walking skeleton's static book, removed when
Session 13 builds the core engine. ``trading`` is how the basic rebalancer trades toward it, and
``reporting`` how the owner hears of it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, PositiveFloat, PositiveInt, model_validator

from ..domain.base import Conid, NonEmptyText
from ..domain.calendar import Mic
from .base import FrozenModel

type Anchor = Literal["open", "close"]
type QuotedFraction = Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False)]
type QuotedMoney = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]

#: A day's minutes: an offset beyond it would anchor a job to another day's session.
MINUTES_PER_DAY = 1440


class SessionAnchor(FrozenModel):
    """A moment of a venue's regular session: its open or close, moved by ``offset_minutes``."""

    venue: Mic
    anchor: Anchor
    offset_minutes: Annotated[int, Field(gt=-MINUTES_PER_DAY, lt=MINUTES_PER_DAY)]


class CycleConfig(SessionAnchor):
    """One decision cycle of a trading day."""

    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]


class HealthConfig(FrozenModel):
    """The heartbeat file and the optional ping (its URL is ``OBLISK_HEALTH_PING_URL``)."""

    ping_timeout_seconds: PositiveFloat


class RuntimeConfig(FrozenModel):
    heartbeat_seconds: PositiveInt
    cycles: list[CycleConfig] = Field(min_length=1)
    digest: SessionAnchor
    catch_up_minutes: PositiveInt
    downgrade_after_failed_cycles: PositiveInt
    stuck_after_minutes: PositiveInt
    status_report_days: PositiveInt
    health: HealthConfig

    @model_validator(mode="after")
    def _distinct_names(self) -> Self:
        names = [cycle.name for cycle in self.cycles]
        if len(names) != len(set(names)):
            raise ValueError("each cycle has its own name")
        return self


class CapitalConfig(FrozenModel):
    """``whole_account``: the account's net liquidation value is managed; ``cap``: at most
    ``cap_base`` of it, in the base currency."""

    mode: Literal["whole_account", "cap"]
    cap_base: QuotedMoney | None

    @model_validator(mode="after")
    def _cap_named(self) -> Self:
        if self.mode == "cap" and self.cap_base is None:
            raise ValueError("capital.mode cap needs capital.cap_base")
        return self


class SkeletonTarget(FrozenModel):
    """One role of the skeleton book: its weight of managed capital and the conid filling it."""

    role: NonEmptyText
    weight: QuotedFraction
    conid: Conid


class CoreConfig(FrozenModel):
    skeleton_targets: list[SkeletonTarget] = Field(min_length=1)

    @model_validator(mode="after")
    def _a_book(self) -> Self:
        roles = [target.role for target in self.skeleton_targets]
        conids = [target.conid for target in self.skeleton_targets]
        if len(set(roles)) != len(roles) or len(set(conids)) != len(conids):
            raise ValueError("each role and each conid is listed once")
        if sum((target.weight for target in self.skeleton_targets), Decimal(0)) > 1:
            raise ValueError("the skeleton's weights add up to more than the whole book")
        return self


class MinTradeConfig(FrozenModel):
    """The smallest trade, in the base currency: an absolute amount, a fraction of managed
    capital, or both combined by ``combine``."""

    absolute_base: QuotedMoney | None
    fraction_of_managed: QuotedFraction | None
    combine: Literal["max", "min"]

    @model_validator(mode="after")
    def _something(self) -> Self:
        if self.absolute_base is None and self.fraction_of_managed is None:
            raise ValueError("min_trade needs absolute_base, fraction_of_managed or both")
        return self


class InnerBandConfig(FrozenModel):
    """A role trades back to its target once its weight is further from it than the smaller of
    ``absolute`` (weight points) and ``relative`` (a share of its target)."""

    absolute: QuotedFraction
    relative: QuotedFraction


class TradingConfig(FrozenModel):
    min_trade: MinTradeConfig
    inner_band: InnerBandConfig
    intent_ttl_minutes: PositiveInt


class TelegramConfig(FrozenModel):
    chunk_characters: PositiveInt
    timeout_seconds: PositiveFloat


class ReportingConfig(FrozenModel):
    telegram: TelegramConfig
    digest_rows: PositiveInt
