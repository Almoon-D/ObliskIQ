"""The shape of the service, capital, trading and reporting configuration.

``runtime`` schedules the service (MASTER_PLAN §5.14): a heartbeat, four decision cycles a trading
day anchored to venue sessions, a daily digest. ``capital`` is managed capital, the sleeve budgets,
deposits, withdrawal reservations, the planner and the time-weighted return (MASTER_PLAN §5.15).
``trading`` is the smallest trade the core makes and how long its intents live, and
``reporting`` how the owner hears of it.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, PositiveFloat, PositiveInt, model_validator

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


type QuotedShare = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
type QuotedRate = Annotated[Decimal, Field(allow_inf_nan=False)]
type QuotedWeight = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
type SleeveName = Literal["core", "satellite", "options"]


class BudgetsConfig(FrozenModel):
    """The sleeve budgets as shares of investable capital (MASTER_PLAN §11 row 2)."""

    core: QuotedShare
    equity_satellite: QuotedShare
    option_premium: QuotedShare
    cash_floor: QuotedShare

    @model_validator(mode="after")
    def _within_the_book(self) -> Self:
        total = self.core + self.equity_satellite + self.option_premium + self.cash_floor
        if total > 1:
            raise ValueError(f"the budgets add up to {total}, more than the whole book")
        return self


class DeploymentConfig(FrozenModel):
    tranche_days: PositiveInt


class ReservationsConfig(FrozenModel):
    buffer_days: Annotated[int, Field(ge=0)]


class PlannerWeightsConfig(FrozenModel):
    tax: QuotedWeight
    trading: QuotedWeight
    strategy: QuotedWeight


class StrategyCostConfig(FrozenModel):
    """Strategy cost as a share of the value sold, by what is known of the position."""

    overweight: QuotedShare
    ordinary: QuotedShare
    protected: QuotedShare

    @model_validator(mode="after")
    def _ranked(self) -> Self:
        if not self.overweight <= self.ordinary <= self.protected:
            raise ValueError("strategy costs rank overweight <= ordinary <= protected")
        return self


class PlannerConfig(FrozenModel):
    weights: PlannerWeightsConfig
    strategy_cost: StrategyCostConfig
    entry_window_days: Annotated[int, Field(ge=0)]
    protected_sleeves: list[SleeveName]


class PacingConfig(FrozenModel):
    """How a planned sale is paced by its expected annual return (sufficient evidence only)."""

    weak_below: QuotedRate
    strong_above: QuotedRate
    tranches: PositiveInt
    hold_sessions: PositiveInt

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.weak_below > self.strong_above:
            raise ValueError("pacing.weak_below cannot exceed pacing.strong_above")
        return self


class QuestionsConfig(FrozenModel):
    postpone_days: PositiveInt


class TwrConfig(FrozenModel):
    flow_timing: Literal["in_reading", "after_reading"]


class CapitalConfig(FrozenModel):
    """``whole_account``: the account's net liquidation value is managed; ``cap``: a book that
    starts at ``cap_base`` (base currency) and follows the owner's flows and its returns."""

    mode: Literal["whole_account", "cap"]
    cap_base: QuotedMoney | None
    budgets: BudgetsConfig
    satellite_ceiling: QuotedShare
    deployment: DeploymentConfig
    reservations: ReservationsConfig
    planner: PlannerConfig
    pacing: PacingConfig
    questions: QuestionsConfig
    twr: TwrConfig

    @model_validator(mode="after")
    def _cap_named(self) -> Self:
        if self.mode == "cap" and self.cap_base is None:
            raise ValueError("capital.mode cap needs capital.cap_base")
        if self.satellite_ceiling < self.budgets.equity_satellite:
            raise ValueError("capital.satellite_ceiling is below the equity satellite's budget")
        floor = self.budgets.core + self.budgets.option_premium + self.budgets.cash_floor
        if self.satellite_ceiling + floor > 1:
            raise ValueError(
                "capital.satellite_ceiling leaves less than the core, option and cash budgets"
            )
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


class TradingConfig(FrozenModel):
    min_trade: MinTradeConfig
    intent_ttl_minutes: PositiveInt


class TelegramConfig(FrozenModel):
    chunk_characters: PositiveInt
    timeout_seconds: PositiveFloat


class ReportingConfig(FrozenModel):
    telegram: TelegramConfig
    digest_rows: PositiveInt
