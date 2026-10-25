"""The numerical configuration: shipped values load, and nonsense is refused at the door.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from typing import Any

import pytest
from pydantic import ValidationError

from obliskiq.config import ObliskConfig
from obliskiq.config.schema_numerics import LiquidityConfig, SmileConfig, SolveConfig


def _solve(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "min_dte": 90,
        "max_dte": 200,
        "moneyness_band": 0.15,
        "max_non_converged_fraction": 0.2,
        "risk_free_series": {"USD": "DTB3"},
    }
    fields.update(overrides)
    return fields


def _smile(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "min_abs_delta": 0.05,
        "max_abs_delta": 0.95,
        "butterfly_max_iv_deviation": 0.1,
        "min_points": 2,
        "skew_delta": 0.25,
        "term_structure": {
            "front_target_dte": 95,
            "back_target_dte": 180,
            "max_target_deviation_days": 35,
            "min_separation_days": 45,
        },
    }
    fields.update(overrides)
    return fields


def test_the_shipped_values_are_the_expert_defaults(shipped_settings: ObliskConfig) -> None:
    numerics = shipped_settings.numerics
    assert numerics.implied_vol.seed_method == "brenner_subrahmanyam"
    assert numerics.bsm.min_sigma < numerics.implied_vol.seed_min_sigma
    assert numerics.implied_vol.seed_max_sigma < numerics.bsm.max_sigma
    assert numerics.implied_vol.solve.min_dte < numerics.implied_vol.solve.max_dte
    assert shipped_settings.smile.min_abs_delta < shipped_settings.smile.max_abs_delta
    assert 0 < shipped_settings.liquidity.max_spread_pct < 1


def test_a_solve_band_must_run_forwards() -> None:
    with pytest.raises(ValidationError, match="max_dte must not be below min_dte"):
        SolveConfig.model_validate(_solve(min_dte=200, max_dte=100))
    assert SolveConfig.model_validate(_solve(min_dte=100, max_dte=100))


@pytest.mark.parametrize("band", [1.0, 1.5])
def test_a_moneyness_band_is_a_fraction_of_spot(band: float) -> None:
    with pytest.raises(ValidationError, match="fraction of spot"):
        SolveConfig.model_validate(_solve(moneyness_band=band))


def test_the_non_convergence_ceiling_is_a_fraction() -> None:
    with pytest.raises(ValidationError, match="must not exceed 1"):
        SolveConfig.model_validate(_solve(max_non_converged_fraction=1.5))
    assert SolveConfig.model_validate(_solve(max_non_converged_fraction=1.0))
    assert SolveConfig.model_validate(_solve(max_non_converged_fraction=0.0))


@pytest.mark.parametrize(
    "overrides",
    [
        {"min_abs_delta": 0.9, "max_abs_delta": 0.5},
        {"min_abs_delta": 0.5, "max_abs_delta": 0.5},
        {"max_abs_delta": 1.2},
    ],
)
def test_the_delta_band_must_be_ordered_and_inside_unity(overrides: dict[str, float]) -> None:
    with pytest.raises(ValidationError, match="0 < min_abs_delta < max_abs_delta <= 1"):
        SmileConfig.model_validate(_smile(**overrides))
    assert SmileConfig.model_validate(_smile(max_abs_delta=1.0))


@pytest.mark.parametrize("field", ["butterfly_max_iv_deviation", "min_points"])
def test_a_smile_threshold_must_be_positive(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        SmileConfig.model_validate(_smile(**{field: 0}))


LIQUIDITY = {
    "max_spread_pct": 0.15,
    "spread_waiver_min_open_interest": 5000,
    "min_open_interest": 500,
    "require_session_volume": True,
}


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_spread_pct": 0.0},
        {"min_open_interest": 0},
        {"require_session_volume": "perhaps"},
        {"spread_waiver_min_open_interest": 500},
    ],
)
def test_a_liquidity_gate_must_be_meaningful(overrides: dict[str, Any]) -> None:
    assert LiquidityConfig.model_validate(LIQUIDITY)
    with pytest.raises(ValidationError):
        LiquidityConfig.model_validate({**LIQUIDITY, **overrides})


def test_the_spread_waiver_is_for_a_book_deeper_than_the_floor() -> None:
    with pytest.raises(ValidationError, match="must exceed min_open_interest"):
        LiquidityConfig.model_validate({**LIQUIDITY, "spread_waiver_min_open_interest": 499})
