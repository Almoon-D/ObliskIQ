"""The migration chain walked forward over a database that already holds data.

Every pre-existing forward test starts one version behind head and seeds one row
per table. That proves the last step; it does not prove the walk. A deployed
store is migrated from wherever it happens to be, through every intervening
step, with real row counts underneath — and two of the steps rebuild the same
table (002 and 004 both recreate ``collector_runs``, 005 rebuilds
``option_ledger``), which is exactly where a forward walk can lose rows that a
fresh-store test would never notice.

These tests seed at 001 and walk the whole chain, and they use enough rows that
a rebuild dropping or reordering them is visible.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import date, timedelta

import pytest

from obliskiq.errors import StoreError
from obliskiq.store.db import MIGRATIONS_DIR, available_migrations, migrate, open_store

#: Enough rows that an ordering or truncation fault is visible, and enough to
#: cross SQLite's page boundaries during a table rebuild.
BAR_COUNT = 2000
RUN_COUNT = 250
GRID_COUNT = 500


def _migrations_through(tmp_path, last_prefix):
    """A migrations directory holding every file up to and including a prefix."""
    directory = tmp_path / f"migrations_to_{last_prefix}"
    directory.mkdir()
    for path in available_migrations():
        shutil.copy(MIGRATIONS_DIR / path.name, directory / path.name)
        if path.name.startswith(last_prefix):
            break
    return directory


def _seed_at_001(store):
    """A store as a long-lived deployment would hold it after the first schema."""
    start = date(2024, 1, 1)
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class) "
            "VALUES (1, 'ASML', 'EUR', 'equity')"
        )
        connection.executemany(
            "INSERT INTO bars (contract_id, ts, freq, open, high, low, close, volume, source) "
            "VALUES (1, ?, '1d', ?, ?, ?, ?, ?, 'cp_api')",
            [
                (
                    (start + timedelta(days=n)).isoformat() + "T00:00:00Z",
                    100.0 + n,
                    101.0 + n,
                    99.0 + n,
                    100.5 + n,
                    1000 + n,
                )
                for n in range(BAR_COUNT)
            ],
        )
        connection.executemany(
            "INSERT INTO macro (series_id, source, reference_date, release_date, value, "
            "frequency) VALUES ('CPI', 'fred', ?, ?, ?, 'monthly')",
            [
                (
                    (start + timedelta(days=31 * n)).isoformat(),
                    (start + timedelta(days=31 * n + 12)).isoformat(),
                    2.0 + n * 0.01,
                )
                for n in range(60)
            ],
        )


def _seed_grid(store, count):
    """Grid rows as a deployment would hold them before 010 rebuilds the table."""
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO grid (as_of, market, sector, indicator, value, status, reason, "
            "window) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "2026-03-02",
                    f"market_{n % 13}",
                    f"sector_{n % 12}",
                    f"indicator_{n}",
                    None if n % 3 == 0 else float(n),
                    "n/a" if n % 3 == 0 else "ok",
                    "nothing was collected" if n % 3 == 0 else None,
                    "21d",
                )
                for n in range(count)
            ],
        )


def _seed_collector_runs(store, count):
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO collector_runs (collector, started_at, finished_at, status, "
            "rows_written) VALUES ('backfill', ?, ?, 'ok', ?)",
            [
                (
                    f"2024-02-{(n % 28) + 1:02d}T00:00:00Z",
                    f"2024-02-{(n % 28) + 1:02d}T00:05:00Z",
                    n,
                )
                for n in range(count)
            ],
        )


def test_the_whole_chain_applies_forward_over_a_populated_store(tmp_path, config):
    """Seed at 001, walk 002 through head, lose nothing.

    This is the path a deployed database actually takes, and the one no test
    covered: the existing forward tests each start at head minus one.
    """
    db = tmp_path / "long_lived.db"
    with open_store(db, config, directory=_migrations_through(tmp_path, "001")) as store:
        _seed_at_001(store)
        before_bars = store.execute("SELECT ts, close FROM bars ORDER BY ts").fetchall()
        before_macro = store.execute(
            "SELECT series_id, reference_date, release_date, value FROM macro "
            "ORDER BY reference_date"
        ).fetchall()

    # The real directory: every remaining migration, in one walk.
    with open_store(db, config) as store:
        applied = [row["name"] for row in store.execute("SELECT name FROM schema_migrations")]
        assert applied == [p.name for p in available_migrations()]

        after_bars = store.execute("SELECT ts, close FROM bars ORDER BY ts").fetchall()
        after_macro = store.execute(
            "SELECT series_id, reference_date, release_date, value FROM macro "
            "ORDER BY reference_date"
        ).fetchall()

    assert len(after_bars) == BAR_COUNT
    assert [tuple(r) for r in after_bars] == [tuple(r) for r in before_bars]
    assert [tuple(r) for r in after_macro] == [tuple(r) for r in before_macro]


def test_both_collector_runs_rebuilds_carry_the_history_across(tmp_path, config):
    """002, 004 and 013 each rebuild collector_runs. Rows written between survive.

    The interaction is the point: a run seeded after 002 has to pass through
    004's rebuild and then 013's, and nothing covered repeated rebuilds over the
    same rows. run_id is carried across explicitly rather than regenerated,
    because collector_runs.sequence_id addresses rows by it.
    """
    db = tmp_path / "runs.db"
    with open_store(db, config, directory=_migrations_through(tmp_path, "002")) as store:
        _seed_collector_runs(store, RUN_COUNT)
        before = [
            tuple(row)
            for row in store.execute(
                "SELECT collector, started_at, status, rows_written FROM collector_runs "
                "ORDER BY run_id"
            )
        ]

    with open_store(db, config) as store:
        after = [
            tuple(row)
            for row in store.execute(
                "SELECT collector, started_at, status, rows_written FROM collector_runs "
                "ORDER BY run_id"
            )
        ]

    assert len(after) == RUN_COUNT
    assert after == before, "a collector_runs rebuild changed rows it was only to move"


def test_013_preserves_run_ids_and_leaves_the_new_columns_unclaimed(tmp_path, config):
    """A run that predates the sequence row belonged to no sequence, and NULL is
    the honest answer. Inventing a sequence of one would make history look like
    it was collected under a discipline that did not exist yet."""
    db = tmp_path / "continuity.db"
    with open_store(db, config, directory=_migrations_through(tmp_path, "004")) as store:
        _seed_collector_runs(store, RUN_COUNT)
        before = [row[0] for row in store.execute("SELECT run_id FROM collector_runs ORDER BY 1")]

    with open_store(db, config) as store:
        rows = store.execute(
            "SELECT run_id, sequence_id, step_index FROM collector_runs ORDER BY run_id"
        ).fetchall()
        assert [row["run_id"] for row in rows] == before
        assert all(row["sequence_id"] is None and row["step_index"] is None for row in rows)
        # And the sequence table arrives empty rather than back-filled from guesses.
        assert store.execute("SELECT COUNT(*) FROM collector_sequences").fetchone()[0] == 0


def test_014_preserves_ranked_rows_and_leaves_the_denominator_unrecorded(tmp_path, config):
    """A row written before the columns existed has no denominator, and NULL says
    so. Back-filling one from today's grid would put a number on a table nobody
    measured, which is the same failure as reporting an untested gate as passed."""
    db = tmp_path / "reach.db"
    with (
        open_store(db, config, directory=_migrations_through(tmp_path, "013")) as store,
        store.transaction() as connection,
    ):
        connection.execute(
            "INSERT INTO grid_rankings (as_of, dimension, rank, market, sector, "
            "indicator, value, window, detail) VALUES "
            "('2026-03-02', 'widest_iv_hv', 1, 'us', 'broad', 'iv_minus_hv', -0.08, "
            "'IV vs 30d RV', 'seeded')"
        )

    with open_store(db, config) as store:
        rows = store.execute(
            "SELECT dimension, rank, value, reachable, ranked_over FROM grid_rankings"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["dimension"] == "widest_iv_hv"
        assert rows[0]["value"] == -0.08
        assert rows[0]["reachable"] is None
        assert rows[0]["ranked_over"] is None


def test_016_preserves_ranked_rows_and_leaves_the_trajectory_unrecorded(tmp_path, config):
    """A row written before the column existed carried no trajectory, and NULL says so.

    Back-filling one from today's grid would stamp today's returns on a table
    built from an older one — the same failure as 014's denominator, and the same
    answer: unrecorded is not the claim that the cell held none of the leaves.
    """
    db = tmp_path / "trajectory.db"
    with (
        open_store(db, config, directory=_migrations_through(tmp_path, "015")) as store,
        store.transaction() as connection,
    ):
        connection.execute(
            "INSERT INTO grid_rankings (as_of, dimension, rank, market, sector, "
            "indicator, value, window, detail, status, reachable, ranked_over) VALUES "
            "('2026-03-02', 'most_compressed', 1, 'us', 'broad', "
            "'parkinson_vol_30d_percentile', 0.04, '2y', 'seeded', 'ok', 12, 9)"
        )

    with open_store(db, config) as store:
        rows = store.execute(
            "SELECT dimension, value, status, reachable, ranked_over, trajectory FROM grid_rankings"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["value"] == 0.04
        assert rows[0]["status"] == "ok"
        assert rows[0]["reachable"] == 12
        assert rows[0]["trajectory"] is None


def test_019_carries_every_correlation_row_across_as_an_equity_pair(tmp_path, config):
    """The table is rebuilt because the primary key changes, and a rebuild is
    where a forward walk loses rows.

    Every pre-019 row is an equity-to-equity market pair, which is all the table
    could hold — so the copy stamps the class rather than guessing it. The two
    new counts stay NULL: nothing recorded them, and a zero would be the claim
    that these rows rested on no observations, which is the same failure as
    reporting an untested gate as passed.
    """
    db = tmp_path / "correlation.db"
    pairs = [
        ("us", "japan", 30, 0.62, 1),
        ("us", "japan", 90, 0.55, 0),
        ("us", "korea", 30, None, 0),
    ]
    with (
        open_store(db, config, directory=_migrations_through(tmp_path, "018")) as store,
        store.transaction() as connection,
    ):
        connection.executemany(
            "INSERT INTO correlation_matrix (as_of, market_a, market_b, window_days, "
            "correlation, norm_mean, norm_sd, z_score, flagged, status, reason) "
            "VALUES ('2026-03-02', ?, ?, ?, ?, 0.4, 0.1, 2.2, ?, ?, ?)",
            [
                (a, b, window, value, flagged, "ok" if value is not None else "n/a", "seeded")
                for a, b, window, value, flagged in pairs
            ],
        )

    with open_store(db, config) as store:
        rows = store.execute(
            "SELECT * FROM correlation_matrix ORDER BY market_b, window_days"
        ).fetchall()
        assert len(rows) == len(pairs), "a rebuild must not drop rows"
        for row in rows:
            assert row["asset_class_a"] == "equity"
            assert row["asset_class_b"] == "equity"
            # An equity leg is identified by its market, which is why the copy is
            # exact rather than back-filled with an index symbol this migration
            # has no way to look up.
            assert row["series_a"] == row["market_a"]
            assert row["series_b"] == row["market_b"]
            assert row["n_obs"] is None
            assert row["norm_independent"] is None
        first = rows[0]
        assert (first["market_a"], first["market_b"]) == ("us", "japan")
        assert first["window_days"] == 30
        assert first["correlation"] == 0.62
        assert first["flagged"] == 1
        assert first["z_score"] == 2.2


def test_the_chain_is_idempotent_over_a_populated_store(tmp_path, config):
    """Re-opening applies nothing and changes nothing.

    002, 004, 005 and 006 use ALTER/RENAME and would fail if re-run; only the
    schema_migrations ledger stops them, so this asserts the ledger holds when
    there is data underneath rather than on an empty file.
    """
    db = tmp_path / "twice.db"
    with open_store(db, config, directory=_migrations_through(tmp_path, "001")) as store:
        _seed_at_001(store)

    with open_store(db, config) as store:
        first = store.execute("SELECT COUNT(*) FROM bars").fetchone()[0]

    with open_store(db, config) as store:
        assert migrate(store.connection) == []
        assert store.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == first


def test_a_partly_applied_chain_stops_at_the_last_good_migration(tmp_path, config):
    """A broken step leaves the store at the previous one, not half-way through.

    Each migration runs inside its own transaction, so the failure must not take
    the ones before it with it — and it must not record itself as applied.
    """
    directory = _migrations_through(tmp_path, "003")
    (directory / "004_broken.sql").write_text("SELECT * FROM a_table_that_is_not_there;\n")

    db = tmp_path / "broken.db"
    with pytest.raises(StoreError, match="004_broken"):
        open_store(db, config, directory=directory)

    # 001-003 stand, and the broken step is not recorded.
    with open_store(db, config, directory=directory, apply_migrations=False) as store:
        applied = {row["name"] for row in store.execute("SELECT name FROM schema_migrations")}
    assert "004_broken.sql" not in applied
    assert any(name.startswith("003") for name in applied)


def test_foreign_keys_survive_the_table_rebuilds(tmp_path, config):
    """A rebuild via RENAME must not orphan the children pointing at it."""
    db = tmp_path / "fk.db"
    with open_store(db, config, directory=_migrations_through(tmp_path, "001")) as store:
        _seed_at_001(store)

    with open_store(db, config) as store:
        store.connection.execute("PRAGMA foreign_keys = ON")
        violations = store.connection.execute("PRAGMA foreign_key_check").fetchall()
        assert violations == [], f"the chain left dangling references: {violations}"
        integrity = store.connection.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok"


def test_the_grid_rebuild_in_010_carries_every_row_across(tmp_path, config):
    """010 rebuilds `grid` to widen its status CHECK, and a rebuild can lose rows.

    SQLite cannot ALTER a CHECK, so adding `inapplicable` meant CREATE / INSERT
    SELECT / DROP / RENAME on a WITHOUT ROWID table carrying five columns that
    003, 007 and 009 bolted on afterwards. Getting that column list wrong drops
    data silently — the same failure 002, 004 and 005 are walked for here.
    """
    db = tmp_path / "grid_rebuild.db"
    with open_store(db, config, directory=_migrations_through(tmp_path, "009")) as store:
        _seed_at_001(store)
        _seed_grid(store, GRID_COUNT)
        before = store.execute(
            "SELECT as_of, market, sector, indicator, value, status, reason FROM grid "
            "ORDER BY indicator"
        ).fetchall()
        assert len(before) == GRID_COUNT

    with open_store(db, config) as store:
        after = store.execute(
            "SELECT as_of, market, sector, indicator, value, status, reason FROM grid "
            "ORDER BY indicator"
        ).fetchall()
        assert [tuple(row) for row in after] == [tuple(row) for row in before]

        # The point of the rebuild: the new status is accepted and a bare one is not.
        store.connection.execute(
            "INSERT INTO grid (as_of, market, sector, indicator, status, reason) "
            "VALUES ('2026-03-03', 'korea', 'broad', 'implied_vol_atm', 'inapplicable', "
            "'no chain is reachable')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute(
                "INSERT INTO grid (as_of, market, sector, indicator, status, reason) "
                "VALUES ('2026-03-03', 'korea', 'broad', 'x', 'not_a_status', 'why')"
            )
        # An inapplicable cell still has to say why.
        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute(
                "INSERT INTO grid (as_of, market, sector, indicator, status) "
                "VALUES ('2026-03-03', 'korea', 'broad', 'y', 'inapplicable')"
            )


def test_010_backfills_history_exchange_from_the_listing_venue(tmp_path, config):
    """The column has to be a no-op for every row that existed before it.

    NULL means "send no exchange", which is a real instruction, so leaving the
    backfill out would silently change how every held instrument is requested.
    """
    db = tmp_path / "history_exchange.db"
    with (
        open_store(db, config, directory=_migrations_through(tmp_path, "009")) as store,
        store.transaction() as connection,
    ):
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class, exchange) "
            "VALUES (2, 'DAX', 'EUR', 'IND', 'EUREX')"
        )
        connection.execute(
            "INSERT INTO universe (contract_id, symbol, currency, asset_class, exchange) "
            "VALUES (3, 'NOVENUE', 'EUR', 'STK', NULL)"
        )

    with open_store(db, config) as store:
        rows = dict(
            store.execute("SELECT symbol, history_exchange FROM universe").fetchall()  # type: ignore[arg-type]
        )
        assert rows["DAX"] == "EUREX"
        assert rows["NOVENUE"] is None
