"""The weekly census: how many contracts clear EVERY gate at once.

The count that matters is the intersection, not any single gate. If it is
routinely below the floor, the census reports it — a gate quietly loosened to
manufacture candidates is how a discipline becomes decoration.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from obliskiq.compute.gates import (
    GATE_IV_PERCENTILE,
    GATE_OPEN_INTEREST,
    GATE_PRICE_CEILING,
    GATE_TENOR,
)
from obliskiq.compute.penalty import PenaltyLedger
from obliskiq.compute.universe_census import (
    VERDICT_NO_DATA,
    VERDICT_THIN,
    VERDICT_VIABLE,
    UniverseCensus,
)

AS_OF = date(2026, 3, 2)
NAV = 400_000.0


@pytest.fixture()
def census(store, config):
    return UniverseCensus(store, config)


def _underlying(store, contract_id=1, symbol="ASML"):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class, market, "
            "sector, collect_chain) VALUES (?, ?, 'EUR', 'STK', 'eurozone', 'technology', 1)",
            (contract_id, symbol),
        )


def _chain(store, rows, *, snapshot_date=AS_OF, underlying_id=1):
    """rows: (dte, strike, bid, ask, open_interest, volume)."""
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO chain_snapshots (snapshot_date, underlying_id, expiry, strike, "
            "right, bid, ask, open_interest, volume, source) "
            "VALUES (?, ?, ?, ?, 'C', ?, ?, ?, ?, 'cp_api')",
            [
                (
                    snapshot_date.isoformat(),
                    underlying_id,
                    (snapshot_date + timedelta(days=dte)).isoformat(),
                    strike,
                    bid,
                    ask,
                    open_interest,
                    volume,
                )
                for dte, strike, bid, ask, open_interest, volume in rows
            ],
        )


def _iv_percentile(store, value, *, contract_id=1, snapshot_date=AS_OF):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO underlying_snapshots (snapshot_date, contract_id, last, "
            "implied_vol, iv_percentile_52w, source) VALUES (?, ?, 100.0, 0.28, ?, 'cp_api')",
            (snapshot_date.isoformat(), contract_id, value),
        )


#: A contract that clears everything: inside the tenor window, deep book, tight
#: spread, traded today, affordable, and implied vol in the bottom quartile.
GOOD = (120, 100.0, 2.00, 2.10, 5_000, 40)


def test_a_contract_clearing_every_gate_is_counted(store, census):
    _underlying(store)
    _chain(store, [GOOD])
    _iv_percentile(store, 0.10)

    result = census.run(as_of=AS_OF, nav=NAV)

    assert result.contracts_scanned == 1
    assert result.passed_all_gates == 1
    assert result.survivors


def test_the_intersection_is_smaller_than_any_single_gate(store, census):
    """Each gate looks survivable alone; the intersection is what can be traded."""
    _underlying(store)
    _chain(
        store,
        [
            GOOD,
            (70, 105.0, 2.00, 2.10, 5_000, 40),  # inside every gate but the tenor
            (120, 110.0, 2.00, 2.10, 10, 40),  # inside every gate but open interest
            # Open interest above the floor but below the depth waiver, so the
            # spread gate actually runs on it.
            (120, 115.0, 2.00, 9.00, 1_000, 40),  # inside every gate but the spread
        ],
    )
    _iv_percentile(store, 0.10)

    result = census.run(as_of=AS_OF, nav=NAV)

    assert result.contracts_scanned == 4
    assert result.passed_all_gates == 1
    assert result.per_gate[GATE_TENOR] == 3
    assert result.per_gate[GATE_OPEN_INTEREST] == 3
    single_gate_counts = (result.per_gate[GATE_TENOR], result.per_gate[GATE_OPEN_INTEREST])
    assert result.passed_all_gates < min(single_gate_counts)


def test_a_thin_universe_is_reported_and_never_relaxed(store, config, census):
    floor = config.require_int("census.min_viable_contracts")
    _underlying(store)
    _chain(store, [GOOD])
    _iv_percentile(store, 0.10)

    result = census.run(as_of=AS_OF, nav=NAV)

    assert result.passed_all_gates < floor
    assert result.verdict == VERDICT_THIN
    assert not result.is_viable
    assert str(floor) in result.reason
    assert "not fixed by relaxing a threshold" in result.reason
    assert "BELOW THE FLOOR" in result.report()


def test_enough_survivors_reads_as_viable(store, config, census):
    floor = config.require_int("census.min_viable_contracts")
    _underlying(store)
    _chain(
        store,
        [(120, 100.0 + n, 2.00, 2.10, 5_000, 40) for n in range(floor)],
    )
    _iv_percentile(store, 0.10)

    result = census.run(as_of=AS_OF, nav=NAV)

    assert result.passed_all_gates == floor
    assert result.verdict == VERDICT_VIABLE
    assert result.is_viable


def test_an_unaffordable_contract_fails_the_price_ceiling(store, census):
    """The budget defines the universe, and it is applied before screening."""
    _underlying(store)
    _chain(store, [(120, 100.0, 400.00, 401.00, 5_000, 40)])
    _iv_percentile(store, 0.10)

    result = census.run(as_of=AS_OF, nav=10_000.0)

    assert result.passed_all_gates == 0
    assert result.per_gate[GATE_PRICE_CEILING] == 0


def test_an_expensive_implied_vol_fails_the_cheap_gate(store, census):
    _underlying(store)
    _chain(store, [GOOD])
    _iv_percentile(store, 0.90)

    result = census.run(as_of=AS_OF, nav=NAV)

    assert result.passed_all_gates == 0
    assert result.per_gate[GATE_IV_PERCENTILE] == 0


def test_an_untested_gate_is_not_a_passed_gate(store, census):
    """No vendor IV percentile means unmeasured, and the report says how many."""
    _underlying(store)
    _chain(store, [GOOD])
    # No underlying_snapshots row at all: the percentile was never served.

    result = census.run(as_of=AS_OF, nav=NAV)

    assert result.untested[GATE_IV_PERCENTILE] == 1
    assert result.per_gate[GATE_IV_PERCENTILE] == 0
    assert "untested" in result.report()


def test_a_futures_underlying_never_survives_the_census(store, census):
    """The futures ban has to hold in this code path too."""
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class, market, "
            "sector, tradable, untradeable_reason) VALUES "
            "(9, 'CL', 'USD', 'FUT', 'us', 'energy', 0, 'futures contract prohibited')"
        )
    _chain(store, [GOOD], underlying_id=9)
    _iv_percentile(store, 0.10, contract_id=9)

    result = census.run(as_of=AS_OF, nav=NAV)

    assert result.contracts_scanned == 1
    assert result.passed_all_gates == 0


def test_no_chain_snapshots_reports_no_data_rather_than_zero_survivors(census):
    result = census.run(as_of=AS_OF, nav=NAV)
    assert result.verdict == VERDICT_NO_DATA
    assert "no chain snapshots" in result.reason
    assert "could not run" in result.report()


def test_no_nav_reports_no_data_and_names_the_failure_mode(store, census):
    _underlying(store)
    _chain(store, [GOOD])
    result = census.run(as_of=AS_OF)
    assert result.verdict == VERDICT_NO_DATA
    assert "budget defines the universe" in result.reason


def test_the_nav_comes_from_nav_history_when_not_supplied(store, config, census):
    from obliskiq.compute.risk_engine import RiskEngine

    _underlying(store)
    _chain(store, [GOOD])
    _iv_percentile(store, 0.10)
    RiskEngine(store, config).record_nav(as_of=AS_OF, nav=NAV)

    result = census.run(as_of=AS_OF)

    assert result.nav == NAV
    assert result.verdict != VERDICT_NO_DATA


def test_the_penalty_balance_shrinks_the_affordability_ceiling(store, config, census):
    _underlying(store)
    _chain(store, [GOOD])
    _iv_percentile(store, 0.10)

    before = census.run(as_of=AS_OF, nav=NAV).available_budget
    PenaltyLedger(store, config).book_loss_detected(expected_loss=NAV)
    after = census.run(as_of=AS_OF, nav=NAV).available_budget

    assert after < before
    assert after == 0.0


def test_the_census_round_trips_to_the_store(store, census):
    _underlying(store)
    _chain(store, [GOOD])
    _iv_percentile(store, 0.10)

    result = census.run(as_of=AS_OF, nav=NAV)
    census.write(result)

    row = store.execute(
        "SELECT * FROM universe_census WHERE as_of = ?", (AS_OF.isoformat(),)
    ).fetchone()
    assert row["contracts_scanned"] == 1
    assert row["passed_all_gates"] == 1
    assert row["min_required"] == result.min_required
    assert row["verdict"] == result.verdict
    assert "survivors" in row["detail"]


def test_rerunning_a_census_updates_rather_than_duplicating(store, census):
    _underlying(store)
    _chain(store, [GOOD])
    _iv_percentile(store, 0.10)
    census.write(census.run(as_of=AS_OF, nav=NAV))
    census.write(census.run(as_of=AS_OF, nav=NAV))
    assert store.execute("SELECT COUNT(*) FROM universe_census").fetchone()[0] == 1


def test_a_routinely_thin_universe_is_detectable_across_runs(store, census):
    """Three weekly runs, each reading the chain that existed on its own day.

    A snapshot is seeded per week rather than once: the census bounds its chain
    read by ``as_of``, so a run dated last week cannot see a snapshot taken
    today. Seeding only the newest would make the two older runs report
    ``no_data`` — correctly, but that is not what this test is about.
    """
    _underlying(store)
    _iv_percentile(store, 0.10)
    for week in range(3):
        _chain(store, [GOOD], snapshot_date=AS_OF - timedelta(weeks=week))
    for week in range(3):
        result = census.run(as_of=AS_OF - timedelta(weeks=week), nav=NAV)
        census.write(result)

    assert census.is_routinely_thin(limit=3)


def test_the_census_frequency_comes_from_config(config, census):
    assert census.frequency == config.require_str("data.universe_census_frequency")
    assert census.frequency == "weekly"


def test_a_contract_with_an_unmeasured_gate_does_not_clear_every_gate(store, census):
    """ "Clears every gate" cannot include a gate that was never asked.

    Reporting 98 of 98 clearing every gate while the IV percentile went untested
    on all 98 is precisely the overstatement the untested count exists to stop.
    """
    _underlying(store)
    _chain(store, [GOOD])
    # No underlying_snapshots row: the vendor IV percentile was never served.

    result = census.run(as_of=AS_OF, nav=NAV)

    assert result.passed_all_gates == 0
    assert result.passed_with_untested == 1
    assert result.untested[GATE_IV_PERCENTILE] == 1
    assert "An untested gate is not a passed gate" in result.report()


def test_a_depth_waived_spread_still_counts_as_fully_tested(store, config, census):
    """The waiver is a rule, not a missing input, so the gate was applied."""
    waiver = config.require_int("liquidity.spread_waiver_min_open_interest")
    _underlying(store)
    # A spread far beyond the ceiling, on a book deep enough to waive the gate.
    _chain(store, [(120, 100.0, 2.00, 9.00, waiver, 40)])
    _iv_percentile(store, 0.10)

    result = census.run(as_of=AS_OF, nav=NAV)

    assert result.passed_all_gates == 1
    assert result.passed_with_untested == 0


def test_the_verdict_reads_the_strict_count(store, config, census):
    floor = config.require_int("census.min_viable_contracts")
    _underlying(store)
    _chain(store, [(120, 100.0 + n, 2.00, 2.10, 5_000, 40) for n in range(floor)])
    # Again no IV percentile: every contract is short one measured gate.

    result = census.run(as_of=AS_OF, nav=NAV)

    assert result.passed_with_untested == floor
    assert result.verdict == VERDICT_THIN, (
        "a universe whose gates were never measured is not a viable universe"
    )


# ── the census reports how far each family reaches ──


def _grid_leaf(store, market, sector, indicator, family, status, *, reason_code=None):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO grid (as_of, market, sector, indicator, value, status, reason, "
            "indicator_family, reason_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                AS_OF.isoformat(),
                market,
                sector,
                indicator,
                1.0 if status == "ok" else None,
                status,
                None if status == "ok" else "seeded",
                family,
                reason_code,
            ),
        )


def test_the_census_reports_what_each_family_can_reach(store, census):
    """The shortfall belongs in a report, not in whoever thinks to read the code.

    A family reaching a handful of cells was discoverable only by reading
    universe.yaml and working out which rows collect a chain. The census is the
    report that answers "what can this machine see at all".
    """
    _grid_leaf(store, "us", "broad", "iv_minus_hv", "iv_hv_spread", "ok")
    _grid_leaf(
        store,
        "japan",
        "broad",
        "iv_minus_hv",
        "iv_hv_spread",
        "inapplicable",
        reason_code="options_not_available",
    )
    _grid_leaf(store, "us", "broad", "return_5d_local", "returns", "ok")
    _grid_leaf(store, "japan", "broad", "return_5d_local", "returns", "ok")

    result = census.run(as_of=AS_OF)
    reach = {item.family: item for item in result.grid_reach}
    assert reach["iv_hv_spread"].cells_reachable == 1
    assert reach["iv_hv_spread"].cells_total == 2
    assert reach["iv_hv_spread"].dominant_blocker == "options_not_available"
    assert reach["returns"].cells_reachable == 2

    report = result.report()
    assert "what each indicator family can reach" in report
    assert "iv_hv_spread: reaches 1/2 cells" in report


def test_reach_is_reported_even_when_the_census_itself_cannot_run(store, census):
    """Reach is a property of the DECLARATIONS, not of a chain snapshot.

    Counting contracts needs a NAV and a collected chain. Saying which fifteenth
    of the grid is a coin flip needs neither, and a run that cannot do the first
    should still do the second.
    """
    _grid_leaf(store, "us", "broad", "return_5d_local", "returns", "ok")

    result = census.run(as_of=AS_OF)
    assert result.verdict == VERDICT_NO_DATA
    assert result.grid_reach
    assert "what each indicator family can reach" in result.report()


def test_a_family_below_the_ranking_floor_is_named(store, config, census):
    floor = config.require_int("grid.min_cells_for_ranked_view")
    for index in range(floor + 1):
        _grid_leaf(store, f"m{index}", "broad", "return_5d_local", "returns", "ok")
    _grid_leaf(store, "m0", "broad", "iv_minus_hv", "iv_hv_spread", "ok")

    result = census.run(as_of=AS_OF)
    assert "iv_hv_spread" in result.thin_families
    assert "returns" not in result.thin_families
    assert "THIN:" in result.report()


def test_reach_reaches_the_stored_detail(store, census):
    _grid_leaf(store, "us", "broad", "return_5d_local", "returns", "ok")
    result = census.run(as_of=AS_OF)
    census.write(result)

    detail = json.loads(
        store.execute(
            "SELECT detail FROM universe_census WHERE as_of = ?", (AS_OF.isoformat(),)
        ).fetchone()[0]
    )
    assert detail["grid_reach"]
    assert detail["grid_reach"][0]["family"] == "returns"
    assert "thin_families" in detail
