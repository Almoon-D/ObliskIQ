"""The connection: pragmas, transactions, snapshots of reads and the ways opening can fail.

Ported in intent from the legacy ``test_store.py``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from obliskiq.config import ObliskConfig
from obliskiq.errors import StoreError
from obliskiq.store import Store, open_store
from obliskiq.store.db import MEMORY, _apply_pragmas
from obliskiq.store.migrations import available_migrations

CORE_TABLES = {
    "bars",
    "chain_snapshots",
    "curves",
    "instruments",
    "macro",
    "run_steps",
    "runs",
    "schema_migrations",
    "snapshots",
}


def _scratch(store: Store) -> None:
    store.execute("CREATE TABLE scratch (n INTEGER PRIMARY KEY, label TEXT)")


def _labels(store: Store) -> list[str]:
    return [row["label"] for row in store.execute("SELECT label FROM scratch ORDER BY n")]


# ── opening ──


def test_every_core_table_exists(store: Store) -> None:
    assert set(store.table_names()) >= CORE_TABLES


def test_write_ahead_logging_and_foreign_keys_are_on(store: Store) -> None:
    assert store.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_the_configured_busy_timeout_and_synchronous_level_are_applied(
    store: Store, shipped_settings: ObliskConfig
) -> None:
    assert store.execute("PRAGMA busy_timeout").fetchone()[0] == (
        shipped_settings.store.busy_timeout_ms
    )
    assert shipped_settings.store.busy_timeout_ms > 0
    levels = {"NORMAL": 1, "FULL": 2, "EXTRA": 3}
    assert (
        store.execute("PRAGMA synchronous").fetchone()[0]
        == levels[shipped_settings.store.synchronous]
    )


def test_another_synchronous_level_is_honoured(
    open_test_store: Callable[..., Store], tmp_path: Path
) -> None:
    store = open_test_store(tmp_path / "full.db", synchronous="FULL")
    assert store.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_an_in_memory_store_works_without_write_ahead_logging(
    open_test_store: Callable[..., Store],
) -> None:
    store = open_test_store(MEMORY)
    assert store.path == MEMORY
    assert set(store.table_names()) >= CORE_TABLES


def test_opening_creates_the_missing_directory(
    open_test_store: Callable[..., Store], tmp_path: Path
) -> None:
    store = open_test_store(tmp_path / "deep" / "er" / "store.db")
    assert (tmp_path / "deep" / "er" / "store.db").is_file()
    assert store.path.endswith("store.db")


def test_open_store_uses_the_configured_path(
    shipped_settings: ObliskConfig, tmp_path: Path
) -> None:
    with open_store(shipped_settings, tmp_path) as opened:
        assert Path(opened.path) == tmp_path / shipped_settings.paths.database
        assert "instruments" in opened.table_names()


def test_reopening_an_existing_store_changes_nothing(
    open_test_store: Callable[..., Store], tmp_path: Path
) -> None:
    path = tmp_path / "again.db"
    first = open_test_store(path)
    first.execute(
        "INSERT INTO runs VALUES ('r', 'daily', '2026-10-09', '2026-10-09T22:30:00Z', "
        "NULL, 'running', 3, 0, NULL, NULL)"
    )
    second = open_test_store(path)
    assert second.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    applied = second.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert applied == len(available_migrations())


def test_a_path_that_cannot_be_opened_is_a_store_error(
    open_test_store: Callable[..., Store], tmp_path: Path
) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises((StoreError, OSError)):
        open_test_store(blocker / "store.db")


def test_a_file_that_is_not_a_database_is_a_store_error(
    open_test_store: Callable[..., Store], tmp_path: Path
) -> None:
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not an SQLite database, only text " * 100)
    with pytest.raises(StoreError):
        open_test_store(garbage)


def test_a_database_that_would_not_enter_write_ahead_logging_is_refused() -> None:
    class Stub:
        def execute(self, sql: str) -> Stub:
            self.sql = sql
            return self

        def fetchone(self) -> tuple[str]:
            return ("delete",)

    with pytest.raises(StoreError, match="write-ahead logging"):
        _apply_pragmas(Stub(), "/some/file.db", 1000, "NORMAL")  # type: ignore[arg-type]


def test_a_database_that_would_not_enable_foreign_keys_is_refused() -> None:
    class Stub:
        def __init__(self) -> None:
            self.last = ""

        def execute(self, sql: str) -> Stub:
            self.last = sql
            return self

        def fetchone(self) -> tuple[object]:
            if "journal_mode" in self.last:
                return ("wal",)
            return (1,) if "synchronous" in self.last else (0,)

    with pytest.raises(StoreError, match="foreign keys"):
        _apply_pragmas(Stub(), "/some/file.db", 1000, "NORMAL")  # type: ignore[arg-type]


def test_an_unknown_synchronous_level_is_a_store_error(
    open_test_store: Callable[..., Store], tmp_path: Path
) -> None:
    with pytest.raises(StoreError, match="synchronous must be one of"):
        open_test_store(tmp_path / "x.db", synchronous="WHENEVER")


def test_a_directory_is_not_a_database(
    open_test_store: Callable[..., Store], tmp_path: Path
) -> None:
    with pytest.raises(StoreError, match="could not open the store"):
        open_test_store(tmp_path)


def test_a_non_positive_busy_timeout_is_a_store_error(
    open_test_store: Callable[..., Store], tmp_path: Path
) -> None:
    with pytest.raises(StoreError, match="busy_timeout_ms must be positive"):
        open_test_store(tmp_path / "x.db", busy_timeout_ms=0)


# ── transactions ──


def test_a_transaction_commits_on_success(store: Store) -> None:
    _scratch(store)
    with store.transaction():
        store.execute("INSERT INTO scratch (label) VALUES ('kept')")
    assert _labels(store) == ["kept"]


def _insert(store: Store, label: str) -> None:
    store.execute("INSERT INTO scratch (label) VALUES (?)", (label,))


def _write_then(store: Store, label: str, then: Callable[[], None]) -> None:
    """Write inside a transaction, then run ``then`` (which may raise) before it ends."""
    with store.transaction():
        _insert(store, label)
        then()


def _boom() -> None:
    raise RuntimeError("boom")


def test_a_transaction_rolls_back_on_any_exception(store: Store) -> None:
    _scratch(store)
    with pytest.raises(RuntimeError):
        _write_then(store, "lost", _boom)
    assert _labels(store) == []


def test_a_keyboard_interrupt_rolls_back_too(store: Store) -> None:
    def interrupt() -> None:
        raise KeyboardInterrupt

    _scratch(store)
    with pytest.raises(KeyboardInterrupt):
        _write_then(store, "lost", interrupt)
    assert _labels(store) == []
    _write_then(store, "after", lambda: None)  # the store is usable afterwards
    assert _labels(store) == ["after"]


def test_a_sqlite_error_inside_a_transaction_becomes_a_store_error(store: Store) -> None:
    def duplicate_key() -> None:
        store.execute("INSERT INTO scratch (n, label) VALUES (2, 'duplicate')")

    def disk_error() -> None:
        raise sqlite3.OperationalError("disk I/O error")

    _scratch(store)
    store.execute("INSERT INTO scratch (n, label) VALUES (2, 'unrelated')")
    for failure in (duplicate_key, disk_error):
        with pytest.raises(StoreError, match="transaction failed"):
            _write_then(store, "lost", failure)
        assert _labels(store) == ["unrelated"]


def test_a_nested_transaction_commits_only_with_its_outer_one(store: Store) -> None:
    def inner_then_fail() -> None:
        with store.transaction():
            _insert(store, "inner")
        assert _labels(store) == ["outer", "inner"]  # visible inside, not yet committed
        _boom()

    _scratch(store)
    with pytest.raises(RuntimeError):
        _write_then(store, "outer", inner_then_fail)
    assert _labels(store) == []


def test_a_nested_failure_rolls_back_to_its_own_savepoint(store: Store) -> None:
    def failing_inner() -> None:
        _write_then(store, "inner", _boom)

    def swallow() -> None:
        with pytest.raises(RuntimeError):
            failing_inner()
        _insert(store, "after")

    _scratch(store)
    _write_then(store, "outer", swallow)
    assert _labels(store) == ["outer", "after"]


def test_an_unswallowed_inner_failure_rolls_the_outer_level_back_too(store: Store) -> None:
    def failing_inner() -> None:
        with store.transaction():
            _boom()

    _scratch(store)
    with pytest.raises(RuntimeError):
        _write_then(store, "outer", failing_inner)
    assert _labels(store) == []


def test_both_levels_commit_when_both_succeed(store: Store) -> None:
    _scratch(store)
    with store.transaction():
        with store.transaction():
            store.execute("INSERT INTO scratch (label) VALUES ('inner')")
        store.execute("INSERT INTO scratch (label) VALUES ('outer')")
    assert _labels(store) == ["inner", "outer"]


def test_a_failed_commit_is_reported_and_leaves_no_transaction_open(store: Store) -> None:
    store.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    store.execute(
        "CREATE TABLE child (id INTEGER PRIMARY KEY, parent INTEGER "
        "REFERENCES parent (id) DEFERRABLE INITIALLY DEFERRED)"
    )
    with pytest.raises(StoreError, match="transaction failed"), store.transaction():
        store.execute("INSERT INTO child (parent) VALUES (99)")  # violates only at commit
    assert not store.connection.in_transaction
    assert store.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 0


def test_the_write_lock_is_taken_up_front(
    open_test_store: Callable[..., Store], tmp_path: Path
) -> None:
    path = tmp_path / "lock.db"
    holder = open_test_store(path)
    waiter = open_test_store(path, busy_timeout_ms=50)
    with holder.transaction(), pytest.raises(StoreError, match="locked"), waiter.transaction():
        pass


def test_beginning_a_transaction_that_cannot_start_is_a_store_error(store: Store) -> None:
    store.connection.execute("BEGIN")
    with pytest.raises(StoreError, match="transaction failed"), store.transaction():
        pass
    store.connection.execute("ROLLBACK")


# ── a consistent view for several reads ──


def test_reads_inside_read_share_one_snapshot(
    open_test_store: Callable[..., Store], tmp_path: Path
) -> None:
    path = tmp_path / "view.db"
    reader = open_test_store(path)
    writer = open_test_store(path)
    _scratch(writer)
    with reader.read():
        before = reader.execute("SELECT COUNT(*) FROM scratch").fetchone()[0]
        writer.execute("INSERT INTO scratch (label) VALUES ('later')")
        assert reader.execute("SELECT COUNT(*) FROM scratch").fetchone()[0] == before
    assert reader.execute("SELECT COUNT(*) FROM scratch").fetchone()[0] == before + 1


def test_read_joins_an_open_transaction(store: Store) -> None:
    _scratch(store)
    with store.transaction():
        store.execute("INSERT INTO scratch (label) VALUES ('mine')")
        with store.read():
            assert _labels(store) == ["mine"]
        assert store.connection.in_transaction  # read() did not end the caller's transaction
    assert _labels(store) == ["mine"]


# ── concurrent first start ──


def test_two_processes_starting_on_an_empty_database_migrate_it_once(tmp_path: Path) -> None:
    from obliskiq.store.migrations import migrate

    path = tmp_path / "race.db"
    barrier = threading.Barrier(2)
    results: list[list[str]] = []
    errors: list[BaseException] = []

    def start() -> None:
        connection = sqlite3.connect(path, autocommit=True)
        try:
            connection.execute("PRAGMA busy_timeout = 10000")
            barrier.wait()
            results.append(migrate(connection))
        except BaseException as error:  # reported to the main thread below
            errors.append(error)
        finally:
            connection.close()

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    # Between them the two apply every migration exactly once.
    names = sorted(name for applied in results for name in applied)
    assert names == sorted(path.name for path in available_migrations())
    check = sqlite3.connect(path)
    try:
        applied_rows = check.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        assert applied_rows == len(available_migrations())
        assert check.execute("SELECT COUNT(*) FROM instruments").fetchone()[0] == 0
    finally:
        check.close()


# ── a body that ends the transaction itself, and a rollback that cannot happen ──


def test_a_body_that_commits_for_itself_is_reported(store: Store) -> None:
    def commit_early() -> None:
        store.execute("COMMIT")

    _scratch(store)
    with pytest.raises(StoreError, match="transaction failed"):
        _write_then(store, "committed early", commit_early)
    assert not store.connection.in_transaction


def test_a_failed_rollback_does_not_mask_the_original_error(store: Store) -> None:
    def release_the_savepoint_then_fail() -> None:
        with store.transaction():
            store.execute("RELEASE oblisk_sp_1")  # the level's own savepoint is gone
            _boom()

    _scratch(store)
    with pytest.raises(RuntimeError, match="boom"):
        _write_then(store, "outer", release_the_savepoint_then_fail)


def test_a_read_view_that_was_ended_inside_is_not_ended_twice(store: Store) -> None:
    with store.read():
        store.execute("COMMIT")
    assert not store.connection.in_transaction


def test_a_read_that_cannot_begin_is_a_store_error() -> None:
    class Refusing:
        in_transaction = False

        def execute(self, sql: str) -> None:
            raise sqlite3.OperationalError("disk I/O error")

    refusing = Store(Refusing(), "stub")  # type: ignore[arg-type]
    with pytest.raises(StoreError, match="read failed"), refusing.read():
        pass
