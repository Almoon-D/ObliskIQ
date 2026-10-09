"""``obliskiq-compute``: the entrypoint, its exit codes and its dry run.

The computation layer touches no network. These tests prove it end to end
against a store filled by hand, which is the same thing a sandbox with no IBKR
access can do.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from obliskiq.collect.universe import load_universe_file
from obliskiq.compute.cli import EXIT_DEGRADED, EXIT_FAILED, EXIT_OK, build_parser, main

AS_OF = date(2026, 3, 2)


def _bars_for(contract_id: int, *, sessions: int = 400) -> list[tuple]:
    return [
        (
            contract_id,
            (AS_OF - timedelta(days=sessions - n)).isoformat(),
            100.0 + n * 0.1,
            101.0 + n * 0.1,
            99.0 + n * 0.1,
            100.0 + n * 0.1,
        )
        for n in range(sessions)
    ]


@pytest.fixture()
def cli_env(tmp_path, monkeypatch, config, store):
    """A store where EVERY instrument universe.yaml declares is seeded and collected.

    It used to seed one native index, and that made the coverage floor pass for
    the wrong reason. The denominator is cells universe.yaml maps an instrument
    to; before this session a mapped-but-unseeded cell reported
    `no_instrument_mapped` and dropped OUT of the denominator, so a store missing
    fifty-six of its fifty-seven instruments divided twelve by thirty-six and
    called it healthy. A failed `obliskiq-collect universe` was invisible to the
    one flag that exists to catch it.

    So the fixture now seeds what a working collector would, and the floor is
    measured against a store that really is collected.
    """
    declared = load_universe_file(config.require_str("collectors.universe.path"), config)
    measuring = [row for row in declared.instruments if row.grid_measures]
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class, market, "
            "sector, is_native_index, grid_measures, tradable, untradeable_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, 'measurement instrument')",
            [
                (
                    index + 1,
                    row.symbol,
                    row.currency,
                    row.asset_class,
                    row.market,
                    row.sector,
                    int(row.is_native_index),
                )
                for index, row in enumerate(measuring)
            ],
        )
        connection.executemany(
            "INSERT INTO bars (contract_id, ts, freq, open, high, low, close, volume, "
            "source) VALUES (?, ?, '1d', ?, ?, ?, ?, 1000, 'cp_api')",
            [row for index in range(len(measuring)) for row in _bars_for(index + 1)],
        )
    monkeypatch.setenv("OBLISK_DB_PATH", str(store.path))
    monkeypatch.setenv("OBLISK_CONFIG_PATH", str(config.source))
    return store


def _run(*argv):
    return main([*argv, "--as-of", AS_OF.isoformat()])


def test_the_parser_offers_every_command():
    parser = build_parser()
    for command in ("grid", "rankings", "fx", "commodity", "census", "all"):
        assert parser.parse_args([command]).command == command


def test_a_grid_build_writes_cells_and_reports_coverage(cli_env, capsys):
    code = _run("grid")
    out = capsys.readouterr().out

    assert code in (EXIT_OK, EXIT_DEGRADED)
    assert "coverage" in out
    assert cli_env.execute("SELECT COUNT(*) FROM grid").fetchone()[0] > 0


def test_a_grid_whose_inputs_have_not_arrived_is_degraded_rather_than_failed(store, config):
    """A young store REPORTS, it does not fail.

    The instrument is mapped and the bars are not there, which is the shape of a
    real collection gap: something is supposed to fill these cells and has not.
    That is what the floor is for.
    """
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class, market, "
            "sector, is_native_index, grid_measures, tradable, untradeable_reason) VALUES "
            "(1, 'SPX', 'USD', 'IND', 'us', 'broad', 1, 1, 0, 'index level')"
        )
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("OBLISK_DB_PATH", str(store.path))
        patch.setenv("OBLISK_CONFIG_PATH", str(config.source))
        assert _run("grid") == EXIT_DEGRADED


def test_a_grid_is_not_degraded_merely_for_being_small(cli_env):
    """The floor became reachable, and this is the regression that proves it.

    A minority of the board's cells carry an instrument and the rest are DECLARED
    gaps. Under the old metric — populated over every leaf written, declared gaps
    included — this could not exceed about a third of the floor whatever
    collection did, so it reported DEGRADED forever and the flag carried no
    information. Measured against what is collectible it passes, and a genuine
    collection outage is once again the thing that trips it.
    """
    assert _run("grid") == EXIT_OK


def test_a_universe_that_was_never_seeded_is_degraded(store, config, monkeypatch):
    """The failure the old denominator hid completely.

    universe.yaml maps an instrument to fifty-seven cells; this store holds one of
    them. Those cells used to report `no_instrument_mapped` — structural, out of
    the denominator — so the ratio was twelve over thirty-six and the build said
    it was healthy while the universe collector had plainly not run. A cell whose
    instrument is DECLARED and missing is a collection gap, and it stays in the
    denominator where the floor can see it.
    """
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class, market, "
            "sector, is_native_index, grid_measures, tradable, untradeable_reason) VALUES "
            "(1, 'SPX', 'USD', 'IND', 'us', 'broad', 1, 1, 0, 'index level')"
        )
        connection.executemany(
            "INSERT INTO bars (contract_id, ts, freq, open, high, low, close, volume, "
            "source) VALUES (?, ?, '1d', ?, ?, ?, ?, 1000, 'cp_api')",
            _bars_for(1),
        )
    monkeypatch.setenv("OBLISK_DB_PATH", str(store.path))
    monkeypatch.setenv("OBLISK_CONFIG_PATH", str(config.source))
    assert _run("grid") == EXIT_DEGRADED

    reasons = {
        row["reason_code"]
        for row in store.execute(
            "SELECT DISTINCT reason_code FROM grid WHERE as_of = ? AND status = 'n/a'",
            (AS_OF.isoformat(),),
        )
    }
    assert "instrument_not_seeded" in reasons


def test_a_dry_run_writes_nothing(cli_env):
    _run("grid", "--dry-run")
    assert cli_env.execute("SELECT COUNT(*) FROM grid").fetchone()[0] == 0
    assert cli_env.execute("SELECT COUNT(*) FROM collector_runs").fetchone()[0] == 0


def test_rankings_run_over_a_built_grid(cli_env, capsys):
    _run("grid")
    capsys.readouterr()
    code = _run("rankings")
    out = capsys.readouterr().out

    assert code == EXIT_OK
    assert "ranked rows" in out
    assert cli_env.execute("SELECT COUNT(*) FROM correlation_matrix").fetchone()[0] >= 0


def test_the_fx_command_decomposes_every_market(cli_env, capsys):
    code = _run("fx")
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "markets decomposed" in out
    assert cli_env.execute("SELECT COUNT(*) FROM fx_state").fetchone()[0] > 0


def test_the_commodity_command_runs_over_an_empty_root_table(cli_env, capsys):
    code = _run("commodity")
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "roots" in out


def test_the_census_reports_no_data_without_a_chain(cli_env, capsys):
    code = _run("census")
    out = capsys.readouterr().out
    assert code == EXIT_DEGRADED
    assert "could not run" in out


def test_all_runs_the_sequence_in_dependency_order(cli_env, capsys):
    code = _run("all")
    capsys.readouterr()
    assert code in (EXIT_OK, EXIT_DEGRADED)

    runs = [
        row[0]
        for row in cli_env.execute(
            "SELECT collector FROM collector_runs ORDER BY run_id"
        ).fetchall()
    ]
    # solve-iv leads: the grid reads the implied vols it writes, so solving after
    # the grid would publish a grid built on the PREVIOUS run's solves.
    assert runs == [
        "compute:solve-iv",
        "compute:grid",
        "compute:rankings",
        "compute:fx",
        "compute:commodity",
    ]


def test_an_unparseable_as_of_fails_cleanly(cli_env):
    assert main(["grid", "--as-of", "the third of never"]) == EXIT_FAILED


def test_every_run_is_recorded_in_the_run_log(cli_env):
    _run("grid")
    row = cli_env.execute(
        "SELECT collector, status, rows_written FROM collector_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    assert row["collector"] == "compute:grid"
    assert row["status"] in ("ok", "degraded")
    assert row["rows_written"] > 0
