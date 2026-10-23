"""Session 11's acceptance: an offline fixture store yields both views with honest statuses.

The store is seeded by ``tests/support/measure_rig.py`` (synthetic, deterministic data written
through the repositories); the views are read back from the measurement store after one
``measure`` build, so what is asserted is what a reader of the store sees.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from measure_rig import AS_OF, QQQ, SPX, STAMP, seed

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.domain.measurement import EntityKind, MeasurementStatus
from obliskiq.grid import views
from obliskiq.grid.equity_grid import EquityGridBuilder
from obliskiq.grid.instrument_measures import InstrumentMeasures
from obliskiq.grid.macro_board import MacroBoardBuilder
from obliskiq.store import Store, measurements, open_database

OK, NA, STALE, INAPPLICABLE = (
    MeasurementStatus.OK,
    MeasurementStatus.NA,
    MeasurementStatus.STALE,
    MeasurementStatus.INAPPLICABLE,
)


@pytest.fixture(scope="module")
def built(
    tmp_path_factory: pytest.TempPathFactory,
    shipped_settings: ObliskConfig,
    calendars: Calendars,
) -> Iterator[Store]:
    """One seeded store and one build for the module: every test only reads it."""
    store = open_database(
        tmp_path_factory.mktemp("accept") / "accept.db",
        busy_timeout_ms=shipped_settings.store.busy_timeout_ms,
        synchronous=shipped_settings.store.synchronous,
    )
    seed(store, shipped_settings)
    grid = EquityGridBuilder(store, shipped_settings)
    board = MacroBoardBuilder(store, shipped_settings)
    instruments = InstrumentMeasures(store, shipped_settings, calendars)
    grid.write(grid.build(as_of=AS_OF, computed_at=STAMP))
    board.write(board.build(as_of=AS_OF, computed_at=STAMP))
    instruments.write(instruments.build(as_of=AS_OF, computed_at=STAMP))
    yield store
    store.close()


def test_the_equity_grid_view_spans_the_declared_axes(
    built: Store, shipped_settings: ObliskConfig
) -> None:
    view = views.equity_grid(built, shipped_settings, as_of=AS_OF)
    grid = shipped_settings.universe.grid
    assert view.session == AS_OF
    assert set(view.cells) == {(m, s) for m in grid.markets for s in grid.sectors}
    leaf_sets = {frozenset(cell.leaves) for cell in view.cells.values()}
    assert len(leaf_sets) == 1, "every cell carries the same leaves, whatever their status"
    families = {row.indicator for cell in view.cells.values() for row in cell.leaves.values()}
    assert len(families) == 15


def test_every_leaf_that_is_not_ok_says_why(built: Store, shipped_settings: ObliskConfig) -> None:
    view = views.equity_grid(built, shipped_settings, as_of=AS_OF)
    for cell in view.cells.values():
        for row in cell.leaves.values():
            if row.status is not OK:
                assert row.reason, (cell.market, cell.sector, row.leaf)
                assert row.reason_code, (cell.market, cell.sector, row.leaf)
            assert (row.value is not None) == row.status.has_value


def test_a_measured_cell_reads_its_native_instrument(
    built: Store, shipped_settings: ObliskConfig
) -> None:
    view = views.equity_grid(built, shipped_settings, as_of=AS_OF)
    us = view.cells[("us", "broad")]
    assert us.disclosure.symbol == "SPX"
    assert us.disclosure.kind == "native_index"
    assert us.leaves["return_63d_local"].status is OK
    assert us.leaves["return_63d_local"].source == f"bars:{SPX}"
    # USD is not the base: the base leg reads EURUSD and is measured too.
    assert us.leaves["return_63d_base"].status is OK
    # The benchmark's correlation to itself is structural, not uncollected.
    assert us.leaves["correlation_30d"].status is INAPPLICABLE
    assert us.leaves["correlation_30d"].reason_code == "cell_is_the_benchmark"
    # An index carries no volume.
    assert us.leaves["turnover_vs_average"].status is INAPPLICABLE
    # The CFTC contract the board declares for the US equity index is read.
    assert us.leaves["positioning_percentile"].status is OK


def test_the_option_families_read_the_chain_where_one_is_collected(
    built: Store, shipped_settings: ObliskConfig
) -> None:
    view = views.equity_grid(built, shipped_settings, as_of=AS_OF)
    tech = view.cells[("us", "technology")]
    assert tech.disclosure.symbol == "QQQ"
    for leaf in ("implied_vol_atm", "implied_vol_skew", "implied_vol_term_slope", "iv_minus_hv"):
        assert tech.leaves[leaf].status is OK, (leaf, tech.leaves[leaf].reason)
    # The chain was captured delayed, and the label travels on the leaf.
    assert tech.leaves["implied_vol_atm"].market_data_status is not None
    assert tech.leaves["implied_vol_atm"].market_data_status.value == "delayed"
    # A put skew: the risk reversal (call minus put) is negative.
    skew = tech.leaves["implied_vol_skew"].value
    assert skew is not None
    assert skew < 0
    # An instrument with a reachable chain and none collected is a collection gap.
    energy = view.cells[("us", "energy")]
    assert energy.leaves["implied_vol_atm"].status is NA
    assert energy.leaves["implied_vol_atm"].reason_code == "no_option_chain"


def test_unmapped_and_unseeded_cells_are_told_apart(
    built: Store, shipped_settings: ObliskConfig
) -> None:
    view = views.equity_grid(built, shipped_settings, as_of=AS_OF)
    declared = view.cells[("germany", "technology")]
    assert all(row.status is INAPPLICABLE for row in declared.leaves.values())
    assert {row.reason_code for row in declared.leaves.values()} == {"no_instrument_mapped"}
    unseeded = view.cells[("japan", "technology")]
    assert {row.reason_code for row in unseeded.leaves.values()} == {"instrument_not_seeded"}
    assert all(row.status is NA for row in unseeded.leaves.values())
    # The macro families still compute on the broad cell of a market nothing measures.
    japan = view.cells[("japan", "broad")]
    assert japan.leaves["return_5d_local"].reason_code == "instrument_not_seeded"
    assert japan.leaves["policy_rate"].reason_code == "macro_series_absent"
    assert japan.leaves["policy_rate_real"].status is INAPPLICABLE


def test_the_disclosure_flags_a_two_stock_fund(
    built: Store, shipped_settings: ObliskConfig
) -> None:
    view = views.equity_grid(built, shipped_settings, as_of=AS_OF)
    energy = view.cells[("us", "energy")].disclosure
    assert energy.top2_concentration == pytest.approx(0.41)
    assert energy.note is not None
    assert "TWO-STOCK READ" in energy.note
    staples = view.cells[("us", "consumer_staples")].disclosure
    assert staples.top2_concentration is None
    assert staples.note is not None
    assert "NOT SOURCED" in staples.note


def test_the_macro_board_view_spans_its_axes_with_honest_statuses(
    built: Store, shipped_settings: ObliskConfig
) -> None:
    view = views.macro_board(built, shipped_settings, as_of=AS_OF)
    board = shipped_settings.universe.macro_board
    assert view.session == AS_OF
    assert set(view.cells) == {(r, a) for r in board.regions for a in board.asset_classes}
    us = {name: view.cells[("us", name)] for name in board.asset_classes}
    assert us["rate_10y"].leaves["level"].status is OK
    assert us["rate_10y"].leaves["level"].units == "decimal"
    assert us["curve"].leaves["slope"].window == "10y_minus_2y"
    assert us["credit"].leaves["level_percentile"].status is OK
    assert us["vol_index"].leaves["term_slope"].status is OK
    assert us["fx_vs_base"].leaves["currency_return_12m"].status is OK
    assert us["equity_index"].leaves["return_63d_local"].status is OK
    # The real rate's CPI is an index (policy_minus_cpi_index_yoy): a year-on-year ratio.
    assert us["real_rate"].leaves["cpi_yoy"].status is OK
    # A cell the board declares unmapped carries its declared reason on every leaf.
    rate_2y = view.cells[("eurozone", "rate_2y")]
    assert {row.status for row in rate_2y.leaves.values()} == {INAPPLICABLE}
    assert all("two-year" in (row.reason or "") for row in rate_2y.leaves.values())
    # The world row's commodities read the futures curves.
    world = view.cells[("world", "commodities")]
    assert world.leaves["roll_yield_CL"].status is OK
    assert world.leaves["front_return_3m_CL"].status is OK
    assert world.leaves["roll_yield_NG"].reason_code == "no_futures_curve"


def test_instruments_carry_the_forecast_and_the_gap_distribution(built: Store) -> None:
    rows = {
        row.leaf: row
        for row in measurements.session_measurements(
            built, EntityKind.INSTRUMENT, as_of=AS_OF, entity_id=str(QQQ)
        )
    }
    assert rows["har_rv_forecast_21d"].status is OK
    assert 0.05 < (rows["har_rv_forecast_21d"].value or 0) < 0.6
    assert rows["gap_q5"].status is OK
    assert (rows["gap_q5"].value or 0) < 0 < (rows["gap_q95"].value or 0)
    # An ETF reports no earnings: a fact about it, not a gap.
    assert rows["gap_earnings_min"].status is INAPPLICABLE


def test_nothing_built_yields_empty_views(
    open_test_store: Callable[..., Store], tmp_path: Path, shipped_settings: ObliskConfig
) -> None:
    store = open_test_store(tmp_path / "empty.db")
    grid = views.equity_grid(store, shipped_settings, as_of=AS_OF)
    board = views.macro_board(store, shipped_settings, as_of=AS_OF)
    assert grid.session is None
    assert not grid.cells
    assert board.session is None
    assert not board.cells
