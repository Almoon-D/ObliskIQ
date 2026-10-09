"""The store: migrations, the schema additions, and the invariants enforced in SQL.

Several rules in BUILD-BRIEF.md are enforced by the database rather than by
convention, because the skill must never be the only thing standing between a bad
write and the ledger. Those are tested here.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import contextlib
import sqlite3

import pytest

from obliskiq.errors import StoreError
from obliskiq.store import open_store
from obliskiq.store.db import available_migrations, migrate

EXPECTED_TABLES = {
    "bars",
    "bar_watermarks",
    "chain_snapshots",
    "collector_runs",
    "curve_snapshots",
    "external_positions",
    "grid",
    "macro",
    "macro_fetch_log",
    "option_ledger",
    "position_ledger",
    "universe",
    "universe_census",
    "zombie_observations",
}


def test_every_table_in_the_brief_exists(store):
    assert EXPECTED_TABLES.issubset(set(store.table_names()))


def test_migrations_are_recorded_and_not_reapplied(tmp_path, config):
    db = tmp_path / "m.db"
    with open_store(db, config) as first:
        applied = [row["name"] for row in first.execute("SELECT name FROM schema_migrations")]
    assert applied == [p.name for p in available_migrations()]

    with open_store(db, config) as second:
        assert migrate(second.connection) == []


def test_an_edited_migration_is_refused(tmp_path, config, monkeypatch):
    """Migrations are forward-only. Editing one that has run must fail loudly."""
    db = tmp_path / "m.db"
    with open_store(db, config):
        pass
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE schema_migrations SET checksum = 'tampered'")
    with sqlite3.connect(db) as connection, pytest.raises(StoreError, match="forward-only"):
        migrate(connection)


def test_wal_and_foreign_keys_are_on(store):
    assert store.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert store.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_the_busy_timeout_is_applied_and_is_not_zero(store, config):
    """WAL lets a reader and a writer coexist; the busy timeout is what makes
    them WAIT for each other instead of failing.

    The collectors write while the server reads, from separate processes, on one
    OCPU where `obliskiq-compute all` and a tool call genuinely overlap. At
    `busy_timeout = 0` SQLite returns SQLITE_BUSY immediately and the loser of
    the race raises "database is locked" — which is the failure this pragma was
    added to prevent, and setting the key to 0 passed the whole suite because
    only journal_mode and foreign_keys were ever read back.

    Asserted against config rather than against a literal: the value is
    runtime.sqlite's to choose, and zero is the one choice that is not a choice.
    """
    declared = config.require_int("runtime.sqlite.busy_timeout_ms")
    assert declared > 0, "a zero busy timeout is no timeout: the first contention raises"
    assert store.execute("PRAGMA busy_timeout").fetchone()[0] == declared


def test_the_synchronous_pragma_is_applied_too(store, config):
    """The third of the four; read back for the same reason as the other three.

    A pragma that is set and never verified is a line of code, not a guarantee.
    """
    modes = {"OFF": 0, "NORMAL": 1, "FULL": 2, "EXTRA": 3}
    declared = config.require_str("runtime.sqlite.synchronous").upper()
    assert declared in modes, f"runtime.sqlite.synchronous={declared!r} is not a pragma value"
    assert store.execute("PRAGMA synchronous").fetchone()[0] == modes[declared]


# ── transactions nest, because two writes have to be one ──


def _count_externals(store) -> int:
    return int(store.execute("SELECT COUNT(*) FROM external_positions").fetchone()[0])


def _insert_external(connection, broker: str) -> None:
    connection.execute(
        "INSERT INTO external_positions (broker, symbol, isin, quantity, currency, "
        "market_value_base, last_updated, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (broker, "ASML", None, 1, "EUR", 100.0, "2026-08-01", None),
    )


def test_a_nested_transaction_commits_only_with_its_outer_one(store):
    """The inner block must not commit the outer one.

    This is what makes a pair of writes atomic: log_entry's position and the
    premium it commits to the sleeve are written by two modules, each opening
    its own transaction. If the inner one committed, a failure after it would
    leave a position standing with no commitment against it.
    """
    with pytest.raises(RuntimeError), store.transaction() as outer:
        _insert_external(outer, "OUTER")
        with store.transaction() as inner:
            _insert_external(inner, "INNER")
        raise RuntimeError("the caller fails after the inner block returned")

    assert _count_externals(store) == 0, "the inner write survived a failed outer transaction"


def test_a_nested_failure_rolls_back_to_its_own_savepoint(store):
    """An inner failure the caller catches must not discard the outer's work."""
    with store.transaction() as outer:
        _insert_external(outer, "OUTER")
        with contextlib.suppress(StoreError), store.transaction() as inner:
            _insert_external(inner, "INNER")
            # An untradeable universe row with no reason: refused by a CHECK.
            inner.execute(
                "INSERT INTO universe (contract_id, symbol, currency, asset_class, tradable) "
                "VALUES (?, ?, ?, ?, ?)",
                (97, "NO_REASON", "EUR", "CMD", 0),
            )

    brokers = [
        row[0] for row in store.execute("SELECT broker FROM external_positions ORDER BY broker")
    ]
    assert brokers == ["OUTER"], "the outer write was lost, or the inner one survived"
    assert not store.execute("SELECT 1 FROM universe WHERE contract_id = 97").fetchall()


def test_both_levels_commit_when_both_succeed(store):
    with store.transaction() as outer:
        _insert_external(outer, "OUTER")
        with store.transaction() as inner:
            _insert_external(inner, "INNER")

    brokers = [
        row[0] for row in store.execute("SELECT broker FROM external_positions ORDER BY broker")
    ]
    assert brokers == ["INNER", "OUTER"]


# ── Session 1 schema additions ──


def test_external_positions_accepts_a_manual_holding(store):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO external_positions (broker, symbol, isin, quantity, currency, "
            "market_value_base, last_updated, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("DEGIRO", "ASML", "NL0010273215", 12, "EUR", 8400.0, "2026-08-01", "manual"),
        )
    row = store.execute("SELECT * FROM external_positions").fetchone()
    assert row["broker"] == "DEGIRO"
    assert row["market_value_base"] == 8400.0


def test_external_positions_are_unique_per_broker_and_instrument(seeded_store):
    row = ("REVOLUT", "AAPL", "US0378331005", 5, "USD", 900.0, "2026-08-01", None)
    with seeded_store.transaction() as connection:
        connection.execute(
            "INSERT INTO external_positions (broker, symbol, isin, quantity, currency, "
            "market_value_base, last_updated, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
    with pytest.raises(StoreError), seeded_store.transaction() as connection:
        connection.execute(
            "INSERT INTO external_positions (broker, symbol, isin, quantity, currency, "
            "market_value_base, last_updated, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )


def test_curve_snapshots_are_idempotent_per_day(store):
    row = ("2026-08-20", "CL", "2026-12-18", 74.2, 0.031)
    sql = (
        "INSERT OR REPLACE INTO curve_snapshots "
        "(snapshot_date, root, expiry, price, annualised_roll) VALUES (?, ?, ?, ?, ?)"
    )
    with store.transaction() as connection:
        connection.execute(sql, row)
        connection.execute(sql, row)
    assert store.execute("SELECT count(*) FROM curve_snapshots").fetchone()[0] == 1


def test_universe_carries_tradable_macro_signal_and_reason(store):
    """A commodity can inform the regime without ever being a candidate."""
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class, tradable, "
            "macro_signal, untradeable_reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (99, "OLIVE_OIL", "EUR", "CMD", 0, 1, "no futures market, no ETF, no viable equity"),
        )
    row = store.execute("SELECT * FROM universe WHERE contract_id = 99").fetchone()
    assert row["tradable"] == 0 and row["macro_signal"] == 1
    assert row["untradeable_reason"]


def test_an_untradeable_row_must_state_why(store):
    with pytest.raises(StoreError), store.transaction() as connection:
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class, tradable) "
            "VALUES (?, ?, ?, ?, ?)",
            (98, "MYSTERY", "EUR", "CMD", 0),
        )


# ── invariants enforced in SQL ──


def test_thesis_text_cannot_be_updated_in_place(seeded_store):
    """BUILD-BRIEF: thesis_text is written once at open and never edited."""
    with seeded_store.transaction() as connection:
        connection.execute(
            "INSERT INTO position_ledger (contract_id, opened_at, entry_price, thesis_text, "
            "invalidation_trigger) VALUES (?, ?, ?, ?, ?)",
            (1, "2026-08-01", 100.0, "original thesis", "close below 90"),
        )
    with pytest.raises(StoreError, match="frozen at entry"), seeded_store.transaction() as conn:
        conn.execute("UPDATE position_ledger SET thesis_text = 'rewritten'")


def test_invalidation_trigger_cannot_be_updated_in_place(seeded_store):
    with seeded_store.transaction() as connection:
        connection.execute(
            "INSERT INTO position_ledger (contract_id, opened_at, entry_price, thesis_text, "
            "invalidation_trigger) VALUES (?, ?, ?, ?, ?)",
            (1, "2026-08-01", 100.0, "thesis", "close below 90"),
        )
    with pytest.raises(StoreError, match="frozen at entry"), seeded_store.transaction() as conn:
        conn.execute("UPDATE position_ledger SET invalidation_trigger = 'anything'")


def test_reunderwrite_log_is_appendable_while_the_thesis_is_not(seeded_store):
    with seeded_store.transaction() as connection:
        connection.execute(
            "INSERT INTO position_ledger (contract_id, opened_at, entry_price, thesis_text, "
            "invalidation_trigger) VALUES (?, ?, ?, ?, ?)",
            (1, "2026-08-01", 100.0, "thesis", "close below 90"),
        )
        connection.execute(
            "UPDATE position_ledger SET reunderwrite_log = ?",
            ('[{"at": "2026-09-01", "note": "new dated catalyst"}]',),
        )
    row = seeded_store.execute("SELECT * FROM position_ledger").fetchone()
    assert row["thesis_text"] == "thesis"
    assert "new dated catalyst" in row["reunderwrite_log"]


def test_closing_a_position_requires_an_exit_reason(seeded_store):
    with pytest.raises(StoreError), seeded_store.transaction() as connection:
        connection.execute(
            "INSERT INTO position_ledger (contract_id, opened_at, entry_price, thesis_text, "
            "invalidation_trigger, closed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (1, "2026-08-01", 100.0, "t", "i", "2026-09-01"),
        )


def test_grid_cells_that_are_not_ok_must_carry_a_reason(store):
    """Never a silent proxy, never an estimate presented as data."""
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO grid (as_of, market, sector, indicator, value, status, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-08-20",
                "korea",
                "materials",
                "valuation_pctile",
                None,
                "n/a",
                "no free source for global historical fundamentals",
            ),
        )
    with pytest.raises(StoreError), store.transaction() as connection:
        connection.execute(
            "INSERT INTO grid (as_of, market, sector, indicator, value, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-08-20", "japan", "energy", "iv_hv_spread", None, "n/a"),
        )


def test_option_ledger_rejects_an_unlisted_event_type(seeded_store):
    with pytest.raises(StoreError), seeded_store.transaction() as connection:
        connection.execute(
            "INSERT INTO option_ledger (event_at, event_type, penalty_balance_after) "
            "VALUES (?, ?, ?)",
            ("2026-08-20", "vibes", 0.0),
        )


def test_chain_snapshot_right_is_constrained(seeded_store):
    with pytest.raises(StoreError), seeded_store.transaction() as connection:
        connection.execute(
            "INSERT INTO chain_snapshots (snapshot_date, underlying_id, expiry, strike, right) "
            "VALUES (?, ?, ?, ?, ?)",
            ("2026-08-20", 1, "2026-12-18", 100.0, "X"),
        )


def test_source_column_is_constrained_to_the_two_collection_routes(seeded_store):
    """Rows record mcp | cp_api so collection can be migrated wholesale."""
    with pytest.raises(StoreError), seeded_store.transaction() as connection:
        connection.execute(
            "INSERT INTO bars (contract_id, ts, freq, close, source) VALUES (?, ?, ?, ?, ?)",
            (1, "2026-08-20T00:00:00Z", "1d", 10.0, "yahoo"),
        )


def test_macro_stores_both_reference_and_release_dates(store):
    """Aligning CPI to its reference month builds look-ahead bias into every
    percentile derived from it."""
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO macro (series_id, source, reference_date, release_date, value, frequency) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("CPIAUCSL", "fred", "2026-07-01", "2026-08-12", 320.1, "monthly"),
        )
    row = store.execute("SELECT * FROM macro").fetchone()
    assert row["reference_date"] != row["release_date"]


def test_zombie_observations_are_one_row_per_session(seeded_store):
    """Two consecutive sessions are required, so the state must be per-session."""
    with seeded_store.transaction() as connection:
        connection.execute(
            "INSERT INTO position_ledger (position_id, contract_id, opened_at, entry_price, "
            "thesis_text, invalidation_trigger) VALUES (?, ?, ?, ?, ?, ?)",
            (1, 1, "2026-08-01", 100.0, "t", "i"),
        )
        for day in ("2026-08-19", "2026-08-20"):
            connection.execute(
                "INSERT INTO zombie_observations (position_id, observed_on, all_conditions_met) "
                "VALUES (?, ?, ?)",
                (1, day, 1),
            )
    count = seeded_store.execute(
        "SELECT count(*) FROM zombie_observations WHERE position_id = 1 AND all_conditions_met = 1"
    ).fetchone()[0]
    assert count == 2


def test_bars_are_unique_per_contract_frequency_and_timestamp(seeded_store):
    sql = "INSERT OR REPLACE INTO bars (contract_id, ts, freq, close) VALUES (?, ?, ?, ?)"
    with seeded_store.transaction() as connection:
        connection.execute(sql, (1, "2026-08-20T00:00:00Z", "1d", 10.0))
        connection.execute(sql, (1, "2026-08-20T00:00:00Z", "1d", 11.0))
    rows = seeded_store.execute("SELECT close FROM bars").fetchall()
    assert len(rows) == 1 and rows[0]["close"] == 11.0


# ── 005 applies FORWARD, onto a store that already holds data ──


def _migrations_before_005(tmp_path):
    """A migrations directory holding 001-004: the schema as it stood before."""
    import shutil

    from obliskiq.store.db import MIGRATIONS_DIR

    earlier = tmp_path / "migrations_001_004"
    earlier.mkdir()
    for path in available_migrations():
        if path.name.startswith("005"):
            break
        shutil.copy(MIGRATIONS_DIR / path.name, earlier / path.name)
    return earlier


def _seed_pre_005(store):
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class) "
            "VALUES (1, 'TEST', 'EUR', 'STK')"
        )
        connection.execute(
            "INSERT INTO position_ledger (contract_id, opened_at, entry_price, thesis_text, "
            "invalidation_trigger) VALUES (1, '2026-01-01', 2.5, 'thesis', 'invalidation')"
        )
        connection.execute(
            "INSERT INTO option_ledger (event_at, event_type, position_id, realised_pnl, "
            "penalty_added, penalty_balance_after, note) "
            "VALUES ('2026-01-02', 'loss_detected', 1, -100.0, 200.0, 200.0, 'pre-existing')"
        )
        connection.execute(
            "INSERT INTO underlying_snapshots (snapshot_date, contract_id, implied_vol, source) "
            "VALUES ('2026-01-02', 1, 0.3142, 'cp_api')"
        )


def test_the_option_ledger_rebuild_carries_existing_rows_across(tmp_path, config):
    """005 rebuilds option_ledger to widen a CHECK. Nothing may be lost doing it.

    A fresh store proves the end state; it does not prove the forward path, and
    the forward path is the one the deployed database will actually take.
    """
    db = tmp_path / "existing.db"
    with open_store(db, config, directory=_migrations_before_005(tmp_path)) as store:
        _seed_pre_005(store)
        before = [dict(row) for row in store.execute("SELECT * FROM option_ledger")]

    with open_store(db, config) as store:
        after = [dict(row) for row in store.execute("SELECT * FROM option_ledger")]

    # Compared column by column over the columns that existed then: later
    # migrations add columns, and a row gaining a NULL it did not have is the
    # normal forward path. What must never change is a value already written.
    assert len(after) == len(before), "the rebuild lost or duplicated a row"
    for old, new in zip(before, after, strict=True):
        assert {key: new[key] for key in old} == old, (
            "the rebuild changed a value it was only supposed to move"
        )


def test_the_widened_check_still_refuses_an_unknown_event_type(tmp_path, config):
    db = tmp_path / "existing.db"
    with open_store(db, config, directory=_migrations_before_005(tmp_path)) as store:
        _seed_pre_005(store)

    with open_store(db, config) as store:
        with store.transaction() as connection:
            connection.execute(
                "INSERT INTO option_ledger (event_at, event_type, position_id, premium_paid, "
                "penalty_added, penalty_balance_after) "
                "VALUES ('2026-01-03', 'premium_committed', 1, 500.0, 0.0, 200.0)"
            )
        with pytest.raises((StoreError, sqlite3.Error)), store.transaction() as connection:
            connection.execute(
                "INSERT INTO option_ledger (event_at, event_type, penalty_added, "
                "penalty_balance_after) VALUES ('2026-01-04', 'nonsense', 0.0, 0.0)"
            )


def test_the_new_percentile_columns_arrive_empty_beside_collected_data(tmp_path, config):
    """A column added to a populated table must not disturb what is already there."""
    db = tmp_path / "existing.db"
    with open_store(db, config, directory=_migrations_before_005(tmp_path)) as store:
        _seed_pre_005(store)

    with open_store(db, config) as store:
        row = store.execute(
            "SELECT implied_vol, source, iv_percentile_13w, iv_percentile_26w, "
            "iv_percentile_route FROM underlying_snapshots"
        ).fetchone()

    assert row["implied_vol"] == pytest.approx(0.3142)
    assert row["source"] == "cp_api"
    assert row["iv_percentile_13w"] is None
    assert row["iv_percentile_26w"] is None
    assert row["iv_percentile_route"] is None


# ── 006 applies FORWARD, onto a store that already holds snapshots ──


def _migrations_before_006(tmp_path):
    """A migrations directory holding 001-005: the schema as it stood before."""
    import shutil

    from obliskiq.store.db import MIGRATIONS_DIR

    earlier = tmp_path / "migrations_001_005"
    earlier.mkdir()
    for path in available_migrations():
        if path.name.startswith("006"):
            break
        shutil.copy(MIGRATIONS_DIR / path.name, earlier / path.name)
    return earlier


def _seed_pre_006(store):
    """One row in each table 006 alters, as a populated database would hold."""
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class) "
            "VALUES (1, 'TEST', 'EUR', 'STK')"
        )
        connection.execute(
            "INSERT INTO chain_snapshots (snapshot_date, underlying_id, expiry, strike, "
            "right, iv, source) VALUES ('2026-01-02', 1, '2026-06-19', 100.0, 'C', 0.31, 'cp_api')"
        )
        connection.execute(
            "INSERT INTO underlying_snapshots (snapshot_date, contract_id, implied_vol, source) "
            "VALUES ('2026-01-02', 1, 0.3142, 'cp_api')"
        )
        connection.execute(
            "INSERT INTO curve_snapshots (snapshot_date, root, expiry, price, source) "
            "VALUES ('2026-01-02', 'CL', '2026-03-20', 71.5, 'cp_api')"
        )


def test_market_data_status_arrives_null_beside_already_collected_rows(tmp_path, config):
    """The column is added to three POPULATED tables. Nothing already held may move.

    And the new column must arrive NULL rather than back-filled: NULL means "the
    route said nothing", and stamping 'REALTIME' onto historical rows would invent
    exactly the fact the column exists to record.
    """
    db = tmp_path / "existing.db"
    with open_store(db, config, directory=_migrations_before_006(tmp_path)) as store:
        _seed_pre_006(store)
        before = {
            table: [tuple(row) for row in store.execute(f"SELECT * FROM {table}")]
            for table in ("chain_snapshots", "underlying_snapshots", "curve_snapshots")
        }

    with open_store(db, config) as store:
        for table in ("chain_snapshots", "underlying_snapshots", "curve_snapshots"):
            row = store.execute(f"SELECT * FROM {table}").fetchone()
            assert row["market_data_status"] is None, f"{table} was back-filled"
            # Every pre-existing column keeps its value, in order.
            assert tuple(row)[: len(before[table][0])] == before[table][0]


def test_a_status_the_api_has_never_emitted_is_still_accepted(tmp_path, config):
    """Deliberately no CHECK: the value set is being discovered, not known.

    A constraint written from today's observations turns tomorrow's new status
    into a collector crash, on a day whose data cannot be recollected.
    """
    db = tmp_path / "open.db"
    with open_store(db, config) as store:
        _seed_pre_006(store)
        with store.transaction() as connection:
            connection.execute(
                "UPDATE chain_snapshots SET market_data_status = 'SOMETHING_NEW_IN_2027'"
            )
        assert (
            store.execute("SELECT market_data_status FROM chain_snapshots").fetchone()[0]
            == "SOMETHING_NEW_IN_2027"
        )


# ── 008: the marketdata.app collection route ────────────────────────────────


def _migrations_before_008(tmp_path):
    """A migrations directory holding 001-007: the schema as it stood before."""
    import shutil

    from obliskiq.store.db import MIGRATIONS_DIR

    earlier = tmp_path / "migrations_001_007"
    earlier.mkdir()
    for path in available_migrations():
        if path.name.startswith("008"):
            break
        shutil.copy(MIGRATIONS_DIR / path.name, earlier / path.name)
    return earlier


def _seed_pre_008(store):
    """One row in each table 008 REBUILDS, as a populated database would hold.

    Both are WITHOUT ROWID with a foreign key to universe, so a rebuild that
    drops a column, reorders the primary key or loses the reference would show
    up here rather than on the day a real backfill runs over it.
    """
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class) "
            "VALUES (1, 'TEST', 'EUR', 'STK')"
        )
        connection.execute(
            "INSERT INTO chain_snapshots (snapshot_date, underlying_id, expiry, strike, "
            "right, contract_id, bid, ask, iv, delta, source, sigma_used, sigma_source, "
            "market_data_status) VALUES ('2026-01-02', 1, '2026-06-19', 100.0, 'C', 777, "
            "1.5, 1.7, 0.31, 0.42, 'cp_api', 0.25, 'primary', 'REALTIME')"
        )
        connection.execute(
            "INSERT INTO underlying_snapshots (snapshot_date, contract_id, last, implied_vol, "
            "iv_percentile_52w, source, iv_percentile_13w, iv_percentile_route, "
            "market_data_status) VALUES ('2026-01-02', 1, 50.0, 0.3142, 0.8, 'cp_api', "
            "0.7, 'mcp', 'DELAYED')"
        )


def test_the_rebuild_carries_every_collected_row_across_unchanged(tmp_path, config):
    """008 REBUILDS two populated tables to widen a CHECK. Nothing already held
    may move, and option_symbol must arrive NULL rather than invented: the OCC
    symbol for a row identified only by an IBKR conid is not recoverable."""
    db = tmp_path / "existing.db"
    tables = ("chain_snapshots", "underlying_snapshots")
    with open_store(db, config, directory=_migrations_before_008(tmp_path)) as store:
        _seed_pre_008(store)
        before = {
            table: [tuple(row) for row in store.execute(f"SELECT * FROM {table}")]
            for table in tables
        }

    with open_store(db, config) as store:
        for table in tables:
            rows = [tuple(row) for row in store.execute(f"SELECT * FROM {table}")]
            assert len(rows) == 1, f"{table} lost or duplicated a row in the rebuild"
        chain = store.execute("SELECT * FROM chain_snapshots").fetchone()
        assert chain["option_symbol"] is None, "option_symbol was back-filled"
        # option_symbol is appended, so every pre-existing column keeps its value
        # AND its position — the same prefix comparison the 006 test makes.
        for table in tables:
            row = store.execute(f"SELECT * FROM {table}").fetchone()
            assert tuple(row)[: len(before[table][0])] == before[table][0]
        assert store.execute("PRAGMA foreign_key_check").fetchall() == []
        assert store.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_the_vendor_route_is_accepted_where_it_writes_and_nowhere_else(seeded_store):
    """marketdata_app is a third route, not a relaxation.

    It writes chains and underlying snapshots, so those two accept it. bars,
    universe and curve_snapshots receive no vendor rows and keep the two-value
    CHECK: a constraint widened "for symmetry" is a constraint that has stopped
    constraining.
    """
    with seeded_store.transaction() as connection:
        connection.execute(
            "INSERT INTO chain_snapshots (snapshot_date, underlying_id, expiry, strike, "
            "right, option_symbol, iv, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-08-20",
                1,
                "2026-12-18",
                100.0,
                "C",
                "TEST261218C00100000",
                0.3,
                "marketdata_app",
            ),
        )
        connection.execute(
            "INSERT INTO underlying_snapshots (snapshot_date, contract_id, implied_vol, source) "
            "VALUES (?, ?, ?, ?)",
            ("2026-08-20", 1, 0.31, "marketdata_app"),
        )

    for sql, params in (
        (
            "INSERT INTO bars (contract_id, ts, freq, close, source) VALUES (?, ?, ?, ?, ?)",
            (1, "2026-08-20T00:00:00Z", "1d", 10.0, "marketdata_app"),
        ),
        (
            "INSERT INTO universe (contract_id, symbol, currency, asset_class, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (2, "OTHER", "EUR", "STK", "marketdata_app"),
        ),
        (
            "INSERT INTO curve_snapshots (snapshot_date, root, expiry, price, source) "
            "VALUES (?, ?, ?, ?, ?)",
            ("2026-08-20", "CL", "2026-12-19", 70.0, "marketdata_app"),
        ),
    ):
        with pytest.raises(StoreError), seeded_store.transaction() as connection:
            connection.execute(sql, params)


def test_an_unknown_route_is_still_refused_on_the_widened_tables(seeded_store):
    """Widening the CHECK by one value must not turn it into no check at all."""
    with pytest.raises(StoreError), seeded_store.transaction() as connection:
        connection.execute(
            "INSERT INTO chain_snapshots (snapshot_date, underlying_id, expiry, strike, "
            "right, source) VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-08-21", 1, "2026-12-18", 100.0, "C", "yahoo"),
        )


def test_the_vendor_percentile_route_stays_refused(seeded_store):
    """The vendor serves no IV percentile. Collecting its chains is what makes
    the percentile SELF-computed, so a route claiming a vendor percentile from
    marketdata_app would record a fact that does not exist."""
    with pytest.raises(StoreError), seeded_store.transaction() as connection:
        connection.execute(
            "INSERT INTO underlying_snapshots (snapshot_date, contract_id, implied_vol, "
            "source, iv_percentile_route) VALUES (?, ?, ?, ?, ?)",
            ("2026-08-22", 1, 0.31, "marketdata_app", "marketdata_app"),
        )


def test_a_fetch_is_logged_once_per_symbol_and_session(store):
    """vendor_fetch_log is what stops a restarted backfill re-spending credits."""
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO vendor_fetch_log (symbol, trade_date, status, contracts, credits) "
            "VALUES ('SPY', '2026-03-16', 'ok', 62, 1)"
        )
        connection.execute(
            "INSERT INTO vendor_fetch_log (symbol, trade_date, status, contracts, credits) "
            "VALUES ('SPY', '2026-03-16', 'ok', 62, 1) "
            "ON CONFLICT (symbol, trade_date, vendor) DO UPDATE SET contracts = 99"
        )
    rows = store.execute("SELECT symbol, contracts, vendor FROM vendor_fetch_log").fetchall()
    assert len(rows) == 1
    assert rows[0]["contracts"] == 99
    assert rows[0]["vendor"] == "marketdata_app"

    with pytest.raises(StoreError), store.transaction() as connection:
        connection.execute(
            "INSERT INTO vendor_fetch_log (symbol, trade_date, status) "
            "VALUES ('QQQ', '2026-03-16', 'maybe')"
        )
