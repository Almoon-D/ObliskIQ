"""The shape of the measurement store's configuration: the equity grid and the macro board, the
ranked views and the correlation matrix, the census, the realised-volatility forecast and the
overnight-gap distributions (MASTER_PLAN §5.7; lite's ``grid``, ``census``, ``screen`` and the
``data`` keys its grid read).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from typing import Annotated, Literal, Self

from pydantic import Field, PositiveFloat, PositiveInt, model_validator

from .base import FrozenModel, Identifier

#: A share of something, strictly inside (0, 1).
type Share = Annotated[float, Field(gt=0, lt=1)]


def _distinct_ascending(values: list[int], name: str) -> None:
    if values != sorted(set(values)):
        raise ValueError(f"{name} must be distinct and ascending")


class FreshnessDaysConfig(FrozenModel):
    """One freshness window per input store, in calendar days."""

    daily_bars: PositiveInt
    option_chain: PositiveInt
    underlying_snapshot: PositiveInt
    macro: PositiveInt


class MinObservationsConfig(FrozenModel):
    """The floors below which a family refuses to compute."""

    returns: PositiveInt
    correlation: PositiveInt
    percentile: PositiveInt
    turnover: PositiveInt


class MacroBoardSettings(FrozenModel):
    """How the macro board reads a series: changes over horizons, a level ranked over a window."""

    change_horizons_months: list[PositiveInt] = Field(min_length=1)
    percentile_window_years: PositiveInt

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        _distinct_ascending(self.change_horizons_months, "macro_board.change_horizons_months")
        return self


class HarRvConfig(FrozenModel):
    """The HAR-RV forecast: its components, the horizons it forecasts, the history it needs."""

    weekly_sessions: PositiveInt
    monthly_sessions: PositiveInt
    horizons_days: list[PositiveInt] = Field(min_length=1)
    min_sessions: PositiveInt

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if not 1 < self.weekly_sessions < self.monthly_sessions:
            raise ValueError("har_rv needs 1 < weekly_sessions < monthly_sessions")
        _distinct_ascending(self.horizons_days, "har_rv.horizons_days")
        if self.min_sessions <= self.monthly_sessions + max(self.horizons_days):
            raise ValueError(
                "har_rv.min_sessions must exceed monthly_sessions plus the longest horizon, or "
                "no training row would exist"
            )
        return self


class OvernightGapsConfig(FrozenModel):
    """Which quantiles of the overnight-gap distribution are reported, and on how many gaps."""

    quantiles: list[Share] = Field(min_length=1)
    min_gaps: PositiveInt

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.quantiles != sorted(set(self.quantiles)):
            raise ValueError("overnight_gaps.quantiles must be distinct and ascending")
        return self


class OptionSurfaceConfig(FrozenModel):
    """The tenors and wing deltas the option surface reads each underlying at."""

    tenors_days: list[PositiveInt] = Field(min_length=1)
    deltas: list[Share] = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        _distinct_ascending(self.tenors_days, "option_surface.tenors_days")
        if self.deltas != sorted(set(self.deltas)):
            raise ValueError("option_surface.deltas must be distinct and ascending")
        return self


class GridConfig(FrozenModel):
    benchmark_market: Identifier
    correlation_shift_sd: PositiveFloat
    correlation_norm_window_years: PositiveInt
    correlation_norm_min_independent: PositiveInt
    realised_vol_percentile_min_independent: PositiveInt
    freshness_days: FreshnessDaysConfig
    min_coverage_pct: Share
    min_cells_for_ranked_view: PositiveInt
    min_observations: MinObservationsConfig
    weekly_window_years: PositiveInt
    rv_tenors_days: list[PositiveInt] = Field(min_length=2)
    correlation_windows_days: list[PositiveInt] = Field(min_length=1)
    technical_window_days: PositiveInt
    atm_iv_prefer: Literal["vendor", "solved", "chain_first"]
    iv_self_source_min_sessions: PositiveInt
    iv_percentile_window_weeks: PositiveInt
    garman_klass_max_restated_open_share: Share
    etf_top2_concentration_flag: Share
    ranking_top_n: PositiveInt
    macro_board: MacroBoardSettings
    har_rv: HarRvConfig
    overnight_gaps: OvernightGapsConfig
    option_surface: OptionSurfaceConfig

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        _distinct_ascending(self.rv_tenors_days, "grid.rv_tenors_days")
        _distinct_ascending(self.correlation_windows_days, "grid.correlation_windows_days")
        return self


class CensusConfig(FrozenModel):
    min_viable_contracts: PositiveInt


class ScreenConfig(FrozenModel):
    """The quartile boundaries behind "cheap" and "extended", on a percentile."""

    cheap_iv_percentile_max: Share
    extended_iv_percentile_min: Share

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.cheap_iv_percentile_max >= self.extended_iv_percentile_min:
            raise ValueError("cheap_iv_percentile_max must sit below extended_iv_percentile_min")
        return self
