"""The grid: fifteen families, native-index measurement, and honest gaps.

Most cells are absent on a young store. That is the point of the tests here: an
absent cell must carry a reason naming the missing input, and the coverage
report must say how many and why, because that count is the deliverable's
honest state rather than a failure to hide.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from obliskiq.collect.universe import BROAD_SECTOR, load_universe_file
from obliskiq.compute.build_grid import (
    KIND_ETF,
    KIND_NATIVE_INDEX,
    KIND_NONE,
    GridBuilder,
    coverage_report,
    family_reach,
)
from obliskiq.compute.indicators import (
    FAMILIES,
    FAMILY_INPUTS,
    IMPLIED_VOL_LEAVES,
    INAPPLICABLE_CODES,
    INPUT_MACRO,
    MACRO_ONLY_FAMILIES,
    POSITIONING_LEVEL,
    RETURN_HORIZONS,
    STATUS_INAPPLICABLE,
    STATUS_NA,
    STATUS_OK,
    STATUS_STALE,
    WEEKS_PER_YEAR,
    Cell,
    _iv_percentile_leaves,
    positioning_change_indicator,
)
from obliskiq.errors import ConfigError

#: The repository root, for the documents this file checks its own numbers against.
REPO_ROOT = Path(__file__).resolve().parent.parent

AS_OF = date(2026, 3, 2)

#: A market x sector pair universe.yaml declares unmapped, with its reason. Named
#: once here because the mapped set changes whenever a probe finds an instrument,
#: and a test hard-coding a cell that later acquires one fails for a reason that
#: has nothing to do with what it tests.
UNMAPPED_CELL = ("germany", "utilities")


def declared_gap_reason(config) -> str:
    return load_universe_file(
        config.require_str("collectors.universe.path"), config
    ).unmapped_cells[UNMAPPED_CELL]


def _index(store, contract_id, symbol, market, *, measures=True):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class, market, "
            "sector, is_native_index, grid_measures, tradable, untradeable_reason) "
            "VALUES (?, ?, 'USD', 'IND', ?, 'broad', 1, ?, 0, 'index level')",
            (contract_id, symbol, market, int(measures)),
        )


def _bars(store, contract_id, *, sessions=400, end=AS_OF, base=100.0, drift=0.1, volume=1_000.0):
    rows = []
    for index in range(sessions):
        day = end - timedelta(days=sessions - index)
        close = base + index * drift
        rows.append(
            (contract_id, day.isoformat(), "1d", close, close + 1, close - 1, close, volume)
        )
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO bars (contract_id, ts, freq, open, high, low, close, volume, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'cp_api')",
            rows,
        )


def _weekly(store, contract_id, *, weeks=260, end=AS_OF, base=100.0, drift=0.5):
    rows = []
    for index in range(weeks):
        day = end - timedelta(weeks=weeks - index)
        close = base + index * drift
        rows.append((contract_id, day.isoformat(), "1w", close, close, close, close, 1_000.0))
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO bars (contract_id, ts, freq, open, high, low, close, volume, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'cp_api')",
            rows,
        )


@pytest.fixture()
def builder(store, config):
    return GridBuilder(store, config)


# ── the axes ──


def test_the_axes_are_declared_not_derived(config, builder):
    """A cell with no instrument must exist as an explicit n/a."""
    from obliskiq.collect.universe import load_universe_file

    universe = load_universe_file(config.require_str("collectors.universe.path"), config)
    assert builder.sectors == universe.sectors
    assert set(builder.markets) == set(universe.markets)
    assert len(builder.sectors) >= 11, "SKILL.md's sector axis"


def test_every_market_declares_a_native_index_or_an_explicit_gap(builder):
    """Declared either way. A market measured on a foreign-currency wrapper would
    report that currency's return under this market's name, so where nothing in the
    right currency serves, universe.yaml says `native_index: null` and the broad
    cell is an explicit gap. An ABSENT key stays an error — a gap nobody wrote down
    is not auditable."""
    for market, spec in builder.markets.items():
        assert "native_index" in spec, f"{market} does not mention native_index at all"


def test_a_universe_with_no_axes_refuses_to_build(store, config, tmp_path):
    import copy

    import yaml

    from obliskiq.config import Config

    stripped = tmp_path / "universe.yaml"
    stripped.write_text(yaml.safe_dump({"instruments": [], "futures_roots": []}))
    data = copy.deepcopy(config.as_dict())
    data["collectors"]["universe"]["path"] = str(stripped)

    with pytest.raises(ConfigError, match="declared rather than"):
        GridBuilder(store, Config(data, config.source))


# ── the fifteen families ──


def test_exactly_fifteen_indicator_families_are_emitted(store, builder):
    """SKILL.md fixes the set so that cells are comparable across markets."""
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    builder.build(as_of=AS_OF)

    families = {
        row[0]
        for row in store.execute(
            "SELECT DISTINCT indicator_family FROM grid WHERE as_of = ?", (AS_OF.isoformat(),)
        ).fetchall()
    }
    assert families == set(FAMILIES)
    assert len(FAMILIES) == 15


def _expected_leaves(config):
    """Every leaf the declarations imply, rebuilt rather than remembered.

    Derived from config and the module constants the emitters actually read, so
    a deliberate change to a tenor list or a window list updates the expectation
    with the code instead of contradicting it. A bare count would be the thing
    this repository deletes on sight.
    """
    data = config.section("data")
    reference_tenor = data.require_int("strike_bounds_rv_tenor_days")
    rv_tenors = [int(t) for t in data.require_list("rv_tenors_days")]
    correlation_windows = [int(w) for w in data.require_list("correlation_windows_days")]
    cot_windows = [int(w) for w in config.require_list("commodities.cot.change_windows_weeks")]

    leaves = {
        # 1 — returns, two currency bases each
        *(
            f"return_{horizon}_{basis}"
            for horizon, _ in (*RETURN_HORIZONS, ("ytd", 0))
            for basis in ("local", "base")
        ),
        # 2, 3, 4 — range position and the worst fall lived through
        f"distance_from_{WEEKS_PER_YEAR}w_high",
        "distance_from_long_run_high",
        f"distance_from_{WEEKS_PER_YEAR}w_low",
        "max_drawdown_long_run",
        # 5 — three estimators, each with its own percentile
        *(
            f"{estimator}_{reference_tenor}d{suffix}"
            for estimator in ("realised_vol", "parkinson_vol", "garman_klass_vol")
            for suffix in ("", "_percentile")
        ),
        # 6 — the realised term structure
        *(f"rv_{tenor}d" for tenor in rv_tenors),
        "rv_term_slope",
        # 7 — level, direction and shape across time
        *IMPLIED_VOL_LEAVES,
        # 8 — whichever leaves data.iv_percentile_source asks for
        *_iv_percentile_leaves(config.require_str("data.iv_percentile_source")),
        # 9
        "iv_minus_hv",
        # 10 — a level and a shift per window
        *(f"correlation_{window}d" for window in correlation_windows),
        *(f"correlation_shift_{window}d" for window in correlation_windows),
        # 11, 12, 13, 14 — the macro row and the flow measure
        "currency_return_12m",
        "policy_rate",
        "policy_rate_real",
        "long_rate_10y",
        "curve_slope",
        "turnover_vs_average",
        # 15 — the crowding level, and the velocity beside it
        POSITIONING_LEVEL,
        *(positioning_change_indicator(weeks) for weeks in cot_windows),
    }
    return leaves


def test_the_leaf_set_is_exactly_what_the_declarations_imply(store, builder, config):
    """The leaf COUNT is quoted in docs/COMPUTE.md and was pinned by nothing.

    The family count is asserted in four places and the tool set in one; the
    leaves — the thing a reader actually joins on — had no guard at all, so a
    config edit silently invalidated the documented figure and the docs drifted
    apart from each other (44 in one paragraph, 47 in another, in one file).

    Asserted as a SET rather than a number, and derived from config rather than
    written down, so it catches a leaf renamed as well as one added or dropped,
    and a deliberate change updates it with the code.
    """
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    builder.build(as_of=AS_OF)

    emitted = {
        row[0]
        for row in store.execute(
            "SELECT DISTINCT indicator FROM grid WHERE as_of = ?", (AS_OF.isoformat(),)
        ).fetchall()
    }
    expected = _expected_leaves(config)

    assert emitted == expected, (
        "grid leaves drifted from the declarations: "
        f"only in the grid {sorted(emitted - expected)}, "
        f"only in the declarations {sorted(expected - emitted)}"
    )
    # Every leaf belongs to one of the fifteen families, so the two counts are
    # readings of one shape and neither can move without the other being checked.
    assert len(emitted) == len(expected)


def test_the_documented_leaf_count_is_the_one_the_grid_emits(store, builder, config):
    """docs/COMPUTE.md quotes the leaf count twice and disagreed with itself.

    One paragraph said 44 "when this was last measured" while the verification
    block forty lines later said 47 — the first written when 44 was right, the
    second updated when the term-structure leaves landed, and nothing able to
    notice. A count in prose that no test reads is a claim with a shelf life.

    This reads the figures back out of the document and checks them against the
    grid actually built above, so the next change to a tenor list or a leaf set
    fails here rather than quietly ageing the docs.
    """
    builder.build(as_of=AS_OF)
    leaves = len(_expected_leaves(config))

    text = (REPO_ROOT / "docs" / "COMPUTE.md").read_text(encoding="utf-8")
    quoted = [int(n) for n in re.findall(r"(\d+)\s+leaves\b", text)]
    quoted += [int(n) for n in re.findall(r"was\s+(\d+)\s+when this was last measured", text)]

    assert quoted, "docs/COMPUTE.md no longer states a leaf count; update this test with it"
    assert set(quoted) == {leaves}, (
        f"docs/COMPUTE.md states leaf counts {sorted(set(quoted))} and the grid emits "
        f"{leaves}. The document is the thing that is wrong."
    )


def test_a_family_emits_several_leaf_cells(store, builder):
    """Returns is one indicator in the mandate and ten numbers in the store."""
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    builder.build(as_of=AS_OF)

    leaves = {
        row[0]
        for row in store.execute(
            "SELECT indicator FROM grid WHERE as_of = ? AND indicator_family = 'returns' "
            "AND market = 'us' AND sector = 'broad'",
            (AS_OF.isoformat(),),
        ).fetchall()
    }
    assert "return_21d_local" in leaves
    assert "return_21d_base" in leaves
    assert "return_ytd_local" in leaves
    assert len(leaves) > len({"returns"})


def test_every_cell_is_written_for_every_market_and_sector(store, builder):
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    report = builder.build(as_of=AS_OF)

    combinations = store.execute(
        "SELECT COUNT(DISTINCT market || '/' || sector) FROM grid WHERE as_of = ?",
        (AS_OF.isoformat(),),
    ).fetchone()[0]
    assert combinations == report.markets * report.sectors


# ── absent cells carry a reason ──


def test_a_cell_with_no_instrument_is_inapplicable_with_its_declared_reason(store, builder, config):
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    builder.build(as_of=AS_OF)

    row = store.execute(
        "SELECT status, reason FROM grid WHERE as_of = ? AND market = ? AND sector = ? LIMIT 1",
        (AS_OF.isoformat(), *UNMAPPED_CELL),
    ).fetchone()
    # INAPPLICABLE, not n/a: no collector will ever fill a cell with no instrument
    # behind it, and counting those against collection health put the coverage
    # floor out of reach — capping coverage near a third of the floor, so every
    # build reported DEGRADED whatever collection did.
    assert row["status"] == STATUS_INAPPLICABLE

    # And the reason is the one a PERSON wrote at the declaration site, verbatim.
    # The builder used to synthesise it, which made every unmapped cell read as
    # though somebody had considered it; only the loader's completeness check
    # makes that true.
    declared = load_universe_file(config.require_str("collectors.universe.path"), config)
    assert row["reason"] == declared.unmapped_cells[UNMAPPED_CELL]


def test_the_reason_for_an_unmapped_cell_is_not_invented_by_the_grid(store, builder, config):
    """Two cells, two declarations, two different reasons reaching the cells.

    A single synthesised sentence would pass a test that only asserted "there is
    a reason", and would say the same eleven words about every gap on the board.
    """
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    builder.build(as_of=AS_OF)

    declared = load_universe_file(config.require_str("collectors.universe.path"), config)
    reasons = {
        (str(row["market"]), str(row["sector"])): row["reason"]
        for row in store.execute(
            "SELECT market, sector, reason FROM grid WHERE as_of = ? AND reason_code = ?",
            (AS_OF.isoformat(), "no_instrument_mapped"),
        )
    }
    assert len(set(reasons.values())) > 1, "every gap is being given the same prose"
    for cell, reason in reasons.items():
        assert reason == declared.unmapped_cells[cell]


def test_no_cell_is_ever_ok_without_a_value(store, builder):
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    builder.build(as_of=AS_OF)

    offenders = store.execute(
        "SELECT COUNT(*) FROM grid WHERE as_of = ? AND status = ? AND value IS NULL",
        (AS_OF.isoformat(), STATUS_OK),
    ).fetchone()[0]
    assert offenders == 0


def test_no_ok_cell_ever_holds_a_non_finite_value(store, builder):
    """The sibling of the test above, and the half it cannot see.

    SQLite stores a Python `nan` as `NULL`, so `value IS NULL` happens to catch
    that half of a non-finite write by accident. It stores `inf` faithfully as
    an ordinary-looking REAL, so the same query reads an infinite cell as a
    populated one. Checked here at the Python level, on every value the grid
    actually wrote, rather than through the one SQL predicate that cannot see
    half the failure.
    """
    import math

    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    builder.build(as_of=AS_OF)

    rows = store.execute(
        "SELECT indicator, value FROM grid WHERE as_of = ? AND status = ?",
        (AS_OF.isoformat(), STATUS_OK),
    ).fetchall()
    offenders = [row["indicator"] for row in rows if not math.isfinite(row["value"])]
    assert offenders == []


def test_a_non_finite_cell_is_demoted_at_the_write_site(store, builder):
    """Proves _write_cells itself does the demotion, independent of whether any
    real indicator can currently be made to produce nan or inf.

    A non-finite value must never reach the store as `status: "ok"` — that is
    the one shape this design refuses everywhere else: an absence presented as
    a measurement, with server/encode.py's non-finite-to-null rule then turning
    it into a JSON `null` a caller cannot tell from a genuine gap.
    """
    for value in (float("nan"), float("inf"), float("-inf")):
        cell = Cell(
            indicator="realised_vol_30d",
            family="5",
            value=value,
            status=STATUS_OK,
            window="30d",
            n_obs=30,
        )
        builder._write_cells([("us", "broad", 1, cell)], as_of=AS_OF)
        row = store.execute(
            "SELECT value, status, reason, reason_code, n_obs FROM grid "
            "WHERE as_of = ? AND market = 'us' AND sector = 'broad' AND indicator = ?",
            (AS_OF.isoformat(), "realised_vol_30d"),
        ).fetchone()
        assert row["status"] == STATUS_NA, value
        assert row["value"] is None, value
        assert row["reason_code"] == "non_finite_value", value
        assert row["reason"] is not None, value
        assert row["n_obs"] == 30, "the sample count survives the demotion"

    # A genuinely finite value is left untouched by the same write path.
    finite = Cell(
        indicator="return_21d", family="4", value=0.05, status=STATUS_OK, window="21d", n_obs=21
    )
    builder._write_cells([("us", "broad", 1, finite)], as_of=AS_OF)
    row = store.execute(
        "SELECT value, status FROM grid WHERE as_of = ? AND market = 'us' AND sector = 'broad' "
        "AND indicator = 'return_21d'",
        (AS_OF.isoformat(),),
    ).fetchone()
    assert row["status"] == STATUS_OK
    assert row["value"] == 0.05


def test_every_non_ok_cell_carries_a_reason(store, builder):
    """Enforced by the schema too, but assert it so a CHECK cannot be relaxed quietly."""
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    builder.build(as_of=AS_OF)

    offenders = store.execute(
        "SELECT COUNT(*) FROM grid WHERE as_of = ? AND status != ? AND "
        "(reason IS NULL OR reason = '')",
        (AS_OF.isoformat(), STATUS_OK),
    ).fetchone()[0]
    assert offenders == 0


def test_a_missing_macro_series_names_the_series(store, builder):
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    builder.build(as_of=AS_OF)

    row = store.execute(
        "SELECT reason FROM grid WHERE as_of = ? AND market = 'us' AND sector = 'broad' "
        "AND indicator = 'policy_rate'",
        (AS_OF.isoformat(),),
    ).fetchone()
    assert "DFF" in row["reason"]
    assert "not a substitute" in row["reason"]


def test_a_short_sample_says_how_short(store, builder):
    """A three-bar history cannot produce a 63-day return under a 63-day label."""
    _index(store, 1, "SPX", "us")
    _bars(store, 1, sessions=3)
    builder.build(as_of=AS_OF)

    rows = dict(
        store.execute(
            "SELECT indicator, reason FROM grid WHERE as_of = ? AND market = 'us' "
            "AND sector = 'broad' AND indicator IN ('return_63d_local', 'return_5d_local') "
            "AND status = ?",
            (AS_OF.isoformat(), STATUS_NA),
        ).fetchall()
    )
    assert "return_63d_local" in rows
    assert "return_5d_local" in rows
    assert "a 63d return needs 64" in rows["return_63d_local"]
    assert "under a longer label" in rows["return_63d_local"]


def test_a_full_window_is_computed_and_a_short_one_is_not(store, builder):
    """The boundary: 22 bars give a 21-day return, 21 do not."""
    _index(store, 1, "SPX", "us")
    _index(store, 2, "SX5E", "eurozone")
    _bars(store, 1, sessions=22)
    _bars(store, 2, sessions=21)
    builder.build(as_of=AS_OF)

    statuses = dict(
        store.execute(
            "SELECT market, status FROM grid WHERE as_of = ? AND sector = 'broad' "
            "AND indicator = 'return_21d_local' AND market IN ('us', 'eurozone')",
            (AS_OF.isoformat(),),
        ).fetchall()
    )
    assert statuses["us"] == STATUS_OK
    assert statuses["eurozone"] == STATUS_NA


# ── values that should be right ──


def test_a_local_return_matches_the_bars(store, builder):
    _index(store, 1, "SPX", "us")
    _bars(store, 1, sessions=400, base=100.0, drift=0.1)
    builder.build(as_of=AS_OF)

    row = store.execute(
        "SELECT value, units FROM grid WHERE as_of = ? AND market = 'us' AND sector = 'broad' "
        "AND indicator = 'return_21d_local'",
        (AS_OF.isoformat(),),
    ).fetchone()
    # 21 sessions of +0.1 on a level that ends at 139.9.
    assert row["value"] == pytest.approx(2.1 / (139.9 - 2.1), rel=1e-6)
    assert row["units"] == "decimal"


def test_the_base_currency_return_equals_the_local_one_for_the_base_market(store, config):
    """No FX leg to apply, and the cell must still be populated rather than n/a."""
    builder = GridBuilder(store, config)
    market = next(m for m, spec in builder.markets.items() if spec.get("fx_series") is None)
    symbol = str(builder.markets[market]["native_index"])
    _index(store, 5, symbol, market)
    _bars(store, 5)
    builder.build(as_of=AS_OF)

    rows = dict(
        store.execute(
            "SELECT indicator, value FROM grid WHERE as_of = ? AND market = ? "
            "AND sector = 'broad' AND indicator IN ('return_21d_local', 'return_21d_base')",
            (AS_OF.isoformat(), market),
        ).fetchall()
    )
    assert rows["return_21d_base"] == pytest.approx(rows["return_21d_local"])


def test_realised_vol_and_its_percentile_are_both_emitted(store, config, builder):
    tenor = config.require_int("data.strike_bounds_rv_tenor_days")
    _index(store, 1, "SPX", "us")
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO bars (contract_id, ts, freq, close, volume, source) "
            "VALUES (1, ?, '1d', ?, 1000, 'cp_api')",
            [
                ((AS_OF - timedelta(days=400 - n)).isoformat(), 100.0 * (1.01 if n % 2 else 0.995))
                for n in range(400)
            ],
        )
    builder.build(as_of=AS_OF)

    rows = dict(
        store.execute(
            "SELECT indicator, value FROM grid WHERE as_of = ? AND market = 'us' "
            "AND sector = 'broad' AND indicator_family = 'realised_vol'",
            (AS_OF.isoformat(),),
        ).fetchall()
    )
    assert rows[f"realised_vol_{tenor}d"] > 0
    assert 0.0 <= rows[f"realised_vol_{tenor}d_percentile"] <= 1.0


# ── the range-based estimators: an ADDITIONAL leaf, never a swap ──


def _range_cells(store, market="us"):
    return {
        str(row["indicator"]): row
        for row in store.execute(
            "SELECT indicator, value, status, reason, reason_code, n_obs, window FROM grid "
            "WHERE as_of = ? AND market = ? AND sector = 'broad' "
            "AND indicator_family = 'realised_vol'",
            (AS_OF.isoformat(), market),
        ).fetchall()
    }


def test_all_three_estimators_are_emitted_and_named_by_leaf(store, config, builder):
    """The store must never leave two estimators indistinguishable.

    Provenance is the LEAF NAME rather than a column, which is why this needs no
    migration: a Parkinson cell and a close-to-close cell cannot be confused,
    and there is no separate provenance field that could drift out of step with
    the value beside it.
    """
    tenor = config.require_int("data.strike_bounds_rv_tenor_days")
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    builder.build(as_of=AS_OF)

    cells = _range_cells(store)
    for name in (
        f"realised_vol_{tenor}d",
        f"parkinson_vol_{tenor}d",
        f"garman_klass_vol_{tenor}d",
    ):
        assert cells[name]["status"] == "ok", cells[name]["reason"]
        assert cells[name]["value"] > 0
        assert cells[name]["n_obs"] == tenor
        assert cells[f"{name}_percentile"]["status"] == "ok"
        assert 0.0 <= cells[f"{name}_percentile"]["value"] <= 1.0

    # Different estimators of a related thing, not one number three times. On a
    # drifting series with a fixed daily range they disagree by construction.
    assert cells[f"parkinson_vol_{tenor}d"]["value"] != pytest.approx(
        cells[f"realised_vol_{tenor}d"]["value"]
    )


def test_a_series_with_no_high_or_low_keeps_close_to_close_and_refuses_the_rest(
    store, config, builder
):
    """The refusal is per estimator, and it names which input is missing.

    A cell that lost its close-to-close reading as well would be this change
    taking something away, which it must not: the range leaves are additional.
    """
    tenor = config.require_int("data.strike_bounds_rv_tenor_days")
    _index(store, 1, "SPX", "us")
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO bars (contract_id, ts, freq, close, volume, source) "
            "VALUES (1, ?, '1d', ?, 1000, 'cp_api')",
            [
                ((AS_OF - timedelta(days=400 - n)).isoformat(), 100.0 * (1.01 if n % 2 else 0.995))
                for n in range(400)
            ],
        )
    builder.build(as_of=AS_OF)

    cells = _range_cells(store)
    assert cells[f"realised_vol_{tenor}d"]["status"] == "ok"
    for name in (f"parkinson_vol_{tenor}d", f"garman_klass_vol_{tenor}d"):
        for leaf in (name, f"{name}_percentile"):
            assert cells[leaf]["status"] == "n/a"
            assert cells[leaf]["reason_code"] == "no_high_low_range"
            # The reason names the count and the prices each estimator needed,
            # so a coverage report says what to collect rather than only that
            # something is missing.
            assert "only 0 daily bar(s)" in cells[leaf]["reason"]
            assert "below the 30 a 30-session range estimate needs" in cells[leaf]["reason"]


def test_a_restated_open_refuses_garman_klass_and_leaves_parkinson_alone(store, config, builder):
    """Parkinson never reads the open, so a fake one is not its problem.

    Refusing both would throw away a reading the data fully supports, and
    computing Garman-Klass anyway would put a different formula's output in a
    cell claiming to be one. The reason carries the measured share so a reader
    can see how the verdict was reached rather than taking it on trust.
    """
    tenor = config.require_int("data.strike_bounds_rv_tenor_days")
    _index(store, 1, "SPX", "us")
    sessions, drift, base = 400, 0.1, 100.0
    rows = []
    for index in range(sessions):
        close = base + index * drift
        # The open IS the previous close, and the range still brackets the bar:
        # well-formed rows that only restated_open_share can see through.
        opening = base + (index - 1) * drift if index else close
        rows.append(
            (
                1,
                (AS_OF - timedelta(days=sessions - index)).isoformat(),
                "1d",
                opening,
                close + 1,
                close - 1,
                close,
                1_000.0,
            )
        )
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO bars (contract_id, ts, freq, open, high, low, close, volume, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'cp_api')",
            rows,
        )
    builder.build(as_of=AS_OF)

    cells = _range_cells(store)
    assert cells[f"realised_vol_{tenor}d"]["status"] == "ok"
    assert cells[f"parkinson_vol_{tenor}d"]["status"] == "ok"
    for leaf in (f"garman_klass_vol_{tenor}d", f"garman_klass_vol_{tenor}d_percentile"):
        assert cells[leaf]["status"] == "n/a"
        assert cells[leaf]["reason_code"] == "open_restates_previous_close"
        assert "100%" in cells[leaf]["reason"]


def test_the_range_leaves_are_collectible_absences_not_structural_ones(store, config, builder):
    """`n/a`, never `inapplicable`, and the difference is a coverage number.

    `bars` has the columns; a route that filled them would fill these cells. An
    `inapplicable` here would take the gap out of the collection-health
    denominator and hide it in the ambition gap, which is the exact failure the
    fourth status was introduced to end.
    """
    tenor = config.require_int("data.strike_bounds_rv_tenor_days")
    _index(store, 1, "SPX", "us")
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO bars (contract_id, ts, freq, close, volume, source) "
            "VALUES (1, ?, '1d', ?, 1000, 'cp_api')",
            [((AS_OF - timedelta(days=400 - n)).isoformat(), 100.0 + n) for n in range(400)],
        )
    builder.build(as_of=AS_OF)

    assert _range_cells(store)[f"parkinson_vol_{tenor}d"]["status"] == "n/a"
    assert "no_high_low_range" not in INAPPLICABLE_CODES


# ── staleness ──


def test_the_daily_bar_freshness_window_has_one_value_in_two_keys(config):
    """`data.grid_staleness_days` and `grid.freshness_days.daily_bars` must agree.

    The builder reads the per-input window; every staleness test below reads
    `data.grid_staleness_days` as its proxy. Config says outright that the two
    are the same number and that splitting freshness by input "re-tunes nothing
    on its own". If they ever diverged, the builder would use one, the tests
    would assert the other, and every case here would pass while measuring
    something that is not the window in force.
    """
    assert config.require_int("data.grid_staleness_days") == config.require_int(
        "grid.freshness_days.daily_bars"
    )


def test_a_stale_instrument_downgrades_its_cells(store, config, builder):
    stale_days = config.require_int("data.grid_staleness_days")
    _index(store, 1, "SPX", "us")
    _bars(store, 1, end=AS_OF - timedelta(days=stale_days + 10))
    builder.build(as_of=AS_OF)

    row = store.execute(
        "SELECT status, reason FROM grid WHERE as_of = ? AND market = 'us' "
        "AND sector = 'broad' AND indicator = 'return_21d_local'",
        (AS_OF.isoformat(),),
    ).fetchone()
    assert row["status"] == STATUS_STALE
    assert "indicative, not live" in row["reason"]


def test_fresh_bars_are_not_stale(store, builder):
    _index(store, 1, "SPX", "us")
    _bars(store, 1, end=AS_OF)
    builder.build(as_of=AS_OF)
    row = store.execute(
        "SELECT status FROM grid WHERE as_of = ? AND market = 'us' AND sector = 'broad' "
        "AND indicator = 'return_21d_local'",
        (AS_OF.isoformat(),),
    ).fetchone()
    assert row["status"] == STATUS_OK


# ── coverage ──


def test_a_native_index_cell_is_recorded_as_such(store, builder):
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    builder.build(as_of=AS_OF)

    row = store.execute(
        "SELECT instrument_kind, is_native_index, symbol FROM grid_coverage "
        "WHERE as_of = ? AND market = 'us' AND sector = 'broad'",
        (AS_OF.isoformat(),),
    ).fetchone()
    assert row["instrument_kind"] == KIND_NATIVE_INDEX
    assert row["is_native_index"] == 1
    assert row["symbol"] == "SPX"


def test_an_unmapped_cell_is_recorded_as_a_coverage_gap(store, builder, config):
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    builder.build(as_of=AS_OF)

    row = store.execute(
        "SELECT instrument_kind, coverage_note FROM grid_coverage WHERE as_of = ? "
        "AND market = ? AND sector = ?",
        (AS_OF.isoformat(), *UNMAPPED_CELL),
    ).fetchone()
    assert row["instrument_kind"] == KIND_NONE
    assert row["coverage_note"] == declared_gap_reason(config)


def test_a_concentrated_etf_is_labelled_a_two_stock_analysis(store, config, builder):
    """The exact claim that once produced a wrong reading of China from ADRs."""
    flag = config.require_float("data.etf_top2_concentration_flag")
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class, market, "
            "sector, grid_measures, hedged, top2_concentration, tradable, "
            "untradeable_reason) VALUES (7, 'QQQ', 'USD', 'ETF', 'us', 'technology', 1, 0, "
            "?, 0, 'no PRIIPs KID')",
            (flag + 0.10,),
        )
    _bars(store, 7)
    builder.build(as_of=AS_OF)

    row = store.execute(
        "SELECT instrument_kind, coverage_note FROM grid_coverage WHERE as_of = ? "
        "AND market = 'us' AND sector = 'technology'",
        (AS_OF.isoformat(),),
    ).fetchone()
    assert row["instrument_kind"] == KIND_ETF
    assert "TWO-STOCK ANALYSIS" in row["coverage_note"]


def test_an_etf_with_no_sourced_concentration_says_the_test_could_not_run(store, builder):
    """An invented figure would defeat the very warning it feeds."""
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class, market, "
            "sector, grid_measures, hedged, tradable, untradeable_reason) VALUES "
            "(8, 'XLE', 'USD', 'ETF', 'us', 'energy', 1, 0, 0, 'no PRIIPs KID')"
        )
    _bars(store, 8)
    builder.build(as_of=AS_OF)

    row = store.execute(
        "SELECT coverage_note FROM grid_coverage WHERE as_of = ? AND market = 'us' "
        "AND sector = 'energy'",
        (AS_OF.isoformat(),),
    ).fetchone()
    assert "NOT SOURCED" in row["coverage_note"]


def test_the_coverage_report_counts_populated_and_absent_with_reasons(store, builder):
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    _weekly(store, 1)
    report = builder.build(as_of=AS_OF)

    assert (
        report.cells_written
        == report.populated + report.absent + report.stale + report.inapplicable
    )
    assert report.collectible == report.cells_written - report.inapplicable
    assert report.populated > 0
    assert report.absent > 0
    assert report.reasons
    assert "coverage" in report.summary()


def test_coverage_is_populated_over_collectible_not_over_every_cell(store, builder, config):
    """The bug this split exists to fix.

    One instrument measures one cell of the board here. Against every leaf
    written the ratio is a fraction of a percent and no collector could ever move
    it; against what is actually collectible it is a number that responds to
    collection.

    The DECLARED gaps are the leaves that must not be in the denominator — a
    reason a person wrote saying nothing will ever measure this cell. The
    mapped-but-unseeded cells are NOT in that class and stay in it: the universe
    collector owes them, which is why the improvement over the naive ratio is a
    factor of a few here rather than the order of magnitude it is on a seeded
    store.
    """
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    _weekly(store, 1)
    report = builder.build(as_of=AS_OF)

    declared = load_universe_file(config.require_str("collectors.universe.path"), config)
    blanketed = store.execute(
        "SELECT COUNT(*) FROM grid WHERE as_of = ? AND reason_code = 'no_instrument_mapped'",
        (AS_OF.isoformat(),),
    ).fetchone()[0]
    assert blanketed > 0
    assert len(declared.unmapped_cells) > 0

    assert report.collectible == report.cells_written - report.inapplicable
    assert report.cells_written - report.collectible >= blanketed, (
        "leaves of a declared gap are still counted as something a collector owes"
    )
    naive = report.populated / report.cells_written
    assert report.coverage_pct > naive * 2, (
        "coverage is still being measured against cells nothing can fill"
    )
    assert report.coverage_pct == report.populated / report.collectible
    assert report.ambition_pct == report.collectible / report.cells_written


def test_the_ambition_gap_is_reported_rather_than_hidden(store, builder):
    """The gap must stay visible; it just must not read as a collection failure."""
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    report = builder.build(as_of=AS_OF)

    assert report.ambition_pct < 0.5, "one instrument cannot make the grid collectible"
    summary = report.summary()
    assert "ambition gap" in summary
    assert "inapplicable" in summary


def test_the_coverage_report_can_be_rederived_from_the_store(store, builder):
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    built = builder.build(as_of=AS_OF)
    read_back = coverage_report(store, as_of=AS_OF)

    assert read_back.cells_written == built.cells_written
    assert read_back.populated == built.populated
    assert read_back.absent == built.absent
    assert set(read_back.families) == set(built.families)


def test_rebuilding_updates_rather_than_duplicating(store, builder):
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    first = builder.build(as_of=AS_OF)
    builder.build(as_of=AS_OF)
    total = store.execute(
        "SELECT COUNT(*) FROM grid WHERE as_of = ?", (AS_OF.isoformat(),)
    ).fetchone()[0]
    assert total == first.cells_written


def test_a_dry_run_writes_nothing(store, builder):
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    report = builder.build(as_of=AS_OF, write=False)
    assert report.cells_written > 0
    assert store.execute("SELECT COUNT(*) FROM grid").fetchone()[0] == 0


# ── the coverage histogram has to be readable ──


def test_missing_cells_are_grouped_by_a_code_not_by_prose(store, builder):
    """One line per market x sector saying the same thing is a report nobody reads."""
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    report = builder.build(as_of=AS_OF)

    assert report.inapplicable > 100, "the fixture should leave plenty of cells unmapped"
    assert len(report.reasons) < 20, (
        f"the histogram has {len(report.reasons)} buckets; it is grouping on prose, not on a code"
    )
    assert "no_instrument_mapped" in report.reasons


def test_every_absent_cell_carries_a_code(store, builder):
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    builder.build(as_of=AS_OF)

    uncoded = store.execute(
        "SELECT COUNT(*) FROM grid WHERE as_of = ? AND status != ? AND reason_code IS NULL",
        (AS_OF.isoformat(), STATUS_OK),
    ).fetchone()[0]
    assert uncoded == 0


def test_the_histogram_carries_a_worked_example_per_code(store, builder):
    """A count with no example is a number nobody can act on."""
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    report = builder.build(as_of=AS_OF)

    assert set(report.examples) == set(report.reasons)
    summary = report.summary()
    assert "e.g." in summary
    assert summary.count("e.g.") == len(report.reasons)


def test_a_stale_cell_is_coded_as_stale(store, config, builder):
    stale_days = config.require_int("data.grid_staleness_days")
    _index(store, 1, "SPX", "us")
    _bars(store, 1, end=AS_OF - timedelta(days=stale_days + 10))
    builder.build(as_of=AS_OF)

    codes = {
        row[0]
        for row in store.execute(
            "SELECT DISTINCT reason_code FROM grid WHERE as_of = ? AND status = ?",
            (AS_OF.isoformat(), STATUS_STALE),
        ).fetchall()
    }
    assert codes == {"inputs_stale"}


def test_no_cell_is_ever_unclassified(store, builder):
    """An absent cell with no code is a bug in whichever family produced it."""
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    report = builder.build(as_of=AS_OF)
    assert "unclassified" not in report.reasons


# ── freshness is per family ──
#
# One reading of the newest daily BAR used to be stamped on all thirty-seven
# leaves of a market x sector. Both directions of that were wrong and neither
# was visible: a policy-rate cell, which reads a published macro series and no
# bar at all, went stale because an equity bar was old; and a months-old vendor
# IV percentile read `ok` because the bars beside it were fresh.


def _macro(store, series_id, rows, *, source="fred", frequency="daily", units="percent"):
    """A macro series with real release dates, so `known_at` will serve it."""
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO macro (series_id, source, reference_date, release_date, value, "
            "frequency, units) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(series_id, source, day, day, value, frequency, units) for day, value in rows],
        )


def _status(store, indicator, *, as_of=AS_OF, market="us", sector="broad"):
    row = store.execute(
        "SELECT status, reason, market_data_status FROM grid WHERE as_of = ? AND market = ? "
        "AND sector = ? AND indicator = ?",
        (as_of.isoformat(), market, sector, indicator),
    ).fetchone()
    return None if row is None else dict(row)


def test_an_old_bar_does_not_make_a_macro_cell_stale(store, config, builder):
    """The policy rate reads `macro` and no bar at all. Its freshness is a fact
    about when the rate was published, not about when an equity last traded."""
    stale_days = config.require_int("data.grid_staleness_days")
    _index(store, 1, "SPX", "us")
    _bars(store, 1, end=AS_OF - timedelta(days=stale_days + 10))
    _macro(store, "DFF", [((AS_OF - timedelta(days=1)).isoformat(), 4.25)])

    builder.build(as_of=AS_OF)

    assert _status(store, "return_21d_local")["status"] == STATUS_STALE, (
        "the bar-fed family should still be stale — the bars really are old"
    )
    policy = _status(store, "policy_rate")
    assert policy["status"] == STATUS_OK, (
        "the policy rate went stale because of an equity bar it does not read"
    )


def test_an_old_macro_series_does_not_make_a_bar_cell_stale(store, config, builder):
    """And the converse, which is the direction that hides a problem rather than
    inventing one."""
    macro_days = config.require_int("grid.freshness_days.macro")
    _index(store, 1, "SPX", "us")
    _bars(store, 1, end=AS_OF)
    _macro(store, "DFF", [((AS_OF - timedelta(days=macro_days + 30)).isoformat(), 4.25)])

    builder.build(as_of=AS_OF)

    assert _status(store, "return_21d_local")["status"] == STATUS_OK
    policy = _status(store, "policy_rate")
    assert policy["status"] == STATUS_STALE
    assert "macro" in policy["reason"]


def test_a_stale_cell_names_the_input_that_aged_out(store, config, builder):
    """A verdict a reader cannot trace is a verdict they cannot act on: which
    store to go and refill is the whole actionable content of `stale`."""
    stale_days = config.require_int("data.grid_staleness_days")
    _index(store, 1, "SPX", "us")
    _bars(store, 1, end=AS_OF - timedelta(days=stale_days + 10))

    builder.build(as_of=AS_OF)

    reason = _status(store, "return_21d_local")["reason"]
    assert "daily_bars" in reason
    assert "grid.freshness_days.daily_bars" in reason
    assert "a cell beside it may be current" in reason


def test_the_freshness_read_cannot_see_bars_recorded_after_the_session(store, builder):
    """Freshness was measured on an UNBOUNDED bar read, so a grid rebuilt for a
    past date compared against bars recorded after it: the age went negative,
    the comparison passed, and the cell was stamped `ok` on data that did not
    exist on the day it claims. The same class as the unbounded snapshot count,
    in the function that decides staleness.
    """
    past = AS_OF - timedelta(days=120)
    _index(store, 1, "SPX", "us")
    # History up to the past session, then a long gap, then bars right up to today.
    _bars(store, 1, end=past)
    _bars(store, 1, sessions=30, end=AS_OF, base=500.0)

    builder.build(as_of=past)

    assert _status(store, "return_21d_local", as_of=past)["status"] == STATUS_OK, (
        "as of the rebuilt session the bars were current, so this must not be stale"
    )
    # And a rebuild for a session the bars do NOT reach is stale, which is what
    # proves the bound is doing work rather than the gap being invisible.
    gap_day = past + timedelta(days=60)
    builder.build(as_of=gap_day)
    assert _status(store, "return_21d_local", as_of=gap_day)["status"] == STATUS_STALE


def test_a_grid_cell_carries_the_market_data_state_behind_it(store, builder):
    """SKILL.md requires anything FROZEN or DELAYED to be labelled indicative in
    every table it appears in. The grid is a table it appears in, and until now
    the column recording it was written by the collectors and read by nothing."""
    _index(store, 1, "SPX", "us")
    _bars(store, 1, end=AS_OF)
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO underlying_snapshots (snapshot_date, contract_id, implied_vol, "
            "iv_percentile_52w, source, market_data_status) "
            "VALUES (?, 1, 0.28, 0.42, 'cp_api', 'FROZEN')",
            (AS_OF.isoformat(),),
        )

    builder.build(as_of=AS_OF)

    percentile = _status(store, "iv_percentile_vendor")
    assert percentile["status"] == STATUS_OK
    assert percentile["market_data_status"] == "FROZEN"
    # A macro cell reads a published series and has no quote state at all;
    # labelling one FROZEN would invent a fact.
    assert _status(store, "policy_rate")["market_data_status"] is None


def test_a_months_old_vendor_iv_percentile_is_stale_not_ok(store, config, builder):
    """`_vendor_iv_percentile` took limit=1 with no age bound at all, so a
    reading from an abandoned collector run was published as `ok` indefinitely.
    An IV percentile is the most time-sensitive figure in the grid."""
    window = config.require_int("grid.freshness_days.underlying_snapshot")
    _index(store, 1, "SPX", "us")
    _bars(store, 1, end=AS_OF)
    old_day = AS_OF - timedelta(days=window + 90)
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO underlying_snapshots (snapshot_date, contract_id, implied_vol, "
            "iv_percentile_52w, source) VALUES (?, 1, 0.28, 0.42, 'cp_api')",
            (old_day.isoformat(),),
        )

    builder.build(as_of=AS_OF)

    cell = _status(store, "iv_percentile_vendor")
    assert cell["status"] == STATUS_NA
    assert old_day.isoformat() in cell["reason"], "the reason must date the reading it refused"


def test_a_fresh_vendor_iv_percentile_still_dates_itself(store, builder):
    """Fresh or not, the reason carries the date. A percentile that does not say
    when it was measured cannot be argued with."""
    _index(store, 1, "SPX", "us")
    _bars(store, 1, end=AS_OF)
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO underlying_snapshots (snapshot_date, contract_id, implied_vol, "
            "iv_percentile_52w, source) VALUES (?, 1, 0.28, 0.42, 'cp_api')",
            (AS_OF.isoformat(),),
        )

    builder.build(as_of=AS_OF)
    cell = _status(store, "iv_percentile_vendor")
    assert cell["status"] == STATUS_OK


def test_every_family_declares_the_inputs_it_reads(config):
    """A family added without an entry in FAMILY_INPUTS would be silently exempt
    from freshness — it would read `ok` forever, on any input, with nothing
    saying so."""
    from obliskiq.compute.indicators import FAMILY_INPUTS, INPUT_SOURCES

    assert set(FAMILY_INPUTS) == set(FAMILIES), (
        f"FAMILY_INPUTS and FAMILIES have drifted: {sorted(set(FAMILIES) ^ set(FAMILY_INPUTS))}"
    )
    for family, groups in FAMILY_INPUTS.items():
        assert groups, f"{family} declares no inputs at all"
        for group in groups:
            assert group, f"{family} declares an empty requirement group"
            for source in group:
                assert source in INPUT_SOURCES, f"{family} reads unknown source {source!r}"


def test_every_input_source_has_a_configured_freshness_window(config):
    """A source with no window would never go stale, which is the failure this
    whole split exists to end."""
    from obliskiq.compute.indicators import INPUT_SOURCES

    windows = config.get("grid.freshness_days")
    assert set(windows) == set(INPUT_SOURCES), (
        f"grid.freshness_days and INPUT_SOURCES have drifted: "
        f"{sorted(set(windows) ^ set(INPUT_SOURCES))}"
    )


def test_a_bar_older_than_any_lookback_is_stale_rather_than_absent(store, config, builder):
    """Freshness used to be read through a windowed bar query, so an instrument
    whose newest bar fell outside that lookback returned nothing and read as "no
    bars at all" — absence, not staleness. The stalest instruments were the ones
    reported `ok`."""
    stale_days = config.require_int("data.grid_staleness_days")
    _index(store, 1, "SPX", "us")
    # Far beyond any plausible lookback multiple of the freshness window.
    _bars(store, 1, end=AS_OF - timedelta(days=stale_days * 40))

    builder.build(as_of=AS_OF)

    cell = _status(store, "return_21d_local")
    assert cell["status"] == STATUS_STALE, (
        "a bar this old was invisible to the windowed freshness read and the cell reported ok"
    )


# ── indicators 7, 8 and 9: unreachable is not the same as uncollected ──


def _measuring_etf(store, contract_id, symbol, market, sector, **columns):
    fields = {
        "tradable": 0,
        "untradeable_reason": "measurement instrument only",
        "grid_measures": 1,
        "collect_chain": 0,
        "options_unavailable_reason": None,
        "hedged": 0,
    }
    fields.update(columns)
    names = ", ".join(fields)
    holes = ", ".join("?" for _ in fields)
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class, market, "
            f"sector, {names}) VALUES (?, ?, 'KRW', 'ETF', ?, ?, {holes})",
            (contract_id, symbol, market, sector, *fields.values()),
        )


def _option_cells(store, market, sector):
    return {
        row["indicator"]: row
        for row in store.execute(
            "SELECT indicator, status, reason, reason_code FROM grid WHERE as_of = ? "
            "AND market = ? AND sector = ? AND indicator_family IN "
            "('implied_vol', 'iv_percentile', 'iv_hv_spread')",
            (AS_OF.isoformat(), market, sector),
        ).fetchall()
    }


def test_an_unreachable_chain_makes_indicators_7_8_and_9_inapplicable(store, builder):
    """The case ROADMAP names: Korea has no reachable chain, so those cells are
    NOT APPLICABLE rather than missing, and must not count against collection."""
    _measuring_etf(
        store,
        20,
        "091170",
        "korea",
        "financials",
        options_unavailable_reason="no chain is reachable on KRX",
    )
    _bars(store, 20)
    builder.build(as_of=AS_OF)

    cells = _option_cells(store, "korea", "financials")
    assert cells, "the option families should still emit their leaves"
    for indicator, row in cells.items():
        assert row["status"] == STATUS_INAPPLICABLE, indicator
        assert row["reason_code"] == "options_not_available", indicator
        assert "no chain is reachable on KRX" in row["reason"], indicator


def test_a_collected_chain_that_has_not_arrived_stays_a_collection_gap(store, builder):
    """The other half of the split, and the one the floor is supposed to police.

    This row collects a chain and none is in the store yet. Something IS supposed
    to fill these cells, so they stay `n/a` and keep counting against coverage.
    """
    _measuring_etf(
        store, 21, "QQQ", "us", "technology", collect_chain=1, options_unavailable_reason=None
    )
    _bars(store, 21)
    builder.build(as_of=AS_OF)

    cells = _option_cells(store, "us", "technology")
    assert cells
    assert all(row["status"] == STATUS_NA for row in cells.values())
    assert not any(row["reason_code"] == "options_not_available" for row in cells.values())


def test_an_inapplicable_option_cell_writes_the_same_leaves_a_populated_one_would(
    store, builder, config
):
    """A grid whose leaf names change with its status cannot be joined across days."""
    _measuring_etf(
        store, 22, "091170", "korea", "financials", options_unavailable_reason="none reachable"
    )
    _measuring_etf(
        store, 23, "QQQ", "us", "technology", collect_chain=1, options_unavailable_reason=None
    )
    _bars(store, 22)
    _bars(store, 23)
    builder.build(as_of=AS_OF)

    assert set(_option_cells(store, "korea", "financials")) == set(
        _option_cells(store, "us", "technology")
    )


def test_a_market_that_declares_no_macro_series_is_inapplicable_not_absent(store, builder):
    """hong_kong nulls its rate series on purpose; no collector can close that."""
    _index(store, 1, "HSI", "hong_kong")
    _bars(store, 1)
    builder.build(as_of=AS_OF)

    rows = store.execute(
        "SELECT status, reason_code FROM grid WHERE as_of = ? AND market = 'hong_kong' "
        "AND sector = 'broad' AND indicator IN ('policy_rate', 'long_rate_10y')",
        (AS_OF.isoformat(),),
    ).fetchall()
    assert rows
    for row in rows:
        assert row["status"] == STATUS_INAPPLICABLE
        assert row["reason_code"] == "macro_series_undeclared"


def test_a_declared_macro_series_that_is_absent_is_still_a_collection_gap(store, builder):
    """us declares DFF and the store does not hold it: that is a collector's job."""
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    builder.build(as_of=AS_OF)

    row = store.execute(
        "SELECT status, reason_code, reason FROM grid WHERE as_of = ? AND market = 'us' "
        "AND sector = 'broad' AND indicator = 'policy_rate'",
        (AS_OF.isoformat(),),
    ).fetchone()
    assert row["status"] == STATUS_NA
    assert row["reason_code"] == "macro_series_absent"
    assert "DFF" in row["reason"]


def test_only_structural_codes_can_be_marked_inapplicable():
    """The guard that stops a collection gap being excused out of the metric."""
    with pytest.raises(ValueError, match="not a structural reason code"):
        Cell.inapplicable("x", "returns", "because", code="sample_too_short")


# ── two absences that no collector could ever close ──


def test_the_benchmark_cell_is_inapplicable_against_itself_not_a_collection_gap(store, builder):
    """A cell that IS the benchmark can never correlate to itself informatively.

    Both that and "the benchmark has no bars" shared one reason code, so the
    benchmark's own six correlation leaves counted against collection coverage
    every day, for a reason nothing could fix.
    """
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    builder.build(as_of=AS_OF)

    rows = store.execute(
        "SELECT status, reason_code FROM grid WHERE as_of = ? AND market = 'us' "
        "AND sector = 'broad' AND indicator LIKE 'correlation_%'",
        (AS_OF.isoformat(),),
    ).fetchall()
    assert rows
    for row in rows:
        assert row["status"] == STATUS_INAPPLICABLE
        assert row["reason_code"] == "cell_is_the_benchmark"


def test_a_benchmark_with_no_bars_is_still_a_collection_gap(store, builder):
    """The other half of the same branch, and it IS something a collector owes."""
    _index(store, 2, "N225", "japan")
    _bars(store, 2)
    builder.build(as_of=AS_OF)

    rows = store.execute(
        "SELECT status, reason_code FROM grid WHERE as_of = ? AND market = 'japan' "
        "AND sector = 'broad' AND indicator LIKE 'correlation_%'",
        (AS_OF.isoformat(),),
    ).fetchall()
    assert rows
    for row in rows:
        assert row["status"] == STATUS_NA
        assert row["reason_code"] == "no_benchmark_series"


def test_an_index_level_has_no_turnover_and_that_is_not_a_gap(store, builder):
    """An index level is a computed number; there is nothing to turn over.

    Every native index measures a market cell, so this counted several of the
    grid's own measuring instruments as permanently behind on collection.
    """
    _index(store, 1, "SPX", "us")
    _bars(store, 1, volume=0.0)
    builder.build(as_of=AS_OF)

    row = store.execute(
        "SELECT status, reason_code, reason FROM grid WHERE as_of = ? AND market = 'us' "
        "AND sector = 'broad' AND indicator = 'turnover_vs_average'",
        (AS_OF.isoformat(),),
    ).fetchone()
    assert row is not None
    assert row["status"] == STATUS_INAPPLICABLE
    assert row["reason_code"] == "index_carries_no_volume"
    assert "index level" in row["reason"]


# ── the macro blanket, and its one exception ──


#: A market this store holds no instrument row for, which is what makes its macro
#: cells the thing under test. The fixture seeds SPX and nothing else, so every
#: market but ``us`` is in that position.
#:
#: It used to name south_africa, the one market universe.yaml declared with
#: ``native_index: null``. Phase 8 removed that market — nothing on this route
#: measures it — and no market declares a null native index now, so the DECLARED
#: half of the exception is pinned by
#: ``test_a_declared_gap_still_computes_macro_on_its_broad_cell`` over a universe
#: written for the purpose instead. Both halves stay covered: build_grid exempts
#: the macro families on the broad cell either way.
UNMEASURED_MARKET = "brazil"


#: What the blanket leaves behind. `inapplicable` is a declared gap and `n/a` a
#: cell whose instrument is mapped and unseeded; neither is a computed reading.
BLANKET_STATUSES = frozenset({STATUS_INAPPLICABLE, STATUS_NA})


def _macro_leaves(store, market, sector):
    return {
        str(row["indicator"]): str(row["status"])
        for row in store.execute(
            "SELECT indicator, status FROM grid WHERE as_of = ? AND market = ? "
            "AND sector = ? AND indicator_family IN "
            "('currency_return', 'policy_rate', 'yield_curve', 'positioning')",
            (AS_OF.isoformat(), market, sector),
        )
    }


def test_a_market_with_no_instrument_still_computes_macro_on_its_broad_cell(store, builder):
    """Indicators 11-13 and 15 read the market spec and touch no instrument data.

    They were being blanketed anyway, so a market with no equity fund lost its
    entire macro row for want of one — which is a claim about the wrong thing.

    This is the UNSEEDED half: universe.yaml maps an instrument here and the
    store holds no row for it. A policy rate does not become unreadable because
    the equity fund beside it failed to seed.
    """
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    builder.build(as_of=AS_OF)

    leaves = _macro_leaves(store, UNMEASURED_MARKET, "broad")
    assert leaves, "the broad cell emitted no macro leaves at all"
    assert any(status != STATUS_INAPPLICABLE for status in leaves.values()), (
        "every macro leaf is still blanketed on a market with no instrument"
    )


def test_the_blanket_still_covers_every_other_sector_of_that_market(store, builder):
    """Deliberate, and the reason is duplication rather than tidiness.

    Per-market macro is ONE fact. Letting it through on all twelve sectors would
    put twelve copies of a single reading into the coverage numerator and make a
    market with no instrument look better collected than one with eleven.

    What is asserted is that the macro leaf is NOT COMPUTED there, not which
    blanket covers it: a sector the market declares as a gap blankets to
    `inapplicable` and one whose instrument merely has not seeded blankets to
    `n/a`, and both are the blanket doing its job. Pinning one status made this
    test a statement about which sectors the market happens to map an instrument
    to, which is not what it is for.
    """
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    builder.build(as_of=AS_OF)

    for sector in (s for s in builder.sectors if s != BROAD_SECTOR):
        leaves = _macro_leaves(store, UNMEASURED_MARKET, sector)
        assert leaves
        computed = {name for name, status in leaves.items() if status not in BLANKET_STATUSES}
        assert not computed, (
            f"{UNMEASURED_MARKET}/{sector} is duplicating the market's macro: {sorted(computed)}"
        )


def test_no_leaf_of_an_unmeasured_broad_cell_is_ever_ok_without_a_value(store, builder):
    """The exception must not manufacture a reading out of an absent series."""
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    builder.build(as_of=AS_OF)

    offenders = store.execute(
        "SELECT COUNT(*) FROM grid WHERE as_of = ? AND market = ? AND sector = 'broad' "
        "AND status = ? AND value IS NULL",
        (AS_OF.isoformat(), UNMEASURED_MARKET, STATUS_OK),
    ).fetchone()[0]
    assert offenders == 0


def test_a_declared_gap_still_computes_macro_on_its_broad_cell(store, config, tmp_path):
    """The DECLARED half of the same exception, which universe.yaml no longer has.

    ``native_index: null`` is a gap somebody wrote down: nothing in the market's
    own currency measures its broad cell, so the cell is an explicit
    ``no_instrument_mapped`` rather than a quietly-filled wrapper. Until Phase 8
    south_africa was that market and this behaviour rode on it; the market was
    removed because nothing can measure it, and the branch is pinned here so it
    cannot rot unnoticed.
    """
    import copy
    from pathlib import Path

    import yaml

    from obliskiq.config import Config

    raw = yaml.safe_load(Path(config.require_str("collectors.universe.path")).read_text())
    gapped = UNMEASURED_MARKET
    raw["markets"][gapped]["native_index"] = None
    # With no instrument left, the market's existing `remaining` block claims
    # every sector including broad, which is exactly the shape under test.
    raw["instruments"] = [row for row in raw["instruments"] if row.get("market") != gapped]
    universe_path = tmp_path / "universe.yaml"
    universe_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    data = copy.deepcopy(config.as_dict())
    data["collectors"]["universe"]["path"] = str(universe_path)
    gapped_builder = GridBuilder(store, Config(data, config.source))

    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    gapped_builder.build(as_of=AS_OF)

    broad = _macro_leaves(store, gapped, "broad")
    assert broad, "the broad cell of a declared gap emitted no macro leaves at all"
    assert any(status != STATUS_INAPPLICABLE for status in broad.values()), (
        "a declared gap is still losing its whole macro row for want of an instrument"
    )
    assert set(_macro_leaves(store, gapped, "utilities").values()) == {STATUS_INAPPLICABLE}, (
        "the blanket must still cover the other eleven sectors, or one per-market "
        "fact lands in the numerator twelve times"
    )


def test_the_macro_only_set_is_derived_from_what_each_family_reads():
    """Hand-listing it would go on asserting the property after it stopped holding."""
    assert MACRO_ONLY_FAMILIES
    for family in MACRO_ONLY_FAMILIES:
        groups = FAMILY_INPUTS[family]
        assert groups
        assert all(group == (INPUT_MACRO,) for group in groups)
    for family, groups in FAMILY_INPUTS.items():
        if any(source != INPUT_MACRO for group in groups for source in group):
            assert family not in MACRO_ONLY_FAMILIES


# ── how far each family reaches ──


def test_family_reach_counts_cells_not_leaves(store, builder):
    """A family emits up to ten leaves per cell; a leaf count would rank the
    wordy families as the far-reaching ones."""
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    builder.build(as_of=AS_OF)

    reach = {item.family: item for item in family_reach(store, as_of=AS_OF)}
    assert set(reach) == set(FAMILIES)
    # Derived, not restated. universe.yaml owns both axes, and a literal here
    # went stale the moment the market axis changed.
    cells = len(builder.markets) * len(builder.sectors)
    for item in reach.values():
        assert item.cells_total == cells
        assert item.cells_reachable <= item.cells_total
        assert item.cells_populated <= item.cells_reachable
        assert item.blocked_cells <= item.cells_total


def _seed_declared_instruments(store, config) -> int:
    """Every measuring instrument universe.yaml declares, as a universe row.

    Reach is a property of the DECLARATIONS, and the builder reads them through
    the store. On a store holding one of the fifty-seven, the other fifty-six
    cells report `instrument_not_seeded` — collectible, because the universe
    collector owes them — and an option family then counts them as reachable,
    which is true of that store and not of this machine. Seeding is what makes
    the question the intended one.
    """
    declared = load_universe_file(config.require_str("collectors.universe.path"), config)
    measuring = [row for row in declared.instruments if row.grid_measures]
    with store.transaction() as connection:
        connection.executemany(
            "INSERT OR REPLACE INTO universe (contract_id, symbol, currency, asset_class, "
            "market, sector, is_native_index, grid_measures, tradable, untradeable_reason, "
            "collect_chain, options_unavailable_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, 'measurement instrument', ?, ?)",
            [
                (
                    index + 100,
                    row.symbol,
                    row.currency,
                    row.asset_class,
                    row.market,
                    row.sector,
                    int(row.is_native_index),
                    int(row.collect_chain),
                    row.options_unavailable_reason,
                )
                for index, row in enumerate(measuring)
            ],
        )
    return len(measuring)


def test_the_option_families_reach_only_the_cells_that_collect_a_chain(store, builder, config):
    """The shortfall this session was called to make visible.

    Indicators 7, 8 and 9 need a chain, and a chain is reachable only where
    universe.yaml sets collect_chain. That was two cells in the whole grid and
    the ranked view built on it printed like the ones that rank a hundred.
    """
    measured = _seed_declared_instruments(store, config)
    builder.build(as_of=AS_OF)

    reach = {item.family: item for item in family_reach(store, as_of=AS_OF)}
    option_families = ("implied_vol", "iv_percentile", "iv_hv_spread")
    reaches = {reach[family].cells_reachable for family in option_families}
    assert len(reaches) == 1, "the three option families disagree about what they can see"
    option_reach = reaches.pop()

    assert reach["returns"].cells_reachable == measured
    assert 0 < option_reach < measured, (
        "an option family cannot reach every cell a family that only needs bars does"
    )
    # What keeps them out of MOST of the grid is having no instrument at all —
    # the same wall every family hits. What keeps them out of the rest is a
    # DECLARED options_unavailable_reason, not an inference from a falsy
    # collect_chain, so it is a claim somebody can be held to.
    blocked = {
        str(row["market"]) + "/" + str(row["sector"])
        for row in store.execute(
            "SELECT DISTINCT market, sector FROM grid WHERE as_of = ? AND reason_code = ?",
            (AS_OF.isoformat(), "options_not_available"),
        )
    }
    assert len(blocked) == measured - option_reach
    for family in option_families:
        assert reach[family].dominant_blocker is not None
        assert reach[family].describe().startswith(family)


def test_reach_is_ordered_narrowest_first(store, builder):
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    builder.build(as_of=AS_OF)

    reach = family_reach(store, as_of=AS_OF)
    assert reach == sorted(reach, key=lambda item: (item.cells_reachable, item.family))


def test_macro_comes_through_on_broad_even_when_the_instrument_merely_failed_to_seed(
    store, builder
):
    """A policy rate does not become unreadable because an equity fund is missing.

    The two ways a cell can have no instrument in the store — declared gap, and
    declared instrument that never seeded — are different STATUSES, but neither
    of them is a fact about the macro series the market declares.
    """
    _index(store, 1, "SPX", "us")
    _bars(store, 1)
    builder.build(as_of=AS_OF)

    # japan is mapped in universe.yaml and unseeded in this store, so its cells
    # carry instrument_not_seeded rather than a declared gap.
    codes = {
        str(row["reason_code"])
        for row in store.execute(
            "SELECT DISTINCT reason_code FROM grid WHERE as_of = ? AND market = 'japan' "
            "AND sector = 'broad'",
            (AS_OF.isoformat(),),
        )
    }
    assert "instrument_not_seeded" in codes
    assert "no_instrument_mapped" not in codes

    # Every macro leaf names a MACRO input as the thing that is missing, not the
    # instrument. `japan` declares no cot_series, so positioning is structurally
    # inapplicable; the other four report the series they could not read.
    macro_codes = {
        str(row["indicator"]): str(row["reason_code"])
        for row in store.execute(
            "SELECT indicator, reason_code FROM grid WHERE as_of = ? AND market = 'japan' "
            "AND sector = 'broad' AND indicator_family IN "
            "('currency_return', 'policy_rate', 'yield_curve', 'positioning')",
            (AS_OF.isoformat(),),
        )
    }
    assert macro_codes
    assert "instrument_not_seeded" not in macro_codes.values(), (
        "the macro row is being blamed on a missing equity fund"
    )
    assert macro_codes["policy_rate"] == "macro_series_absent"
    assert macro_codes["currency_return_12m"] == "fx_series_absent"
    assert macro_codes["positioning_percentile"] == "no_futures_contract"


# ── reading one cell across sessions ──────────────────────────────────────────
#
# `grid` is keyed on (as_of, market, sector, indicator) and every build upserts
# its own session, so the table has always been a time series. Nothing could read
# it as one: every reader took a single `as_of`, so a consumer asked to tell a
# rising reading from a falling one could see only the window statistics computed
# INSIDE one snapshot.


def _grid_row(store, as_of, indicator, value, *, status="ok", reason=None, window="30d"):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO grid (as_of, market, sector, indicator, indicator_family, value, "
            "status, reason, window, units) VALUES (?,'us','broad',?,'realised_vol',?,?,?,?, "
            "'decimal')",
            (as_of, indicator, value, status, reason, window),
        )


def test_a_cell_reads_back_across_sessions_newest_first(store):
    from obliskiq.compute.build_grid import read_cell_history

    for day, value in (("2026-09-09", 0.10), ("2026-09-10", 0.14), ("2026-09-11", 0.19)):
        _grid_row(store, day, "realised_vol_30d", value)

    rows = read_cell_history(store, market="us", sector="broad", indicator="realised_vol_30d")

    assert [str(r.as_of) for r in rows] == ["2026-09-11", "2026-09-10", "2026-09-09"]
    assert [r.value for r in rows] == [0.19, 0.14, 0.10]


def test_a_session_the_cell_was_not_measured_is_a_gap_not_a_skip(store):
    """Silently closing the gap would make the series look continuous when it is
    not, which is the one thing a trajectory reader must never be shown."""
    from obliskiq.compute.build_grid import read_cell_history

    _grid_row(store, "2026-09-09", "realised_vol_30d", 0.10)
    _grid_row(store, "2026-09-10", "realised_vol_30d", None, status="n/a", reason="no bars")
    _grid_row(store, "2026-09-11", "realised_vol_30d", 0.19)

    rows = read_cell_history(store, market="us", sector="broad", indicator="realised_vol_30d")

    assert len(rows) == 3
    middle = rows[1]
    assert middle.value is None and middle.status == "n/a" and middle.reason == "no bars"


def test_history_is_bounded_and_honours_an_as_of(store):
    from obliskiq.compute.build_grid import read_cell_history

    for day in ("2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"):
        _grid_row(store, day, "realised_vol_30d", 0.1)

    assert (
        len(
            read_cell_history(
                store, market="us", sector="broad", indicator="realised_vol_30d", limit=2
            )
        )
        == 2
    )
    bounded = read_cell_history(
        store, market="us", sector="broad", indicator="realised_vol_30d", until=date(2026, 9, 9)
    )
    assert [str(r.as_of) for r in bounded] == ["2026-09-09", "2026-09-08"]


def test_a_window_that_changed_between_builds_is_visible_on_the_rows(store):
    """The grid accumulates history, it does not re-measure it — so two rows under
    one indicator name can be incomparable, and only `window` says so."""
    from obliskiq.compute.build_grid import read_cell_history

    _grid_row(store, "2026-09-10", "realised_vol_30d", 0.10, window="30d (8 readings)")
    _grid_row(store, "2026-09-11", "realised_vol_30d", 0.19, window="30d (16 readings)")

    rows = read_cell_history(store, market="us", sector="broad", indicator="realised_vol_30d")

    assert rows[0].window != rows[1].window
