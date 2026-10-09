"""The shape of the numerical configuration: the pricing engine, the implied-volatility solver,
smile hygiene and the liquidity tests the smile shares with the gates.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from typing import Literal, Self

from pydantic import NonNegativeFloat, PositiveFloat, PositiveInt, model_validator

from .base import FrozenModel


class BsmConfig(FrozenModel):
    day_count_basis: PositiveFloat
    min_tau_years: PositiveFloat
    min_sigma: PositiveFloat
    max_sigma: PositiveFloat
    min_price: PositiveFloat
    vega_per_point_divisor: PositiveFloat
    theta_per_day_divisor: PositiveFloat

    @model_validator(mode="after")
    def _sigma_range_is_ordered(self) -> Self:
        if self.max_sigma <= self.min_sigma:
            raise ValueError("requires 0 < min_sigma < max_sigma")
        return self


class SolveConfig(FrozenModel):
    min_dte: PositiveInt
    max_dte: PositiveInt
    moneyness_band: PositiveFloat
    max_non_converged_fraction: NonNegativeFloat

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.max_dte < self.min_dte:
            raise ValueError("max_dte must not be below min_dte")
        if self.moneyness_band >= 1.0:
            raise ValueError("moneyness_band is a fraction of spot and must be below 1")
        if self.max_non_converged_fraction > 1.0:
            raise ValueError("max_non_converged_fraction is a fraction and must not exceed 1")
        return self


class ImpliedVolConfig(FrozenModel):
    seed_method: Literal["brenner_subrahmanyam", "fixed"]
    seed_fixed_sigma: PositiveFloat
    seed_min_sigma: PositiveFloat
    seed_max_sigma: PositiveFloat
    max_newton_iterations: PositiveInt
    max_bisection_iterations: PositiveInt
    price_abs_tolerance: PositiveFloat
    price_rel_tolerance: PositiveFloat
    min_vega: PositiveFloat
    min_vega_for_reported_iv: PositiveFloat
    reject_below_intrinsic: bool
    reject_above_upper_bound: bool
    solve: SolveConfig

    @model_validator(mode="after")
    def _seed_range_is_ordered(self) -> Self:
        if self.seed_max_sigma <= self.seed_min_sigma:
            raise ValueError("requires seed_min_sigma < seed_max_sigma")
        return self


class NumericsConfig(FrozenModel):
    bsm: BsmConfig
    implied_vol: ImpliedVolConfig

    @model_validator(mode="after")
    def _seed_range_sits_inside_the_sigma_range(self) -> Self:
        solver, engine = self.implied_vol, self.bsm
        if solver.seed_min_sigma < engine.min_sigma or solver.seed_max_sigma > engine.max_sigma:
            raise ValueError(
                "implied_vol seed range must sit inside bsm.min_sigma .. bsm.max_sigma"
            )
        return self


class SmileConfig(FrozenModel):
    min_abs_delta: PositiveFloat
    max_abs_delta: PositiveFloat
    butterfly_max_iv_deviation: PositiveFloat
    min_points: PositiveInt

    @model_validator(mode="after")
    def _delta_band_is_ordered(self) -> Self:
        if not self.min_abs_delta < self.max_abs_delta <= 1.0:
            raise ValueError("requires 0 < min_abs_delta < max_abs_delta <= 1")
        return self


class LiquidityConfig(FrozenModel):
    max_spread_pct: PositiveFloat
    min_open_interest: PositiveInt
    require_session_volume: bool
