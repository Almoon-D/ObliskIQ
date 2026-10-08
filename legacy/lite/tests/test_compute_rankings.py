"""The four ranked views, and the cross-market correlation matrix.

The session reads rankings, not sixteen hundred raw cells. Each view ranks on
one named indicator so the ordering can be checked against the cell it came
from, and the matrix compares every pair against its OWN norm rather than a
shared threshold.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from itertools import accumulate

import pytest

from obliskiq.compute.indicators import (
    INDICATOR_FUNCTIONS,
    IndicatorInputs,
    positioning_change_indicator,
)
from obliskiq.compute.rankings import (
    DIMENSION_LARGEST_CORRELATION_SHIFT,
    DIMENSION_MOST_COMPRESSED,
    DIMENSION_MOST_EXTENDED,
    DIMENSION_WIDEST_IV_HV,
    DIMENSIONS,
    CorrelationMatrix,
    Rankings,
)

AS_OF = date(2026, 3, 2)


def _cell(store, market, sector, indicator, value, *, family="returns", status="ok", window=None):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO grid (as_of, market, sector, indicator, value, status, reason, "
            "window, indicator_family, units) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'decimal') "
            "ON CONFLICT (as_of, market, sector, indicator) DO UPDATE SET value = excluded.value",
            (
                AS_OF.isoformat(),
                market,
                sector,
                indicator,
                value,
                status,
                None if status == "ok" else "seeded",
                window,
                family,
            ),
        )


@pytest.fixture()
def rankings(store, config):
    return Rankings(store, config)


# ── the four views ──


def test_most_extended_ranks_nearest_the_high_first(store, rankings):
    _cell(store, "us", "broad", "distance_from_52w_high", -0.02)
    _cell(store, "japan", "broad", "distance_from_52w_high", -0.35)
    _cell(store, "brazil", "broad", "distance_from_52w_high", -0.18)

    rows = [r for r in rankings.build(as_of=AS_OF) if r.dimension == DIMENSION_MOST_EXTENDED]

    assert [row.market for row in rows] == ["us", "brazil", "japan"]
    assert rows[0].rank == 1
    assert rows[0].indicator == "distance_from_52w_high"


def test_most_compressed_ranks_the_lowest_parkinson_percentile_first(store, config, rankings):
    """The view orders on the RANGE-BASED estimator, and says which one it was.

    Parkinson reads the intraday range the store has always carried and is far
    less noisy on the same sample. It is not the same quantity as the
    close-to-close figure — it sees no overnight gap — which is why the row names
    the leaf it ordered on rather than leaving a reader to assume.
    """
    tenor = config.require_int("data.strike_bounds_rv_tenor_days")
    indicator = f"parkinson_vol_{tenor}d_percentile"
    _cell(store, "us", "broad", indicator, 0.80, family="realised_vol")
    _cell(store, "korea", "broad", indicator, 0.05, family="realised_vol")
    _cell(store, "germany", "broad", indicator, 0.40, family="realised_vol")

    rows = [r for r in rankings.build(as_of=AS_OF) if r.dimension == DIMENSION_MOST_COMPRESSED]

    assert [row.market for row in rows] == ["korea", "germany", "us"]
    assert rows[0].indicator == indicator
    assert "compression is the setup" in rows[0].detail


def test_the_close_to_close_percentile_alone_no_longer_ranks_a_cell(store, config, rankings):
    """An ADDITIONAL leaf, never a swap — and the swap has to be visible.

    A cell holding only the close-to-close percentile is not ranked by this view
    any more. That is the honest outcome rather than a regression: the view names
    ``parkinson_vol_<tenor>d_percentile`` as what it ordered on, and ranking a
    close-to-close reading under that name would be the estimator confusion
    docs/DECISIONS.md records reaching opposite verdicts on ASML.
    """
    tenor = config.require_int("data.strike_bounds_rv_tenor_days")
    _cell(store, "us", "broad", f"realised_vol_{tenor}d_percentile", 0.05, family="realised_vol")

    rows = [r for r in rankings.build(as_of=AS_OF) if r.dimension == DIMENSION_MOST_COMPRESSED]
    assert rows == []


def test_widest_iv_hv_surfaces_both_tails(store, rankings):
    """Implied far below realised is cheap; far above is expensive. Both matter."""
    _cell(store, "us", "broad", "iv_minus_hv", 0.02, family="iv_hv_spread")
    _cell(store, "korea", "broad", "iv_minus_hv", -0.25, family="iv_hv_spread")
    _cell(store, "japan", "broad", "iv_minus_hv", 0.18, family="iv_hv_spread")

    rows = [r for r in rankings.build(as_of=AS_OF) if r.dimension == DIMENSION_WIDEST_IV_HV]

    assert [row.market for row in rows] == ["korea", "japan", "us"]
    assert rows[0].value == -0.25, "the signed value is reported, the magnitude ranked"


def test_the_largest_correlation_shift_ranks_across_every_window(store, rankings):
    _cell(store, "us", "broad", "correlation_shift_30d", 0.4, family="correlation", window="30d")
    _cell(
        store, "korea", "broad", "correlation_shift_250d", -2.6, family="correlation", window="250d"
    )
    _cell(store, "japan", "broad", "correlation_shift_90d", 1.2, family="correlation", window="90d")

    rows = [
        r for r in rankings.build(as_of=AS_OF) if r.dimension == DIMENSION_LARGEST_CORRELATION_SHIFT
    ]

    assert [row.market for row in rows] == ["korea", "japan", "us"]
    assert rows[0].window == "250d", "each row keeps the window it came from"


def test_every_dimension_names_the_indicator_it_ranked_on(store, config, rankings):
    tenor = config.require_int("data.strike_bounds_rv_tenor_days")
    _cell(store, "us", "broad", "distance_from_52w_high", -0.02)
    _cell(store, "us", "broad", f"parkinson_vol_{tenor}d_percentile", 0.1, family="realised_vol")
    _cell(store, "us", "broad", "iv_minus_hv", -0.1, family="iv_hv_spread")
    _cell(store, "us", "broad", "correlation_shift_30d", 2.0, family="correlation", window="30d")

    rows = rankings.build(as_of=AS_OF)
    assert {row.dimension for row in rows} == set(DIMENSIONS)
    for row in rows:
        assert row.indicator, "a ranking with no named indicator cannot be audited"


def test_an_absent_cell_is_never_ranked(store, rankings):
    _cell(store, "us", "broad", "distance_from_52w_high", -0.02)
    _cell(store, "japan", "broad", "distance_from_52w_high", None, status="n/a")

    rows = [r for r in rankings.build(as_of=AS_OF) if r.dimension == DIMENSION_MOST_EXTENDED]
    assert [row.market for row in rows] == ["us"]


def test_a_stale_cell_is_ranked_but_keeps_its_status_upstream(store, rankings):
    """Stale is indicative, not absent: excluding it would hide the whole market."""
    _cell(store, "us", "broad", "distance_from_52w_high", -0.02, status="stale")
    rows = [r for r in rankings.build(as_of=AS_OF) if r.dimension == DIMENSION_MOST_EXTENDED]
    assert [row.market for row in rows] == ["us"]


def test_the_top_n_is_respected(store, config, rankings):
    top_n = config.require_int("data.grid_ranking_top_n")
    for index in range(top_n + 5):
        _cell(store, f"m{index}", "broad", "distance_from_52w_high", -0.01 * index)

    rows = [r for r in rankings.build(as_of=AS_OF) if r.dimension == DIMENSION_MOST_EXTENDED]
    assert len(rows) == top_n


def test_rankings_round_trip_and_a_shorter_rebuild_leaves_no_tail(store, rankings):
    for index in range(5):
        _cell(store, f"m{index}", "broad", "distance_from_52w_high", -0.01 * index)
    rankings.write(rankings.build(as_of=AS_OF), as_of=AS_OF)
    assert len(rankings.read(as_of=AS_OF, dimension=DIMENSION_MOST_EXTENDED)) == 5

    with store.transaction() as connection:
        connection.execute(
            "DELETE FROM grid WHERE market IN ('m2', 'm3', 'm4') AND as_of = ?",
            (AS_OF.isoformat(),),
        )
    rankings.write(rankings.build(as_of=AS_OF), as_of=AS_OF)

    rows = rankings.read(as_of=AS_OF, dimension=DIMENSION_MOST_EXTENDED)
    assert len(rows) == 2, "yesterday's tail must not survive a shorter rebuild"


# ── the correlation matrix ──


def _index(store, contract_id, symbol, market):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class, market, "
            "sector, is_native_index, grid_measures, tradable, untradeable_reason) "
            "VALUES (?, ?, 'USD', 'IND', ?, 'broad', 1, 1, 0, 'index level')",
            (contract_id, symbol, market),
        )


def _walk(store, contract_id, *, sessions, shape):
    rows = []
    level = 100.0
    for index in range(sessions):
        level *= math.exp(shape(index))
        day = AS_OF - timedelta(days=sessions - index)
        rows.append((contract_id, day.isoformat(), "1d", level))
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO bars (contract_id, ts, freq, close, source) VALUES (?, ?, ?, ?, 'cp_api')",
            rows,
        )


MARKETS = {
    "us": {"currency": "USD", "native_index": "SPX"},
    "japan": {"currency": "JPY", "native_index": "N225"},
}


def test_a_pair_that_moves_together_correlates(store, config):
    _index(store, 1, "SPX", "us")
    _index(store, 2, "N225", "japan")
    shape = lambda n: 0.01 * math.sin(n / 5.0)  # noqa: E731
    _walk(store, 1, sessions=600, shape=shape)
    _walk(store, 2, sessions=600, shape=shape)

    matrix = CorrelationMatrix(store, config, MARKETS)
    cells = {cell.window_days: cell for cell in matrix.build(as_of=AS_OF)}

    for window, cell in cells.items():
        assert cell.status == "ok", f"{window}d: {cell.reason}"
        assert cell.correlation == pytest.approx(1.0, abs=1e-6)


def test_a_measured_correlation_with_no_norm_stays_ok_and_says_why(store, config):
    """The correlation is data; only the norm is missing. Two locked series have
    no dispersion in their own history, so there is nothing to deviate from."""
    _index(store, 1, "SPX", "us")
    _index(store, 2, "N225", "japan")
    shape = lambda n: 0.01 * math.sin(n / 5.0)  # noqa: E731
    _walk(store, 1, sessions=600, shape=shape)
    _walk(store, 2, sessions=600, shape=shape)

    cells = CorrelationMatrix(store, config, MARKETS).build(as_of=AS_OF)
    without_norm = [cell for cell in cells if cell.z_score is None and cell.correlation]

    assert without_norm, "identical series should leave at least one norm unmeasurable"
    for cell in without_norm:
        assert cell.status == "ok"
        assert not cell.flagged
        assert "no norm to deviate from" in cell.reason


def test_every_configured_window_is_computed(store, config):
    _index(store, 1, "SPX", "us")
    _index(store, 2, "N225", "japan")
    shape = lambda n: 0.01 * math.sin(n / 7.0)  # noqa: E731
    _walk(store, 1, sessions=700, shape=shape)
    _walk(store, 2, sessions=700, shape=lambda n: 0.01 * math.cos(n / 7.0))

    matrix = CorrelationMatrix(store, config, MARKETS)
    windows = {cell.window_days for cell in matrix.build(as_of=AS_OF)}
    assert windows == set(config.require_list("data.correlation_windows_days"))


def test_a_market_with_no_bars_is_na_and_names_the_gap(store, config):
    _index(store, 1, "SPX", "us")
    _walk(store, 1, sessions=400, shape=lambda n: 0.001)

    matrix = CorrelationMatrix(store, config, MARKETS)
    cells = matrix.build(as_of=AS_OF)

    assert all(cell.status == "n/a" for cell in cells)
    assert all("native index" in cell.reason for cell in cells)
    assert all("not a substitute" in cell.reason for cell in cells)


def test_a_pair_is_flagged_against_its_own_norm(store, config):
    """Two markets always at 0.9 and two never correlated both deviate meaningfully."""
    _index(store, 1, "SPX", "us")
    _index(store, 2, "N225", "japan")
    # A long history of near-lockstep movement, then a sharp decoupling at the end.
    sessions = 700
    rows_a, rows_b = [], []
    level_a = level_b = 100.0
    for index in range(sessions):
        step = 0.01 * math.sin(index / 5.0)
        decoupled = index > sessions - 40
        level_a *= math.exp(step)
        level_b *= math.exp(-step if decoupled else step)
        day = AS_OF - timedelta(days=sessions - index)
        rows_a.append((1, day.isoformat(), "1d", level_a))
        rows_b.append((2, day.isoformat(), "1d", level_b))
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO bars (contract_id, ts, freq, close, source) VALUES (?, ?, ?, ?, 'cp_api')",
            rows_a + rows_b,
        )

    matrix = CorrelationMatrix(store, config, MARKETS)
    cells = matrix.build(as_of=AS_OF)
    thirty = next(c for c in cells if c.window_days == 30)

    assert thirty.status == "ok"
    assert thirty.z_score is not None
    assert thirty.flagged, "a market decoupling from its usual driver is a regime signal"
    assert abs(thirty.z_score) > config.require_float("grid.correlation_shift_sd")


def test_the_matrix_round_trips_and_flagged_pairs_are_queryable(store, config):
    _index(store, 1, "SPX", "us")
    _index(store, 2, "N225", "japan")
    shape = lambda n: 0.01 * math.sin(n / 5.0)  # noqa: E731
    _walk(store, 1, sessions=700, shape=shape)
    _walk(store, 2, sessions=700, shape=shape)

    matrix = CorrelationMatrix(store, config, MARKETS)
    cells = matrix.build(as_of=AS_OF)
    assert matrix.write(cells, as_of=AS_OF) == len(cells)

    stored = store.execute(
        "SELECT COUNT(*) FROM correlation_matrix WHERE as_of = ?", (AS_OF.isoformat(),)
    ).fetchone()[0]
    assert stored == len(cells)
    assert isinstance(matrix.flagged(as_of=AS_OF), list)


def test_writing_the_matrix_twice_updates_rather_than_duplicating(store, config):
    _index(store, 1, "SPX", "us")
    _index(store, 2, "N225", "japan")
    shape = lambda n: 0.01 * math.sin(n / 5.0)  # noqa: E731
    _walk(store, 1, sessions=700, shape=shape)
    _walk(store, 2, sessions=700, shape=shape)

    matrix = CorrelationMatrix(store, config, MARKETS)
    cells = matrix.build(as_of=AS_OF)
    matrix.write(cells, as_of=AS_OF)
    matrix.write(cells, as_of=AS_OF)

    stored = store.execute("SELECT COUNT(*) FROM correlation_matrix").fetchone()[0]
    assert stored == len(cells)


# ── a ranked row carries what it was measured on ──


def test_a_ranked_row_carries_the_state_of_the_cell_behind_it(store, config):
    """Ranked views admit stale cells, and carried nothing to say which were.

    These four tables are what a session reads instead of the raw grid, so a
    stale cell ordered beside a fresh one with no label is the one table where
    SKILL.md's indicative rule was not applied.
    """
    _cell(store, "us", "broad", "distance_from_52w_high", -0.02, status="stale")
    _cell(store, "japan", "broad", "distance_from_52w_high", -0.30)

    rows = Rankings(store, config).build(as_of=AS_OF)
    extended = [row for row in rows if row.dimension == DIMENSION_MOST_EXTENDED]

    assert [row.status for row in extended] == ["stale", "ok"]


def test_the_compressed_view_carries_the_rest_of_the_definition(store, config):
    """SKILL.md's *coiled* is four conditions; the view orders on one.

    The ordering stays on one named indicator so it is reproducible — that is
    the design, not an oversight — but the other conditions travel with the row,
    so "most compressed" cannot be read as "coiled" when only compression was
    measured.
    """
    tenor = config.require_int("data.strike_bounds_rv_tenor_days")
    low = config.require_float("screen.cheap_iv_percentile_max")

    _cell(store, "us", "broad", f"parkinson_vol_{tenor}d_percentile", low / 2)
    _cell(store, "us", "broad", f"realised_vol_{tenor}d_percentile", low / 2)
    _cell(store, "us", "broad", "iv_percentile", low / 2)
    _cell(store, "us", "broad", "distance_from_long_run_high", -0.40)
    _cell(store, "us", "broad", "distance_from_52w_low", low / 2)

    rows = Rankings(store, config).build(as_of=AS_OF)
    compressed = [row for row in rows if row.dimension == DIMENSION_MOST_COMPRESSED]

    assert len(compressed) == 1
    conditions = compressed[0].conditions
    assert conditions["parkinson_vol_compressed"] is True
    assert conditions["realised_vol_compressed"] is True
    assert conditions["iv_percentile_low"] is True
    assert conditions["well_below_long_run_high"] is True
    assert conditions["near_the_52w_low"] is True
    assert compressed[0].meets_full_definition is True


def test_an_unmeasured_condition_is_never_reported_as_a_failed_one(store, config):
    """The grid serves no IV percentile on most cells; that is not "not coiled"."""
    tenor = config.require_int("data.strike_bounds_rv_tenor_days")
    low = config.require_float("screen.cheap_iv_percentile_max")

    _cell(store, "us", "broad", f"parkinson_vol_{tenor}d_percentile", low / 2)
    # No iv_percentile, no range leaves, and no close-to-close percentile either:
    # four conditions unmeasurable, including the other estimator's.

    rows = Rankings(store, config).build(as_of=AS_OF)
    compressed = [row for row in rows if row.dimension == DIMENSION_MOST_COMPRESSED]

    assert compressed[0].conditions["realised_vol_compressed"] is None, (
        "the close-to-close verdict is unmeasured here, which is never the same as False"
    )
    assert compressed[0].conditions["iv_percentile_low"] is None
    assert compressed[0].meets_full_definition is None, (
        "an unmeasured condition makes the verdict unknown, never false"
    )


def test_the_conditions_survive_a_write_and_a_read(store, config):
    tenor = config.require_int("data.strike_bounds_rv_tenor_days")
    low = config.require_float("screen.cheap_iv_percentile_max")
    _cell(store, "us", "broad", f"parkinson_vol_{tenor}d_percentile", low / 2)
    _cell(store, "us", "broad", "iv_percentile", low / 2)

    rankings = Rankings(store, config)
    rankings.write(rankings.build(as_of=AS_OF), as_of=AS_OF)
    stored = rankings.read(as_of=AS_OF, dimension=DIMENSION_MOST_COMPRESSED)

    assert stored[0]["conditions"]["iv_percentile_low"] is True
    assert stored[0]["status"] == "ok"


# ── a view carries the denominator it ranked over ──


def test_a_ranked_row_carries_what_its_view_could_and_did_rank(store, config, rankings):
    """The four views print identically whatever they ordered.

    That is fine for `most_extended`, which ranks every cell with bars, and
    misleading for `widest_iv_hv`, which ranks the cells with a reachable option
    chain — and the only way to tell them apart used to be reading universe.yaml
    and working out which rows set collect_chain.
    """
    _cell(store, "us", "broad", "iv_minus_hv", -0.08, family="iv_hv_spread")
    _cell(store, "japan", "broad", "iv_minus_hv", 0.05, family="iv_hv_spread")
    _cell(
        store,
        "germany",
        "broad",
        "iv_minus_hv",
        None,
        family="iv_hv_spread",
        status="inapplicable",
    )
    _cell(store, "spain", "broad", "iv_minus_hv", None, family="iv_hv_spread", status="n/a")

    rows = [row for row in rankings.build(as_of=AS_OF) if row.dimension == DIMENSION_WIDEST_IV_HV]
    assert rows
    for row in rows:
        # Three cells are reachable; the inapplicable one is not. Two held a
        # value, so the view ranked two of the three it could have.
        assert row.reachable == 3
        assert row.ranked_over == 2
        assert row.is_thin is True


def test_reach_counts_cells_not_rows_across_correlation_windows(store, config, rankings):
    """The shift emits one leaf per window, so a row count would treble it."""
    for window in (30, 90, 250):
        _cell(
            store,
            "japan",
            "broad",
            f"correlation_shift_{window}d",
            1.5,
            family="correlation",
        )

    rows = [
        row
        for row in rankings.build(as_of=AS_OF)
        if row.dimension == DIMENSION_LARGEST_CORRELATION_SHIFT
    ]
    assert rows
    assert rows[0].reachable == 1
    assert rows[0].ranked_over == 1


def test_a_view_below_the_floor_says_so_in_words(store, config, rankings):
    """LABELLED, never suppressed.

    Suppressing it would delete the only implied-vol signal the store holds in
    order to avoid admitting the signal is thin — the opposite of the rule that a
    cell says n/a with its sample count.
    """
    floor = config.require_int("grid.min_cells_for_ranked_view")
    _cell(store, "us", "broad", "iv_minus_hv", -0.08, family="iv_hv_spread")

    rows = [row for row in rankings.build(as_of=AS_OF) if row.dimension == DIMENSION_WIDEST_IV_HV]
    assert len(rows) == 1, "the view was suppressed rather than labelled"
    assert "INSUFFICIENT REACH" in rows[0].detail
    assert str(floor) in rows[0].detail
    assert rows[0].ranked_over < floor


def test_a_view_with_enough_cells_carries_no_warning(store, config, rankings):
    floor = config.require_int("grid.min_cells_for_ranked_view")
    for index in range(floor):
        _cell(
            store,
            f"m{index}",
            "broad",
            "distance_from_52w_high",
            -0.01 * index,
            family="distance_from_high",
        )

    rows = [row for row in rankings.build(as_of=AS_OF) if row.dimension == DIMENSION_MOST_EXTENDED]
    assert rows
    assert all("INSUFFICIENT REACH" not in row.detail for row in rows)
    assert all(row.ranked_over == floor for row in rows)


def test_the_denominator_survives_a_write_and_a_read(store, config, rankings):
    _cell(store, "us", "broad", "iv_minus_hv", -0.08, family="iv_hv_spread")
    rankings.write(rankings.build(as_of=AS_OF), as_of=AS_OF)

    read = rankings.read(as_of=AS_OF, dimension=DIMENSION_WIDEST_IV_HV)
    assert read
    assert read[0]["reachable"] == 1
    assert read[0]["ranked_over"] == 1


# ── a ranked row carries the trajectory of the cell behind it ──


def _trajectory_cell(store, config, market="us", sector="broad"):
    """One cell holding the ranked leaf and every declared trajectory leaf."""
    tenor = config.require_int("data.strike_bounds_rv_tenor_days")
    _cell(
        store,
        market,
        sector,
        f"parkinson_vol_{tenor}d_percentile",
        0.04,
        family="realised_vol",
        window="2y of 30d readings (16 readings)",
    )
    _cell(store, market, sector, "return_21d_local", -0.031, window="21d (22 bars)")
    _cell(store, market, sector, "return_21d_base", -0.048, window="21d (22 bars)")
    _cell(store, market, sector, "return_63d_local", 0.092, window="63d (64 bars)")
    _cell(store, market, sector, "return_63d_base", 0.071, window="63d (64 bars)")
    _cell(
        store,
        market,
        sector,
        "rv_term_slope",
        -0.28,
        family="rv_term_structure",
        window="10d vs 90d",
    )
    _cell(
        store,
        market,
        sector,
        "distance_from_52w_high",
        -0.11,
        family="distance_from_high",
        window="52w (52 bars)",
    )
    _cell(
        store,
        market,
        sector,
        "distance_from_52w_low",
        0.19,
        family="distance_from_low",
        window="52w (52 bars)",
    )
    _cell(
        store,
        market,
        sector,
        "max_drawdown_long_run",
        -0.34,
        family="drawdown",
        window="5y weekly (260 bars)",
    )


def test_a_ranked_row_carries_the_trajectory_of_the_cell_behind_it(store, config):
    """A percentile is a LEVEL. It says nothing about which way that level is moving.

    The trajectory was in the grid all along — same cell, same as_of — and
    reading it meant a get_grid_cell call per ranked row, which is the raw-table
    reading the four views exist to replace.
    """
    _trajectory_cell(store, config)

    rows = Rankings(store, config).build(as_of=AS_OF)
    row = next(r for r in rows if r.dimension == DIMENSION_MOST_COMPRESSED)

    assert row.trajectory["return_21d_base"].value == -0.048
    assert row.trajectory["rv_term_slope"].value == -0.28
    assert row.trajectory["max_drawdown_long_run"].value == -0.34


def test_every_trajectory_leaf_carries_its_own_status_and_window(store, config):
    """A stale return beside a fresh percentile, unlabelled, is what 012 ended.

    A market x sector cell is fed by four stores on four cadences, so the pairing
    is ordinary and correct — and a table that cannot say which is which reads as
    healthier than the store is.
    """
    _trajectory_cell(store, config)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE grid SET status = 'stale', reason = 'inputs stale' "
            "WHERE as_of = ? AND indicator = 'return_63d_local'",
            (AS_OF.isoformat(),),
        )

    rows = Rankings(store, config).build(as_of=AS_OF)
    row = next(r for r in rows if r.dimension == DIMENSION_MOST_COMPRESSED)

    assert row.status == "ok"
    assert row.trajectory["return_63d_local"].status == "stale"
    assert row.trajectory["return_21d_local"].status == "ok"
    for leaf in row.trajectory.values():
        assert leaf.window, "a value with no window is a value nobody can date"
        assert leaf.units


def test_a_leaf_the_store_could_not_measure_is_absent_never_zero(store, config):
    """Zero is a reading. Absence is not, and the two must never print alike."""
    _trajectory_cell(store, config)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE grid SET value = NULL, status = 'n/a', reason = 'no fx series' "
            "WHERE as_of = ? AND indicator = 'return_63d_base'",
            (AS_OF.isoformat(),),
        )

    rows = Rankings(store, config).build(as_of=AS_OF)
    row = next(r for r in rows if r.dimension == DIMENSION_MOST_COMPRESSED)

    assert "return_63d_base" not in row.trajectory
    assert "return_63d_local" in row.trajectory


def test_the_leaf_a_row_ranks_on_is_not_repeated_in_its_trajectory(store, config):
    """One number under two names invites a search for a difference that cannot exist."""
    _trajectory_cell(store, config)

    rows = Rankings(store, config).build(as_of=AS_OF)
    extended = next(r for r in rows if r.dimension == DIMENSION_MOST_EXTENDED)

    assert extended.indicator == "distance_from_52w_high"
    assert "distance_from_52w_high" not in extended.trajectory
    assert extended.trajectory["distance_from_52w_low"].value == 0.19


def test_the_trajectory_carries_the_positioning_level_and_its_change(store, config):
    """Crowding does not time a move; an unwind already underway is the move.

    The level travels with the change because a change alone cannot say whether
    two points of open interest came off a crowded book or a median one.
    """
    _trajectory_cell(store, config)
    _cell(
        store,
        "us",
        "broad",
        "positioning_percentile",
        0.94,
        family="positioning",
        window="5y of COT_WTI",
    )
    windows = [int(w) for w in config.require_list("commodities.cot.change_windows_weeks")]
    for weeks in windows:
        _cell(
            store,
            "us",
            "broad",
            positioning_change_indicator(weeks),
            -0.03,
            family="positioning",
            window=f"{weeks}w of COT_WTI",
        )

    rows = Rankings(store, config).build(as_of=AS_OF)
    row = next(r for r in rows if r.dimension == DIMENSION_MOST_COMPRESSED)

    assert row.trajectory["positioning_percentile"].value == 0.94
    for weeks in windows:
        assert row.trajectory[positioning_change_indicator(weeks)].value == -0.03


def test_every_declared_trajectory_leaf_is_one_some_family_actually_emits(store, config):
    """A misspelt leaf name would be absent from every row for ever, silently.

    The set is declared in one place and the families emit their leaves in
    another, so this is the only thing standing between a typo and a column that
    reads as "the store never measured it".
    """
    inputs = IndicatorInputs.bind(
        store,
        config,
        as_of=AS_OF,
        market="us",
        sector="broad",
        contract_id=None,
        symbol=None,
        market_spec={"currency": "USD", "cot_series": "COT_WTI"},
        benchmark_contract_id=None,
        benchmark_market="us",
    )
    emitted = {cell.indicator for function in INDICATOR_FUNCTIONS for cell in function(inputs)}
    declared = set(Rankings(store, config)._trajectory_leaves)

    assert declared <= emitted, f"declared but never emitted: {sorted(declared - emitted)}"


def test_the_trajectory_set_stays_a_shortlist_rather_than_the_raw_cell(store, config):
    """A row carrying every leaf is the raw table again, which is what the views avoid."""
    inputs = IndicatorInputs.bind(
        store,
        config,
        as_of=AS_OF,
        market="us",
        sector="broad",
        contract_id=None,
        symbol=None,
        market_spec={"currency": "USD", "cot_series": "COT_WTI"},
        benchmark_contract_id=None,
        benchmark_market="us",
    )
    leaves_in_a_cell = len({cell.indicator for f in INDICATOR_FUNCTIONS for cell in f(inputs)})
    declared = len(Rankings(store, config)._trajectory_leaves)

    assert declared * 2 < leaves_in_a_cell, (
        f"{declared} of {leaves_in_a_cell} leaves is no longer a chosen subset"
    )


def test_the_correlation_shift_view_carries_a_trajectory_too(store, config):
    """A decoupling still happens to a market that is moving somewhere."""
    _trajectory_cell(store, config)
    _cell(store, "us", "broad", "correlation_shift_30d", 2.4, family="correlation", window="30d")

    rows = Rankings(store, config).build(as_of=AS_OF)
    row = next(r for r in rows if r.dimension == DIMENSION_LARGEST_CORRELATION_SHIFT)

    assert row.trajectory["return_21d_local"].value == -0.031
    assert row.conditions == {}, "this view names no conditions, and inventing some is a score"


def test_the_trajectory_survives_a_write_and_a_read(store, config):
    """The server reads the STORED rankings, so a field that does not round-trip
    does not exist as far as a session is concerned."""
    _trajectory_cell(store, config)

    rankings = Rankings(store, config)
    rankings.write(rankings.build(as_of=AS_OF), as_of=AS_OF)
    stored = rankings.read(as_of=AS_OF, dimension=DIMENSION_MOST_COMPRESSED)

    leaf = stored[0]["trajectory"]["return_21d_base"]
    assert leaf["value"] == -0.048
    assert leaf["status"] == "ok"
    assert leaf["window"] == "21d (22 bars)"


def test_a_row_whose_cell_holds_no_trajectory_leaf_stores_nothing(store, config):
    """An empty map and an unrecorded one read the same way a NULL always has."""
    tenor = config.require_int("data.strike_bounds_rv_tenor_days")
    _cell(store, "us", "broad", f"parkinson_vol_{tenor}d_percentile", 0.04, family="realised_vol")

    rankings = Rankings(store, config)
    rankings.write(rankings.build(as_of=AS_OF), as_of=AS_OF)

    raw = store.execute(
        "SELECT trajectory FROM grid_rankings WHERE as_of = ? AND dimension = ?",
        (AS_OF.isoformat(), DIMENSION_MOST_COMPRESSED),
    ).fetchone()
    assert raw["trajectory"] is None
    assert rankings.read(as_of=AS_OF, dimension=DIMENSION_MOST_COMPRESSED)[0]["trajectory"] == {}


# ── a ranked view has to say how much of the board it saw ─────────────────────
#
# The four views print identically whatever they ranked, and the absolute floor
# is not what makes one misleading. Measured 2026-09-11 on the built grid:
# `widest_iv_hv` ranked 11 of 11 reachable cells — ABOVE
# grid.min_cells_for_ranked_view, so it printed no warning at all — and every one
# of those eleven was `us`. A 7%, single-market slice of the board presented in
# the same shape as a 37% one is the US lens this project exists to avoid, and no
# count threshold catches it because the count was never the problem.


def test_a_single_market_view_says_so_however_many_cells_it_ranked(rankings):
    detail = rankings._detail("base", reachable=11, ranked_over=11, board=156, markets=["us"] * 11)

    assert "7% of the 156-cell board" in detail
    assert "ONE market (us)" in detail
    # It is ABOVE the count floor, so the old warning must not be what carries it.
    assert "INSUFFICIENT REACH" not in detail


def test_a_broad_view_lists_its_markets_rather_than_warning(rankings):
    detail = rankings._detail(
        "base",
        reachable=57,
        ranked_over=56,
        board=156,
        markets=["us", "japan", "korea", "us"],
    )

    assert "37% of the 156-cell board" in detail
    assert "markets represented: japan, korea, us" in detail
    assert "ONE market" not in detail


def test_the_count_floor_still_fires_underneath_the_disclosure(rankings):
    detail = rankings._detail(
        "base", reachable=3, ranked_over=2, board=156, markets=["us", "japan"]
    )

    assert "INSUFFICIENT REACH" in detail
    assert "2% of the 156-cell board" in detail


# ── the matrix beyond equities ──

#: Two markets, each declaring an equity index, an FX cross and two rate series.
#: `us` rates are daily and its long rate is monthly, which is the real shape:
#: every declared long_rate_series in universe.yaml is a monthly OECD/FRED id.
CROSS_ASSET_MARKETS = {
    "us": {
        "currency": "USD",
        "native_index": "SPX",
        "fx_series": "EURUSD",
        "policy_rate_series": "DFF",
        "long_rate_series": "DGS10M",
    },
    "japan": {
        "currency": "JPY",
        "native_index": "N225",
        "fx_series": "EURJPY",
        "policy_rate_series": None,
        "long_rate_series": None,
    },
}


def _macro(
    store, series_id, *, sessions, value_at, frequency="daily", units="Percent", lag=0, step=1
):
    """A release-dated macro series aligned to the same sessions the bars walk.

    ``step`` writes only every Nth session, for testing that alignment is by date
    rather than by position. ``lag`` is the publication delay: a value is
    invisible to a session earlier than its release date.
    """
    rows = []
    for index in range(0, sessions, step):
        day = AS_OF - timedelta(days=sessions - index)
        release = day + timedelta(days=lag)
        rows.append(
            (
                series_id,
                "fred",
                day.isoformat(),
                release.isoformat(),
                value_at(index),
                frequency,
                units,
            )
        )
    with store.transaction() as connection:
        connection.executemany(
            "INSERT OR REPLACE INTO macro (series_id, source, reference_date, release_date, "
            "value, frequency, units) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def _by_pair(cells, window=30):
    return {
        (cell.left.asset_class, cell.left.series, cell.right.asset_class, cell.right.series): cell
        for cell in cells
        if cell.window_days == window
    }


def test_equity_against_rates_is_computed_and_is_a_level_change(store, config):
    """The regime marker the matrix was missing, and the transform it needs.

    A log return on a rate near zero is not a return, so a rate leg is
    differenced. The shape here is a sine, so the correlation is not pinned — the
    test below does that on a monotone pair. This one pins the plumbing: the pair
    exists, it is measured, and each side carries the transform its asset class
    implies.
    """
    _index(store, 1, "SPX", "us")
    _walk(store, 1, sessions=600, shape=lambda n: 0.01 * math.sin(n / 5.0))
    _macro(store, "DFF", sessions=600, value_at=lambda n: 5.0 - 0.001 * n)

    cells = CorrelationMatrix(store, config, CROSS_ASSET_MARKETS).build(as_of=AS_OF)
    cell = _by_pair(cells)[("equity", "us", "rate", "DFF")]

    assert cell.status == "ok", cell.reason
    assert cell.correlation is not None
    assert cell.n_obs is not None and cell.n_obs > 0
    assert cell.left.market == "us" and cell.right.market == "us"
    assert cell.right.transform == "level_change"
    assert cell.left.transform == "log_return"


def test_equity_against_rates_carries_the_SIGN_of_the_relationship(store, config):
    """Equity against rates flipping sign is the whole reason the matrix reaches
    beyond equities, so the sign is the one number that has to be right.

    Nothing asserted it. The transform's own arithmetic was untested, and a rate
    leg differenced backwards — `previous - current` — inverts every reading here
    while the status stays `ok`, the counts stay plausible and the labels stay
    correct. That mutation passed the entire suite.

    The rate is built so its first difference is exactly the NEGATIVE of the
    index's log return on the same session: every session the index rises, the
    rate is cut by the same amount. A correct pairing is therefore -1 to floating
    point, and nothing about the answer is a matter of degree. A constant rate
    change would NOT do — a series with no dispersion has no correlation at all,
    and the residue left by floating point would be tested instead of the sign.
    """
    sessions = 600
    shape = lambda n: 0.01 * math.sin(n / 5.0)  # noqa: E731
    # The index's log return on session n is shape(n), because _walk compounds
    # exp(shape(n)). Accumulating it and subtracting gives a rate whose own first
    # difference is -shape(n), session for session.
    cumulative = list(accumulate(shape(n) for n in range(sessions)))

    _index(store, 1, "SPX", "us")
    _walk(store, 1, sessions=sessions, shape=shape)
    _macro(store, "DFF", sessions=sessions, value_at=lambda n: 3.0 - cumulative[n])

    cells = CorrelationMatrix(store, config, CROSS_ASSET_MARKETS).build(as_of=AS_OF)
    cell = _by_pair(cells)[("equity", "us", "rate", "DFF")]

    assert cell.status == "ok", cell.reason
    assert cell.correlation == pytest.approx(-1.0, abs=1e-6), (
        "an index rising into a rate being cut is a perfect NEGATIVE correlation; "
        "positive means the rate leg was differenced backwards"
    )


def test_equity_against_rates_reports_a_POSITIVE_correlation_when_it_is_one(store, config):
    """The other half of the sign, so the test above cannot pass by always
    reporting -1. Same construction, rate moving WITH the index instead."""
    sessions = 600
    shape = lambda n: 0.01 * math.sin(n / 5.0)  # noqa: E731
    cumulative = list(accumulate(shape(n) for n in range(sessions)))

    _index(store, 1, "SPX", "us")
    _walk(store, 1, sessions=sessions, shape=shape)
    _macro(store, "DFF", sessions=sessions, value_at=lambda n: 3.0 + cumulative[n])

    cells = CorrelationMatrix(store, config, CROSS_ASSET_MARKETS).build(as_of=AS_OF)
    cell = _by_pair(cells)[("equity", "us", "rate", "DFF")]

    assert cell.status == "ok", cell.reason
    assert cell.correlation == pytest.approx(1.0, abs=1e-6)


def test_a_rate_at_zero_is_still_correlatable(store, config):
    """ECB_DFR has sat at zero. Under log returns this pair would not exist."""
    _index(store, 1, "SPX", "us")
    _walk(store, 1, sessions=600, shape=lambda n: 0.01 * math.sin(n / 5.0))
    _macro(store, "DFF", sessions=600, value_at=lambda n: 0.0 if n % 2 else 0.25)

    cells = CorrelationMatrix(store, config, CROSS_ASSET_MARKETS).build(as_of=AS_OF)
    cell = _by_pair(cells)[("equity", "us", "rate", "DFF")]

    assert cell.status == "ok", cell.reason
    assert cell.correlation is not None


def test_a_daily_series_against_a_monthly_one_is_refused_not_resampled(store, config):
    """Twelve readings a year against two hundred and fifty is not a correlation,
    and any alignment that makes the lengths match has resampled one of them."""
    _index(store, 1, "SPX", "us")
    _walk(store, 1, sessions=600, shape=lambda n: 0.01 * math.sin(n / 5.0))
    _macro(store, "DGS10M", sessions=600, value_at=lambda n: 3.0 + 0.001 * n, frequency="monthly")

    cells = CorrelationMatrix(store, config, CROSS_ASSET_MARKETS).build(as_of=AS_OF)
    cell = _by_pair(cells)[("equity", "us", "rate", "DGS10M")]

    assert cell.status == "n/a"
    assert cell.correlation is None
    assert "monthly" in cell.reason
    assert "refused rather than resampled" in cell.reason


def test_equity_against_fx_is_computed(store, config):
    _index(store, 1, "SPX", "us")
    _walk(store, 1, sessions=600, shape=lambda n: 0.01 * math.sin(n / 5.0))
    _macro(store, "EURUSD", sessions=600, value_at=lambda n: 1.1 + 0.0001 * n, units="EUR")

    cells = CorrelationMatrix(store, config, CROSS_ASSET_MARKETS).build(as_of=AS_OF)
    cell = _by_pair(cells)[("equity", "us", "fx", "EURUSD")]

    assert cell.status == "ok", cell.reason
    # FX is a PRICE, so it is log-returned like the equity leg beside it.
    assert cell.right.transform == "log_return"


def test_an_observation_published_after_the_session_is_not_in_the_correlation(store, config):
    """Point-in-time, and the ONLY filter that enforces it is the release date.

    Every value here is published a year after the period it describes. The older
    half of the series was therefore knowable at `as_of` and the newest year of
    it was not — so the correlation is measured on strictly fewer observations
    than the store holds, and the ones it drops are the ones nobody had.
    """
    _index(store, 1, "SPX", "us")
    _walk(store, 1, sessions=600, shape=lambda n: 0.01 * math.sin(n / 5.0))
    _macro(store, "DFF", sessions=600, value_at=lambda n: 5.0 - 0.001 * n, lag=365)

    lagged = _by_pair(CorrelationMatrix(store, config, CROSS_ASSET_MARKETS).build(as_of=AS_OF))[
        ("equity", "us", "rate", "DFF")
    ]

    # The same series published same-day: now every observation is knowable.
    with store.transaction() as connection:
        connection.execute("UPDATE macro SET release_date = reference_date")
    published = _by_pair(CorrelationMatrix(store, config, CROSS_ASSET_MARKETS).build(as_of=AS_OF))[
        ("equity", "us", "rate", "DFF")
    ]

    assert lagged.n_obs is not None and published.n_obs is not None
    assert lagged.n_obs < published.n_obs, "a year of unpublished prints must not be in it"
    assert published.n_obs - lagged.n_obs >= 300


def test_a_series_published_entirely_after_the_session_is_absent(store, config):
    _index(store, 1, "SPX", "us")
    _walk(store, 1, sessions=600, shape=lambda n: 0.01 * math.sin(n / 5.0))
    _macro(store, "DFF", sessions=600, value_at=lambda n: 5.0 - 0.001 * n, lag=1000)

    cells = CorrelationMatrix(store, config, CROSS_ASSET_MARKETS).build(as_of=AS_OF)
    cell = _by_pair(cells)[("equity", "us", "rate", "DFF")]

    assert cell.status == "n/a"
    assert "no release-dated observations" in cell.reason
    assert "invisible point-in-time" in cell.reason


def test_alignment_is_by_date_and_never_by_position(store, config):
    """Tokyo Monday must not pair with New York Friday.

    The rate here carries only every third session. A positional pairing would
    line its 200 readings up against the equity's first 200 bars and return a
    confident number; a date intersection keeps 200 matched pairs and the count
    says so.
    """
    _index(store, 1, "SPX", "us")
    _walk(store, 1, sessions=600, shape=lambda n: 0.01 * math.sin(n / 5.0))
    _macro(store, "DFF", sessions=600, value_at=lambda n: 5.0 - 0.001 * n, step=3)

    cells = CorrelationMatrix(store, config, CROSS_ASSET_MARKETS).build(as_of=AS_OF)
    cell = _by_pair(cells)[("equity", "us", "rate", "DFF")]
    held = store.execute("SELECT COUNT(*) FROM macro WHERE series_id = 'DFF'").fetchone()[0]
    bars = store.execute("SELECT COUNT(*) FROM bars WHERE contract_id = 1").fetchone()[0]

    assert held * 3 <= bars + 3, "the rate carries a third of the sessions the bars do"
    assert cell.n_obs is not None
    # Changes, so one fewer than the intersection. A POSITIONAL pairing would
    # have lined the rate's 200 readings against the first 200 bars and produced
    # a confident number off dates that never met.
    assert cell.n_obs == held - 1


def test_a_market_declaring_no_fx_or_rate_contributes_no_leg(store, config):
    """`fx_series: null` is a declared coverage gap, not a series to go looking for.

    Every euro-area market declares one: the base currency has no cross against
    itself.
    """
    _index(store, 1, "SPX", "us")
    _index(store, 2, "N225", "japan")
    _walk(store, 1, sessions=600, shape=lambda n: 0.01 * math.sin(n / 5.0))
    _walk(store, 2, sessions=600, shape=lambda n: 0.01 * math.cos(n / 5.0))

    cells = CorrelationMatrix(store, config, CROSS_ASSET_MARKETS).build(as_of=AS_OF)
    series = {(cell.right.asset_class, cell.right.series) for cell in cells}

    assert ("rate", "DFF") in series, "us declares a policy rate"
    assert not any(asset == "rate" and cell_series is None for asset, cell_series in series), (
        "japan declares none, so it contributes no rate leg at all"
    )
    japan_rates = [c for c in cells if c.right.market == "japan" and c.right.asset_class == "rate"]
    assert japan_rates == []


def test_only_the_admitted_class_pairs_are_computed(store, config):
    """FX x FX would be the dollar index measured eleven ways, and every euro-area
    market shares one policy rate, so rate x rate would mostly correlate ECB_DFR
    with itself."""
    _index(store, 1, "SPX", "us")
    _index(store, 2, "N225", "japan")
    _walk(store, 1, sessions=600, shape=lambda n: 0.01 * math.sin(n / 5.0))
    _walk(store, 2, sessions=600, shape=lambda n: 0.01 * math.cos(n / 5.0))
    _macro(store, "EURUSD", sessions=600, value_at=lambda n: 1.1 + 0.0001 * n)
    _macro(store, "EURJPY", sessions=600, value_at=lambda n: 160.0 + 0.01 * n)
    _macro(store, "DFF", sessions=600, value_at=lambda n: 5.0 - 0.001 * n)

    cells = CorrelationMatrix(store, config, CROSS_ASSET_MARKETS).build(as_of=AS_OF)
    combos = {frozenset((cell.left.asset_class, cell.right.asset_class)) for cell in cells}

    assert combos == {
        frozenset({"equity"}),
        frozenset({"equity", "fx"}),
        frozenset({"equity", "rate"}),
    }


def test_a_pair_is_written_once_and_read_back_with_its_classes(store, config):
    _index(store, 1, "SPX", "us")
    _walk(store, 1, sessions=600, shape=lambda n: 0.01 * math.sin(n / 5.0))
    _macro(store, "DFF", sessions=600, value_at=lambda n: 5.0 - 0.001 * n)

    matrix = CorrelationMatrix(store, config, CROSS_ASSET_MARKETS)
    cells = matrix.build(as_of=AS_OF)
    written = matrix.write(cells, as_of=AS_OF)
    # Idempotent: a second build of the same session updates rather than doubling.
    matrix.write(matrix.build(as_of=AS_OF), as_of=AS_OF)

    held = store.execute("SELECT COUNT(*) FROM correlation_matrix").fetchone()[0]
    assert held == written

    row = store.execute(
        "SELECT * FROM correlation_matrix WHERE asset_class_b = 'rate' AND series_a = 'us' "
        "AND window_days = 30"
    ).fetchone()
    assert row["asset_class_a"] == "equity"
    assert row["series_a"] == "us", "an equity leg is identified by its market"
    assert row["series_b"] == "DFF"
    assert row["market_a"] == "us" and row["market_b"] == "us"
    assert row["n_obs"] is not None


def test_the_norm_floor_is_unchanged_and_reports_its_count(store, config):
    """Most cross-asset pairs will sit below it for a long time. That is the
    honest answer, and the count is what makes it checkable in SQL rather than
    only readable in a sentence."""
    _index(store, 1, "SPX", "us")
    _walk(store, 1, sessions=600, shape=lambda n: 0.01 * math.sin(n / 5.0))
    _macro(store, "DFF", sessions=600, value_at=lambda n: 5.0 - 0.001 * n)

    cells = CorrelationMatrix(store, config, CROSS_ASSET_MARKETS).build(as_of=AS_OF)
    floor = config.require_int("grid.correlation_norm_min_independent")
    scored = [c for c in cells if c.z_score is not None]
    unscored = [c for c in cells if c.correlation is not None and c.z_score is None]

    for cell in scored:
        assert cell.norm_independent >= floor
    for cell in unscored:
        assert cell.norm_independent is not None
        assert cell.norm_independent < floor
        assert str(cell.norm_independent) in cell.reason


def test_an_undeclared_frequency_is_refused_rather_than_assumed_daily(store, config):
    """The guard fails CLOSED, and `macro.frequency` really is nullable.

    `macro/rates.py` cached a risk-free rate with a hardcoded cadence until this
    session, so a monthly series could reach the store stamped daily. Unioning an
    empty frequency set into a daily one leaves a single element, which is the
    fail-open version of this check: an undeclared series would be correlated
    against daily bars as though it had agreed to be.
    """
    _index(store, 1, "SPX", "us")
    _walk(store, 1, sessions=600, shape=lambda n: 0.01 * math.sin(n / 5.0))
    _macro(store, "DFF", sessions=600, value_at=lambda n: 5.0 - 0.001 * n)
    with store.transaction() as connection:
        connection.execute("UPDATE macro SET frequency = NULL WHERE series_id = 'DFF'")

    cells = CorrelationMatrix(store, config, CROSS_ASSET_MARKETS).build(as_of=AS_OF)
    cell = _by_pair(cells)[("equity", "us", "rate", "DFF")]

    assert cell.status == "n/a"
    assert "declares no frequency" in cell.reason
    assert "refused rather than assumed" in cell.reason


def test_a_series_whose_own_rows_disagree_about_cadence_is_refused(store, config):
    """Two writers, one series: the rate cache and the full macro pull. A series
    that carries both stamps cannot be correlated at either."""
    _index(store, 1, "SPX", "us")
    _walk(store, 1, sessions=600, shape=lambda n: 0.01 * math.sin(n / 5.0))
    _macro(store, "DFF", sessions=600, value_at=lambda n: 5.0 - 0.001 * n)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE macro SET frequency = 'monthly' WHERE series_id = 'DFF' "
            "AND reference_date < '2025-06-01'"
        )

    cells = CorrelationMatrix(store, config, CROSS_ASSET_MARKETS).build(as_of=AS_OF)
    cell = _by_pair(cells)[("equity", "us", "rate", "DFF")]

    assert cell.status == "n/a"
    assert "more than one frequency" in cell.reason
    assert "daily, monthly" in cell.reason


def test_an_index_with_no_bars_names_the_gap_rather_than_the_overlap(store, config):
    """A contract that resolves but holds no bars is ABSENT, not a zero-length
    series. Reporting '0 overlapping observations' blames the pair for a gap that
    belongs to one side of it."""
    _index(store, 1, "SPX", "us")
    _index(store, 2, "N225", "japan")
    _walk(store, 1, sessions=600, shape=lambda n: 0.01 * math.sin(n / 5.0))
    # japan resolves and has no bars at all.

    cells = CorrelationMatrix(store, config, CROSS_ASSET_MARKETS).build(as_of=AS_OF)
    cell = _by_pair(cells)[("equity", "japan", "equity", "us")]

    assert cell.status == "n/a"
    assert "no bars for its native index" in cell.reason
    assert "overlapping" not in cell.reason


def test_a_series_several_markets_share_becomes_one_leg(store, config):
    """Six euro-area markets declare ECB_DFR as their policy rate. It is ONE
    series and one leg: emitting it per claimant would write the same
    measurement six times under six different market_b values."""
    shared = {
        "germany": {"currency": "EUR", "native_index": "DAX", "policy_rate_series": "ECB_DFR"},
        "france": {"currency": "EUR", "native_index": "CAC", "policy_rate_series": "ECB_DFR"},
        "spain": {"currency": "EUR", "native_index": "IBEX", "policy_rate_series": "ECB_DFR"},
    }
    for contract_id, (market, spec) in enumerate(sorted(shared.items()), start=1):
        _index(store, contract_id, spec["native_index"], market)
        _walk(store, contract_id, sessions=600, shape=lambda n: 0.01 * math.sin(n / 5.0))
    _macro(store, "ECB_DFR", sessions=600, value_at=lambda n: 2.0 - 0.0005 * n)

    cells = CorrelationMatrix(store, config, shared).build(as_of=AS_OF)
    rate_legs = {(c.right.asset_class, c.right.series, c.right.market) for c in cells}
    rate_legs |= {(c.left.asset_class, c.left.series, c.left.market) for c in cells}
    rates = {leg for leg in rate_legs if leg[0] == "rate"}

    assert rates == {("rate", "ECB_DFR", "france")}, "the first market in sorted order owns it"
    # Three equity legs against one rate leg, at every window.
    windows = len(config.require_list("data.correlation_windows_days"))
    equity_rate = [c for c in cells if c.right.asset_class == "rate"]
    assert len(equity_rate) == len(shared) * windows
