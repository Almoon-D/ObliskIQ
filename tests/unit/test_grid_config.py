"""The measurement layer's configuration: lite's values where lite had them, and the checks that
refuse a board, a benchmark, a term structure or a discount rate the code could not read.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import copy
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from obliskiq.config import ObliskConfig
from obliskiq.config.schema import ObliskConfig as Schema
from obliskiq.config.schema_grid import GridConfig, HarRvConfig, OvernightGapsConfig, ScreenConfig
from obliskiq.config.schema_numerics import SmileConfig, TermStructureConfig
from obliskiq.config.schema_universe import BOARD_ASSET_CLASSES, UniverseConfig

type Data = dict[str, Any]


def test_the_ported_values_are_lites(shipped_settings: ObliskConfig) -> None:
    grid = shipped_settings.grid
    assert grid.benchmark_market == "us"
    assert grid.correlation_shift_sd == 1.0
    assert grid.correlation_norm_window_years == 2
    assert grid.correlation_norm_min_independent == 4
    assert grid.realised_vol_percentile_min_independent == 8
    assert grid.freshness_days.model_dump() == {
        "daily_bars": 5,
        "option_chain": 5,
        "underlying_snapshot": 5,
        "macro": 45,
    }
    assert grid.min_coverage_pct == 0.20
    assert grid.min_cells_for_ranked_view == 5
    assert grid.min_observations.model_dump() == {
        "returns": 2,
        "correlation": 21,
        "percentile": 26,
        "turnover": 21,
    }
    assert (grid.weekly_window_years, grid.technical_window_days) == (5, 90)
    assert grid.rv_tenors_days == [10, 30, 90]
    assert grid.correlation_windows_days == [30, 90, 250]
    assert grid.atm_iv_prefer == "vendor"
    assert (grid.iv_self_source_min_sessions, grid.iv_percentile_window_weeks) == (252, 52)
    assert grid.garman_klass_max_restated_open_share == 0.10
    assert grid.etf_top2_concentration_flag == 0.40
    assert grid.ranking_top_n == 20
    assert shipped_settings.census.min_viable_contracts == 3
    assert shipped_settings.screen.cheap_iv_percentile_max == 0.25
    assert shipped_settings.screen.extended_iv_percentile_min == 0.75
    assert shipped_settings.smile.skew_delta == 0.25
    terms = shipped_settings.smile.term_structure
    assert (terms.front_target_dte, terms.back_target_dte) == (95, 180)
    assert (terms.max_target_deviation_days, terms.min_separation_days) == (35, 45)


def test_the_term_structure_targets_sit_inside_the_band_the_collector_buys(
    shipped_settings: ObliskConfig,
) -> None:
    """Outside it the leaf would be n/a on every cell for ever with nothing saying why."""
    terms, tenor = shipped_settings.smile.term_structure, shipped_settings.tenor
    assert (
        tenor.entry_dte_min <= terms.front_target_dte < terms.back_target_dte <= tenor.entry_dte_max
    )
    data = shipped_settings.model_dump(mode="json")
    data["smile"]["term_structure"]["front_target_dte"] = 30
    with pytest.raises(ValidationError, match=r"tenor\.entry_dte_min"):
        Schema.model_validate(data)


def test_the_benchmark_must_be_a_market_with_a_native_index(shipped_settings: ObliskConfig) -> None:
    data = shipped_settings.model_dump(mode="json")
    data["grid"]["benchmark_market"] = "mars"
    with pytest.raises(ValidationError, match=r"grid\.benchmark_market"):
        Schema.model_validate(data)


def test_every_discount_rate_is_a_declared_percent_series(shipped_settings: ObliskConfig) -> None:
    data = shipped_settings.model_dump(mode="json")
    data["numerics"]["implied_vol"]["solve"]["risk_free_series"]["JPY"] = "NOT_DECLARED"
    with pytest.raises(ValidationError, match=r"risk_free_series\.JPY"):
        Schema.model_validate(data)
    declared = {s.id for s in shipped_settings.sources.macro.series}
    assert set(shipped_settings.numerics.implied_vol.solve.risk_free_series.values()) <= declared


def test_every_grid_market_reads_a_board_region_of_its_name(shipped_settings: ObliskConfig) -> None:
    assert set(shipped_settings.universe.grid.markets) <= set(
        shipped_settings.universe.macro_board.regions
    )


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"rv_tenors_days": [30, 10]}, "ascending"),
        ({"correlation_windows_days": [30, 30]}, "ascending"),
        ({"min_coverage_pct": 1.0}, "less than 1"),
        ({"atm_iv_prefer": "whatever"}, "atm_iv_prefer"),
    ],
)
def test_a_malformed_grid_section_is_refused(
    shipped_settings: ObliskConfig, change: Data, match: str
) -> None:
    data = {**shipped_settings.grid.model_dump(mode="json"), **change}
    with pytest.raises(ValidationError, match=match):
        GridConfig.model_validate(data)


def test_a_har_fit_needs_room_for_a_training_row(shipped_settings: ObliskConfig) -> None:
    data = shipped_settings.grid.har_rv.model_dump()
    with pytest.raises(ValidationError, match="min_sessions"):
        HarRvConfig.model_validate({**data, "min_sessions": 50})
    with pytest.raises(ValidationError, match="weekly_sessions"):
        HarRvConfig.model_validate({**data, "weekly_sessions": 30})


def test_cheap_sits_below_extended() -> None:
    with pytest.raises(ValidationError, match="below"):
        ScreenConfig.model_validate(
            {"cheap_iv_percentile_max": 0.8, "extended_iv_percentile_min": 0.75}
        )


# ── the board's labels ──


@pytest.fixture(scope="module")
def universe(shipped_settings: ObliskConfig) -> Data:
    return shipped_settings.universe.model_dump(mode="json")


def refused(universe: Data, change: Callable[[Data], None], match: str) -> None:
    data = copy.deepcopy(universe)
    change(data)
    with pytest.raises(ValidationError, match=match):
        UniverseConfig.model_validate(data)


def cell(data: Data, region: str, asset_class: str) -> Data:
    found: Data = data["macro_board"]["regions"][region]["cells"][asset_class]
    return found


def test_an_asset_class_the_board_cannot_read_is_refused(universe: Data) -> None:
    def add(d: Data) -> None:
        d["macro_board"]["asset_classes"].append("housing")
        for region in d["macro_board"]["regions"].values():
            region["cells"]["housing"] = {"series": [], "roots": [], "label": None, "unmapped": "x"}

    refused(universe, add, "does not know how to read")
    assert set(BOARD_ASSET_CLASSES) == set(universe["macro_board"]["asset_classes"])


def test_a_label_nothing_computes_or_the_wrong_series_count_is_refused(universe: Data) -> None:
    refused(
        universe,
        lambda d: cell(d, "us", "curve").update(label="10y_minus_5y"),
        "does not compute there",
    )
    refused(
        universe,
        lambda d: cell(d, "us", "curve").update(series=["DGS10"]),
        "combines 2 series, not 1",
    )
    refused(
        universe,
        lambda d: cell(d, "us", "rate_10y").update(series=["DGS10", "DGS2"]),
        "reads one series",
    )
    refused(
        universe,
        lambda d: cell(d, "us", "curve").update(label=None),
        "must say how",
    )


def test_the_policy_rate_must_agree_between_the_real_rate_and_the_curve(universe: Data) -> None:
    refused(
        universe,
        lambda d: cell(d, "eurozone", "curve").update(series=["IRLTLT01EZM156N", "DFF"]),
        "disagree",
    )


def test_quantiles_run_upwards_and_term_targets_keep_their_separation(
    shipped_settings: ObliskConfig,
) -> None:
    with pytest.raises(ValidationError, match="ascending"):
        OvernightGapsConfig.model_validate({"quantiles": [0.5, 0.05], "min_gaps": 60})
    terms = shipped_settings.smile.term_structure.model_dump()
    with pytest.raises(ValidationError, match="min_separation_days apart"):
        TermStructureConfig.model_validate({**terms, "back_target_dte": 120})
    smile = shipped_settings.smile.model_dump()
    with pytest.raises(ValidationError, match="inside the delta band"):
        SmileConfig.model_validate({**smile, "skew_delta": 0.99})


def test_a_cell_naming_inputs_its_asset_class_does_not_read_is_refused(universe: Data) -> None:
    refused(
        universe,
        lambda d: cell(d, "us", "equity_index").update(roots=["CL"]),
        "only a commodities cell reads them",
    )
    refused(
        universe,
        lambda d: cell(d, "world", "commodities").update(series=["DGS10"]),
        "a commodities cell reads futures roots",
    )
    refused(
        universe,
        lambda d: cell(d, "us", "vol_index").update(label=None),
        "names 2 series without a label",
    )
    refused(
        universe,
        lambda d: cell(d, "world", "equity_index").update(series=["DGS10"]),
        "without a label saying what they are",
    )
