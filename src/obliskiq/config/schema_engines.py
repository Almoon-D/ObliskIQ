"""The shape of the engines' configuration: the options sleeve and its zombie test, the risk
engine's stops and rules, the FX engine and the commodity engine (lite's ``option_sleeve``,
``zombie_test``, ``equity_risk``, ``fx_inflation`` and ``commodities`` keys they read).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, PositiveFloat, PositiveInt, model_validator

from ..domain.base import NonEmptyText
from .base import FrozenModel

#: A probability or a share, inside (0, 1).
type Share = Annotated[float, Field(gt=0, lt=1)]
#: A money multiple or share, quoted, finite and positive.
type PositiveDecimal = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]

type ExitReason = Literal["target", "time_window", "trailing_stop"]
type Wrapper = Literal["eu_listed_physical_etc", "producer_equity", "futures_tracking_wrapper"]

#: The wrapper the contango ceiling refuses while it is breached.
FUTURES_TRACKING_WRAPPER = "futures_tracking_wrapper"


class SleeveActivationConfig(FrozenModel):
    """The sleeve's activation condition (DECISIONS D66): its hypothesis and the friction gate."""

    hypothesis: NonEmptyText
    friction_max_share_of_premium: Annotated[Decimal, Field(gt=0, lt=1, allow_inf_nan=False)]


class OptionSleeveConfig(FrozenModel):
    """lite's ``option_sleeve`` keys, less ``budget_pct`` (``capital.budgets.option_premium``)."""

    loss_penalty_multiple: PositiveDecimal
    recovery_basis: Literal["realised_gains"]
    qualifying_exit_reasons: list[ExitReason] = Field(min_length=1)
    min_payoff_ratio: PositiveFloat
    max_concurrent_positions: PositiveInt
    min_premium_base_currency: PositiveDecimal
    activation: SleeveActivationConfig

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        if len(set(self.qualifying_exit_reasons)) != len(self.qualifying_exit_reasons):
            raise ValueError("qualifying_exit_reasons lists a reason twice")
        return self


class ZombieTestConfig(FrozenModel):
    """lite's ``zombie_test``: four conditions, all on consecutive review sessions."""

    max_delta: Share
    max_dte: PositiveInt
    max_prob_target: Share
    max_prob_ratio_vs_entry: Share
    min_recoverable_pct_of_premium: Share
    consecutive_sessions_required: PositiveInt
    prob_vol_source: Literal["strike_smile_iv"]
    book_penalty_at: Literal["detection", "close"]


class EquityRiskConfig(FrozenModel):
    """lite's ``equity_risk`` keys and the ATR window its stops are scaled by."""

    stop_type: Literal["volatility_scaled"]
    stop_atr_multiple: PositiveFloat
    trailing_stop_enabled: bool
    trailing_stop_atr_multiple: PositiveFloat
    atr_period: PositiveInt
    allow_averaging_down: bool
    pre_earnings_rule: Literal["hold", "flag_and_size", "force_reduce"]
    pre_earnings_range_decile_trigger: Share


class FxInflationConfig(FrozenModel):
    """lite's ``fx_inflation`` keys the FX engine reads."""

    require_four_way_decomposition: bool
    high_inflation_cpi_yoy: PositiveFloat
    high_depreciation_3y_annualised: PositiveFloat
    depreciation_window_years: PositiveInt
    carry_illusion_tolerance: PositiveFloat


class CommodityTermStructureConfig(FrozenModel):
    contango_max_annualised_for_futures_wrapper: PositiveFloat


class CommodityCotConfig(FrozenModel):
    extreme_percentile: Annotated[float, Field(gt=0.5, lt=1)]
    require_price_confirmation: bool


class CommodityHistoryConfig(FrozenModel):
    seasonal_adjustment: bool


class CommodityInventoryConfig(FrozenModel):
    seasonal_window_years: PositiveInt
    seasonal_week_halfwidth: Annotated[int, Field(ge=0)]
    min_band_observations: Annotated[int, Field(ge=2)]


class CommoditiesConfig(FrozenModel):
    """The keys of lite's ``commodities`` block the commodity engine reads."""

    term_structure: CommodityTermStructureConfig
    preferred_wrapper_order: list[Wrapper] = Field(min_length=1)
    cot: CommodityCotConfig
    history: CommodityHistoryConfig
    inventory: CommodityInventoryConfig

    @model_validator(mode="after")
    def _wrappers(self) -> Self:
        order = self.preferred_wrapper_order
        if len(set(order)) != len(order):
            raise ValueError("preferred_wrapper_order lists a wrapper twice")
        if order == [FUTURES_TRACKING_WRAPPER]:
            raise ValueError(
                "preferred_wrapper_order needs a wrapper besides the futures-tracking one, which "
                "the contango ceiling refuses"
            )
        return self
