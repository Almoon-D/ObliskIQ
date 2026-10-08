"""The collector entrypoint and the schema it writes into.

The entrypoint is what a systemd timer actually invokes, with nobody watching. Its
exit code is the only signal a scheduler gets, and the order of the daily sequence
is the only thing standing between a fresh strike band and a degraded one.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from obliskiq.collect.cli import (
    DAILY_SEQUENCE,
    EXIT_DEGRADED,
    EXIT_FAILED,
    EXIT_OK,
    NEEDS_MARKET_DATA,
    Context,
    _collect_vendor_chain,
    _exit_code,
    _run_command,
    build_parser,
)
from obliskiq.collect.runlog import CollectorRun, RunStatus
from obliskiq.errors import ObliskError, StoreError

# ── the command surface ──


@pytest.mark.parametrize("command", ["universe", "backfill", "chain", "curve", "macro", "daily"])
def test_every_collector_is_reachable_from_the_entrypoint(command):
    assert build_parser().parse_args([command]).command == command


def test_macro_does_not_need_a_market_data_route():
    """It reaches FRED, ECB, CFTC and EIA, and no IBKR endpoint at all.

    It used to be built a Client Portal route regardless, so
    ``obliskiq-collect macro`` refused to start without OBLISK_IBKR_CP_BASE_URL —
    and deploy/obliskiq-collect@macro.timer runs it as a unit of its own, so the
    whole macro layer was unreachable whenever the IBKR credentials were absent,
    expired or not yet issued. Every source it pulls but two needs no key at all.
    """
    assert "macro" not in NEEDS_MARKET_DATA


@pytest.mark.parametrize("command", ["universe", "backfill", "chain", "curve", "daily"])
def test_the_collectors_that_read_ibkr_still_declare_that_they_do(command):
    assert command in NEEDS_MARKET_DATA


def test_vendor_chain_builds_its_own_client_and_needs_no_route():
    """It reaches marketdata.app through a VendorClient of its own."""
    assert "vendor-chain" not in NEEDS_MARKET_DATA


def test_every_step_of_the_daily_sequence_is_covered_one_way_or_the_other():
    """`daily` needs a route because steps inside it do; none may be forgotten.

    A step added to the sequence that reads market data, and left out of
    NEEDS_MARKET_DATA, would fail at the accessor rather than at startup.
    """
    assert "daily" in NEEDS_MARKET_DATA
    for step in DAILY_SEQUENCE:
        assert step in NEEDS_MARKET_DATA or step in {"vendor-chain", "macro"}


def test_a_collector_reaching_for_a_route_that_was_never_built_is_refused(config, store):
    """Optional, not a null object. A stub would report success over nothing."""
    context = Context(
        config=config,
        settings=None,
        store=store,
        market_data=None,
        http=None,
        dry_run=True,
        only=None,
        force=False,
        skip=frozenset(),
    )
    with pytest.raises(ObliskError, match="needs a market-data route"):
        context.require_market_data()


def test_an_unknown_command_is_refused():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["nonsense"])


def test_the_fixture_route_is_selectable_so_a_dry_run_touches_no_network():
    args = build_parser().parse_args(["chain", "--source", "fixture", "--fixtures", "x"])
    assert args.source == "fixture" and args.fixtures == "x"


def test_only_is_repeatable():
    args = build_parser().parse_args(["chain", "--only", "SPY", "--only", "QQQ"])
    assert args.only == ["SPY", "QQQ"]


def test_dry_run_and_force_are_available():
    args = build_parser().parse_args(["macro", "--dry-run", "--force"])
    assert args.dry_run and args.force


# ── --as-of, which picks the marketdata.app CREDIT TIER ──
#
# `historical = trade_date != today` inside VendorChainCollector._collect_one is
# the only thing deciding whether a request carries `date=`, and the walk used to
# END at that same date. So the newest session collected was always the current
# one, on the live per-symbol tier, and buying ONE past session meant paying for
# today first. --as-of moves the end of the walk and leaves the tier reference
# alone; passing it as `today` instead would put the pinned date back on the live
# tier and stamp the CURRENT chain with it.


def test_as_of_parses_to_a_date():
    args = build_parser().parse_args(["vendor-chain", "--as-of", "2026-03-16"])
    assert args.as_of == date(2026, 3, 16)


def test_as_of_defaults_to_none_so_the_collector_keeps_its_own_default():
    assert build_parser().parse_args(["vendor-chain"]).as_of is None


@pytest.mark.parametrize("raw", ["not-a-date", "2026-13-01", "16/03/2026", ""])
def test_a_date_that_is_not_a_date_is_refused_before_anything_is_spent(raw):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["vendor-chain", "--as-of", raw])


@pytest.mark.parametrize("raw", ["20260316", "2026-W11-1"])
def test_only_the_YYYY_MM_DD_spelling_is_accepted(raw):
    """``date.fromisoformat`` takes week dates and basic format on 3.11+. A
    session is written one way everywhere else in this package, and a flag that
    silently accepts a second spelling is a flag whose logs cannot be grepped."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["vendor-chain", "--as-of", raw])


def _context_with(config, store, **kwargs):
    base = {
        "config": config,
        # Only the vendor token is read on this path, and the recorder below
        # never spends it. A real Settings would need an environment.
        "settings": SimpleNamespace(marketdata_api_token="tok"),
        "store": store,
        "market_data": None,
        "http": None,
        "dry_run": True,
        "only": None,
        "force": False,
        "skip": frozenset(),
    }
    return Context(**{**base, **kwargs})


@pytest.mark.parametrize("command", ["macro", "chain", "backfill", "daily"])
async def test_as_of_is_refused_on_a_command_that_would_ignore_it(config, store, command):
    """Refused, not ignored. Only vendor-chain reads it, so accepting it on
    `daily` would pin one step of five while an operator believed otherwise."""
    context = _context_with(config, store, as_of=date(2026, 3, 16))
    args = build_parser().parse_args([command])
    with pytest.raises(ObliskError, match="--as-of applies to vendor-chain"):
        await _run_command(args, context)


async def test_only_is_refused_on_universe_rather_than_silently_ignored(config, store):
    """It walked all 91 targets while the operator read the flag as narrowing it.

    `universe` reconciles the whole file against the account in ONE pass: it
    retires the row a corrected conid supersedes and ages out portfolio rows it
    did not see this run. A subset changes what "did not see" means, so the
    filtered run would retire on evidence it never gathered — which is why this
    is a refusal and not a filter that was never wired up.
    """
    context = _context_with(config, store, only=["ASML"])
    args = build_parser().parse_args(["universe"])
    with pytest.raises(ObliskError, match="--only does not apply to universe"):
        await _run_command(args, context)


@pytest.mark.parametrize("command", ["chain", "backfill", "curve", "macro"])
async def test_only_still_reaches_every_command_that_reads_it(config, store, command):
    """The refusal above is one command wide, and this is what stops it widening."""
    context = _context_with(config, store, only=["SPY"])
    args = build_parser().parse_args([command])
    with pytest.raises(Exception) as caught:  # any failure is fine but THIS one
        await _run_command(args, context)
    assert "--only does not apply" not in str(caught.value)


async def test_a_future_session_is_refused(config, store):
    """The walk would treat it as the current session, buy today's chain undated
    on the LIVE tier, and file it under a day that has not happened."""
    context = _context_with(config, store, as_of=date.today() + timedelta(days=400))
    args = build_parser().parse_args(["vendor-chain"])
    with pytest.raises(ObliskError, match="is in the future"):
        await _run_command(args, context)


async def test_as_of_reaches_the_collector_as_as_of_and_never_as_today(config, store, monkeypatch):
    """The wiring, and the distinction it turns on.

    `today` is the collector's TIER REFERENCE. Handing it the pinned date would
    make that date look like the current session again — an undated, live-tier
    request whose rows carry a past date they did not come from. `as_of` moves
    the end of the walk and leaves the reference alone.
    """
    seen = {}

    class _Recorder:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, run, **kwargs):
            seen.update(kwargs)
            return 0

    monkeypatch.setattr("obliskiq.collect.cli.VendorChainCollector", _Recorder)
    monkeypatch.setattr("obliskiq.collect.cli.VendorClient", lambda *a, **k: None)

    pinned = date(2026, 3, 16)
    context = _context_with(config, store, as_of=pinned)
    with CollectorRun(store, "vendor-chain", dry_run=True) as run:
        await _collect_vendor_chain(context, run)
    assert seen["as_of"] == pinned
    assert seen.get("today") is None, "the pinned date must not become the tier reference"


async def test_no_as_of_hands_the_collector_none_rather_than_a_second_clock_reading(
    config, store, monkeypatch
):
    """None means "use your own default", which is today in New York. Resolving
    it in the CLI would read the clock a second time, and the two readings can
    straddle a session boundary."""
    seen = {}

    class _Recorder:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, run, **kwargs):
            seen.update(kwargs)
            return 0

    monkeypatch.setattr("obliskiq.collect.cli.VendorChainCollector", _Recorder)
    monkeypatch.setattr("obliskiq.collect.cli.VendorClient", lambda *a, **k: None)

    context = _context_with(config, store, as_of=None)
    with CollectorRun(store, "vendor-chain", dry_run=True) as run:
        await _collect_vendor_chain(context, run)
    assert seen["as_of"] is None


# ── exit codes ──


def test_a_clean_run_exits_zero():
    assert _exit_code([RunStatus.OK, RunStatus.OK]) == EXIT_OK


def test_a_degraded_run_has_its_own_exit_code(config):
    """So a scheduler can alert on it without treating it as an outage."""
    assert _exit_code([RunStatus.OK, RunStatus.DEGRADED]) == EXIT_DEGRADED
    assert EXIT_DEGRADED != EXIT_OK != EXIT_FAILED


def test_a_failure_outranks_a_degradation():
    assert _exit_code([RunStatus.DEGRADED, RunStatus.PARTIAL]) == EXIT_FAILED
    assert _exit_code([RunStatus.DEGRADED, RunStatus.ERROR]) == EXIT_FAILED


def test_a_step_skipped_by_a_filter_does_not_fail_the_sequence():
    """--only scopes the whole daily sequence, but the collectors do not share a
    namespace: an equity symbol means something to chain and backfill and nothing
    to curve, which tracks futures roots. The skipped step contributes no status,
    so a scoped run that collected what it could still exits zero."""
    assert _exit_code([RunStatus.OK, RunStatus.OK]) == EXIT_OK
    # And the status is not silently one of the ones a scheduler alerts on.
    assert RunStatus.SKIPPED not in (RunStatus.OK, RunStatus.ERROR, RunStatus.PARTIAL)


def test_the_systemd_unit_treats_the_degraded_code_as_a_success(repo_root):
    """Re-running a degraded collector would spend the API budget again on the same
    weak inputs; it needs looking at, not retrying."""
    unit = (repo_root / "deploy" / "obliskiq-collect@.service").read_text(encoding="utf-8")
    assert f"SuccessExitStatus={EXIT_OK} {EXIT_DEGRADED}" in unit
    assert "Restart=on-failure" in unit


def test_the_timers_catch_up_a_missed_run(repo_root):
    """Persistent=true is load-bearing: for a series that cannot be backfilled, it
    is the difference between a gap and no gap."""
    for name in ("obliskiq-collect@daily.timer", "obliskiq-collect@macro.timer"):
        assert "Persistent=true" in (repo_root / "deploy" / name).read_text(encoding="utf-8")


def test_the_console_script_is_declared(repo_root):
    import tomllib

    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["scripts"]["obliskiq-collect"] == "obliskiq.collect.cli:main"


def test_the_entrypoint_binds_nothing_and_starts_no_server(repo_root):
    """A standalone entrypoint, deliberately separate from the MCP server process:
    that one holds a position ledger it can write to, and the two have different
    blast radii."""
    import ast

    source = (repo_root / "src" / "obliskiq" / "collect" / "cli.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert not any("server" in name for name in imported)
    for forbidden in ("require_mcp_port", "require_mcp_auth_token", "mcp_host", ".serve("):
        assert forbidden not in source


# ── the schema the collectors write into ──


def test_migration_002_is_applied_by_a_fresh_store(store):
    applied = {row[0] for row in store.execute("SELECT name FROM schema_migrations ORDER BY name")}
    assert "001_initial.sql" in applied
    assert "002_collectors.sql" in applied


def test_the_run_status_check_admits_skipped(store):
    """Migration 004 widened the CHECK. Without it the status is rejected at write
    time and the whole run fails on the way out — which is how the skip was found."""
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO collector_runs (collector, started_at, status) VALUES (?, ?, ?)",
            ("curve", "2026-08-23T00:00:00Z", RunStatus.SKIPPED),
        )
    row = store.execute(
        "SELECT status FROM collector_runs WHERE collector = 'curve' ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == RunStatus.SKIPPED


def test_an_invented_run_status_is_still_refused(store):
    """The CHECK is widened, not removed. The store surfaces the violation as a
    StoreError rather than letting a bare sqlite3 exception escape the layer."""
    with (
        pytest.raises(StoreError, match="CHECK constraint failed"),
        store.transaction() as connection,
    ):
        connection.execute(
            "INSERT INTO collector_runs (collector, started_at, status) VALUES (?, ?, ?)",
            ("curve", "2026-08-23T00:00:00Z", "probably_fine"),
        )


def test_the_collector_tables_exist(store):
    tables = set(store.table_names())
    assert {"futures_roots", "collector_state", "collector_runs"} <= tables


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("universe", "origin"),
        ("universe", "last_seen_at"),
        ("universe", "collect_chain"),
        ("universe", "collect_bars"),
        ("universe", "listed_on"),
        ("chain_snapshots", "sigma_used"),
        ("chain_snapshots", "sigma_source"),
    ],
)
def test_the_added_columns_are_present(store, table, column):
    assert column in {row[1] for row in store.execute(f"PRAGMA table_info({table})")}


def test_universe_origin_is_constrained(seeded_store):
    from obliskiq.errors import StoreError

    with (
        pytest.raises(StoreError, match="CHECK constraint failed"),
        seeded_store.transaction() as connection,
    ):
        connection.execute("UPDATE universe SET origin = 'guessed' WHERE contract_id = 1")


def test_the_source_column_still_means_the_collection_route(seeded_store):
    """It is what makes a wholesale migration possible if a CP OAuth session turns
    out to invalidate the MCP session, so it must not be overloaded with provenance."""
    from obliskiq.errors import StoreError

    with seeded_store.transaction() as connection:
        connection.execute("UPDATE universe SET source = 'cp_api' WHERE contract_id = 1")
    with (
        pytest.raises(StoreError, match="CHECK constraint failed"),
        seeded_store.transaction() as connection,
    ):
        connection.execute("UPDATE universe SET source = 'portfolio_sync' WHERE contract_id = 1")


def test_the_existing_run_history_survives_the_table_rebuild(tmp_path, config):
    """002 recreates collector_runs to widen its CHECK. A migration that silently
    dropped the history would erase the only record of what ran before it."""
    import shutil
    import sqlite3

    from obliskiq.store import open_store
    from obliskiq.store.db import MIGRATIONS_DIR, migrate

    only_001 = tmp_path / "migrations"
    only_001.mkdir()
    shutil.copy(MIGRATIONS_DIR / "001_initial.sql", only_001)

    db = tmp_path / "history.db"
    with open_store(db, config, directory=only_001) as store, store.transaction() as connection:
        connection.execute(
            "INSERT INTO collector_runs (collector, started_at, status, rows_written) "
            "VALUES (?, ?, ?, ?)",
            ("chain", "2026-01-14T22:30:00Z", "ok", 1234),
        )

    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    migrate(connection)
    row = connection.execute("SELECT collector, rows_written FROM collector_runs").fetchone()
    assert (row["collector"], row["rows_written"]) == ("chain", 1234)
    # And the widened status is now accepted.
    connection.execute(
        "INSERT INTO collector_runs (collector, started_at, status) VALUES (?, ?, ?)",
        ("chain", "2026-01-15T22:30:00Z", "degraded"),
    )
    connection.close()


def test_deleting_a_universe_row_would_take_its_history_with_it(seeded_store):
    """The reason reconciliation clears the collection flags instead of deleting."""
    with seeded_store.transaction() as connection:
        connection.execute(
            "INSERT INTO bars (contract_id, ts, freq, close) VALUES (?, ?, ?, ?)",
            (1, "2026-01-15T00:00:00Z", "1d", 100.0),
        )
    assert seeded_store.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 1

    with seeded_store.transaction() as connection:
        connection.execute("DELETE FROM universe WHERE contract_id = 1")
    assert seeded_store.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 0


def test_a_futures_root_round_trips(store):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO futures_roots (root, exchange, currency, bucket) VALUES (?, ?, ?, ?)",
            ("CL", "NYMEX", "USD", "energy"),
        )
    row = store.execute("SELECT root, tracked, seasonal FROM futures_roots").fetchone()
    assert (row["root"], row["tracked"], row["seasonal"]) == ("CL", 1, 0)
