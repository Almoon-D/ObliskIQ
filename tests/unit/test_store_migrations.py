"""The migration runner, exercised over small chains written for each case.

The legacy chain test applied nineteen real migrations over a populated store. This chain is
fresh, so the cases that matter are the runner's own: what it refuses, what a failure leaves
behind, and that data survives the ways a later migration will change a table.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from obliskiq.errors import MigrationError
from obliskiq.store import migrations
from obliskiq.store.migrations import (
    MIGRATIONS_DIR,
    _apply,
    applied_migrations,
    available_migrations,
    checksum,
    migrate,
)

FIRST = "CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT NOT NULL);\n"


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(tmp_path / "chain.db", autocommit=True)
    conn.execute("PRAGMA busy_timeout = 100")
    yield conn
    conn.close()


@pytest.fixture
def chain(tmp_path: Path) -> Callable[..., Path]:
    """Write a chain of migration files into a fresh directory and return it."""

    def write(*scripts: str, names: tuple[str, ...] | None = None) -> Path:
        directory = tmp_path / "migrations"
        directory.mkdir(exist_ok=True)
        for old in directory.glob("*.sql"):
            old.unlink()
        for position, script in enumerate(scripts, start=1):
            name = names[position - 1] if names else f"{position:03d}_step.sql"
            (directory / name).write_text(script, encoding="utf-8", newline="")
        return directory

    return write


def _tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


# ── the shipped chain ──


def test_the_shipped_chain_starts_with_the_core_schema() -> None:
    names = [path.name for path in available_migrations()]
    assert names[0] == "001_core.sql"
    assert MIGRATIONS_DIR.is_dir()


def test_the_shipped_chain_applies_once_and_is_recorded(connection: sqlite3.Connection) -> None:
    applied = migrate(connection)
    assert applied == [path.name for path in available_migrations()]
    recorded = connection.execute(
        "SELECT name, checksum, applied_at FROM schema_migrations ORDER BY name"
    ).fetchall()
    assert [row[0] for row in recorded] == applied
    first = (MIGRATIONS_DIR / "001_core.sql").read_bytes()
    assert recorded[0][1] == hashlib.sha256(first.replace(b"\r\n", b"\n")).hexdigest()
    assert recorded[0][2].endswith("Z")
    assert len(recorded[0][2]) == len("2026-10-09T22:30:00Z")
    assert migrate(connection) == []
    assert applied_migrations(connection) == {row[0]: row[1] for row in recorded}


def test_the_checksum_ignores_line_endings() -> None:
    assert checksum("a\r\nb\r\n") == checksum("a\nb\n")
    assert checksum("a\nb\n") != checksum("a\nB\n")


# ── what the runner refuses ──


def test_an_edited_migration_is_refused(
    connection: sqlite3.Connection, chain: Callable[..., Path]
) -> None:
    directory = chain(FIRST)
    migrate(connection, directory)
    (directory / "001_step.sql").write_text(FIRST + "-- edited\n", encoding="utf-8")
    with pytest.raises(MigrationError, match=r"001_step.sql has changed since it was applied"):
        migrate(connection, directory)


def test_a_migration_missing_from_disk_is_refused(
    connection: sqlite3.Connection, chain: Callable[..., Path]
) -> None:
    directory = chain(FIRST, "CREATE TABLE child (id INTEGER PRIMARY KEY);\n")
    migrate(connection, directory)
    (directory / "002_step.sql").unlink()
    with pytest.raises(MigrationError, match=r"002_step.sql was applied but its file is missing"):
        migrate(connection, directory)


def test_a_migration_numbered_below_one_already_applied_is_refused(
    connection: sqlite3.Connection, chain: Callable[..., Path]
) -> None:
    directory = chain(FIRST, "CREATE TABLE b (i INTEGER);\n", "CREATE TABLE c (i INTEGER);\n")
    applied_migrations(connection)  # creates the history table
    for name in ("001_step.sql", "003_step.sql"):
        text = (directory / name).read_text(encoding="utf-8")
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?, ?, '2026-10-09T00:00:00Z')",
            (name, checksum(text)),
        )
    with pytest.raises(MigrationError, match=r"002_step.sql is not applied but 003_step.sql is"):
        migrate(connection, directory)


@pytest.mark.parametrize(
    ("names", "complaint"),
    [
        (("001_a.sql", "003_c.sql"), "without a gap"),
        (("002_a.sql", "003_b.sql"), "without a gap"),
        (("001_a.sql", "001_b.sql"), "without a gap"),
        (("001_a.sql", "02_b.sql"), "must be named"),
        (("001_A.sql", "002_b.sql"), "must be named"),
        (("001_a.sql", "002_b.sqlx"), None),
        (("first.sql",), "must be named"),
    ],
)
def test_the_numbering_and_names_must_be_regular(
    connection: sqlite3.Connection,
    chain: Callable[..., Path],
    names: tuple[str, ...],
    complaint: str | None,
) -> None:
    directory = chain(*(["SELECT 1;\n"] * len(names)), names=names)
    if complaint is None:  # a file that is not .sql is not a migration at all
        assert [path.name for path in available_migrations(directory)] == ["001_a.sql"]
        return
    with pytest.raises(MigrationError, match=complaint):
        migrate(connection, directory)


def test_a_final_statement_without_a_semicolon_is_still_checked(
    connection: sqlite3.Connection, chain: Callable[..., Path]
) -> None:
    with pytest.raises(MigrationError, match="transaction control"):
        migrate(connection, chain("CREATE TABLE t (i INTEGER);\nCOMMIT"))
    assert migrate(connection, chain("CREATE TABLE t (i INTEGER)")) == ["001_step.sql"]


def test_a_missing_directory_is_refused(connection: sqlite3.Connection, tmp_path: Path) -> None:
    with pytest.raises(MigrationError, match="directory not found"):
        migrate(connection, tmp_path / "nowhere")


@pytest.mark.parametrize(
    "script",
    [
        "CREATE TABLE t (i INTEGER);\nCOMMIT;\n",
        "BEGIN;\nCREATE TABLE t (i INTEGER);\n",
        "CREATE TABLE t (i INTEGER);\nROLLBACK;\n",
        "SAVEPOINT s;\nCREATE TABLE t (i INTEGER);\nRELEASE s;\n",
        "END TRANSACTION;\n",
        "commit;\n",
        "/* a comment first */ COMMIT;\n",
    ],
)
def test_a_script_that_controls_the_transaction_is_refused_before_it_runs(
    connection: sqlite3.Connection, chain: Callable[..., Path], script: str
) -> None:
    directory = chain(script)
    with pytest.raises(MigrationError, match="transaction control"):
        migrate(connection, directory)
    assert _tables(connection) <= {"schema_migrations"}


@pytest.mark.parametrize(
    "script",
    [
        "PRAGMA foreign_keys = OFF;\nCREATE TABLE t (i INTEGER);\n",
        "pragma  journal_mode = DELETE;\n",
        "PRAGMA synchronous = OFF;\n",
        "PRAGMA busy_timeout = 1;\n",
    ],
)
def test_a_script_that_changes_a_connection_setting_is_refused(
    connection: sqlite3.Connection, chain: Callable[..., Path], script: str
) -> None:
    with pytest.raises(MigrationError, match="connection pragma"):
        migrate(connection, chain(script))


def test_comments_and_trigger_bodies_are_not_transaction_control(
    connection: sqlite3.Connection, chain: Callable[..., Path]
) -> None:
    script = (
        "-- COMMIT the idea; BEGIN again\n"
        "/* ROLLBACK is only a word here */\n"
        "CREATE TABLE audit (id INTEGER PRIMARY KEY, note TEXT);\n"
        "CREATE TABLE thing (id INTEGER PRIMARY KEY);\n"
        "CREATE TRIGGER thing_audit AFTER INSERT ON thing\n"
        "BEGIN\n"
        "    INSERT INTO audit (note) VALUES ('added');\n"
        "END;\n"
        "INSERT INTO thing DEFAULT VALUES;\n"
    )
    assert migrate(connection, chain(script)) == ["001_step.sql"]
    assert connection.execute("SELECT note FROM audit").fetchone()[0] == "added"


def test_a_script_that_ends_the_transaction_anyway_is_caught_afterwards(
    connection: sqlite3.Connection, chain: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(migrations, "_refuse_unsafe", lambda *_: None)
    with pytest.raises(MigrationError, match="ended the transaction"):
        migrate(connection, chain("CREATE TABLE t (i INTEGER);\nCOMMIT;\n"))
    assert "schema_migrations" in _tables(connection)
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0


# ── failure leaves the store as it was ──


def test_a_failing_migration_leaves_nothing_of_itself_and_stops_the_chain(
    connection: sqlite3.Connection, chain: Callable[..., Path]
) -> None:
    directory = chain(
        FIRST,
        "CREATE TABLE half (id INTEGER PRIMARY KEY);\nINSERT INTO nonexistent VALUES (1);\n",
        "CREATE TABLE never (id INTEGER PRIMARY KEY);\n",
    )
    with pytest.raises(MigrationError, match=r"002_step.sql failed"):
        migrate(connection, directory)
    assert set(applied_migrations(connection)) == {"001_step.sql"}
    assert "half" not in _tables(connection)
    assert "never" not in _tables(connection)
    assert not connection.in_transaction


def test_a_failed_migration_that_is_then_fixed_applies_on_the_next_start(
    connection: sqlite3.Connection, chain: Callable[..., Path]
) -> None:
    directory = chain(FIRST, "INSERT INTO nonexistent VALUES (1);\n")
    with pytest.raises(MigrationError):
        migrate(connection, directory)
    (directory / "002_step.sql").write_text("CREATE TABLE fixed (id INTEGER);\n", encoding="utf-8")
    assert migrate(connection, directory) == ["002_step.sql"]
    assert "fixed" in _tables(connection)


def test_a_migration_that_breaks_a_foreign_key_is_rolled_back(
    connection: sqlite3.Connection, chain: Callable[..., Path]
) -> None:
    script = (
        "CREATE TABLE parent (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE child (id INTEGER PRIMARY KEY, parent INTEGER REFERENCES parent (id));\n"
        "INSERT INTO child (parent) VALUES (42);\n"
    )
    with pytest.raises(MigrationError, match="break a foreign key"):
        migrate(connection, chain(script))
    assert "child" not in _tables(connection)


def test_a_history_that_cannot_be_read_is_a_migration_error(
    connection: sqlite3.Connection, chain: Callable[..., Path]
) -> None:
    connection.close()
    with pytest.raises(MigrationError, match="could not read the migration history"):
        migrate(connection, chain(FIRST))


def test_a_database_locked_while_reading_the_history_is_a_migration_error(
    tmp_path: Path, connection: sqlite3.Connection, chain: Callable[..., Path]
) -> None:
    directory = chain(FIRST)
    holder = sqlite3.connect(tmp_path / "chain.db", autocommit=True)
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(MigrationError, match=r"could not read the migration history.*locked"):
            migrate(connection, directory)
    finally:
        holder.close()


def test_a_database_locked_when_a_migration_begins_is_a_migration_error(
    tmp_path: Path, connection: sqlite3.Connection
) -> None:
    applied_migrations(connection)  # the history table exists before the lock is taken
    holder = sqlite3.connect(tmp_path / "chain.db", autocommit=True)
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(MigrationError, match=r"001_step.sql failed.*locked"):
            _apply(connection, "001_step.sql", FIRST)
    finally:
        holder.close()
    assert not connection.in_transaction


def test_a_migration_applied_meanwhile_is_skipped_not_repeated(
    connection: sqlite3.Connection,
) -> None:
    applied_migrations(connection)
    connection.execute(
        "INSERT INTO schema_migrations VALUES ('001_step.sql', ?, '2026-10-09T00:00:00Z')",
        (checksum(FIRST),),
    )
    assert _apply(connection, "001_step.sql", FIRST) is False
    assert "parent" not in _tables(connection)
    assert not connection.in_transaction


def test_a_migration_another_process_applied_meanwhile_is_not_reported_as_ours(
    connection: sqlite3.Connection, chain: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = chain(FIRST)
    assert migrate(connection, directory) == ["001_step.sql"]
    monkeypatch.setattr(migrations, "applied_migrations", lambda _: {})  # a stale view
    assert migrate(connection, directory) == []


def test_a_migration_changed_meanwhile_is_refused(connection: sqlite3.Connection) -> None:
    applied_migrations(connection)
    connection.execute(
        "INSERT INTO schema_migrations VALUES ('001_step.sql', 'deadbeef', '2026-10-09T00:00:00Z')"
    )
    with pytest.raises(MigrationError, match="has changed since it was applied"):
        _apply(connection, "001_step.sql", FIRST)
    assert not connection.in_transaction


# ── data survives what later migrations will do ──


def test_rows_survive_an_added_column_and_a_table_rebuild(
    connection: sqlite3.Connection, chain: Callable[..., Path]
) -> None:
    directory = chain(
        "CREATE TABLE note (id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL);\n"
        "INSERT INTO note (body) VALUES ('one'), ('two');\n",
        "ALTER TABLE note ADD COLUMN tag TEXT;\n",
        "ALTER TABLE note RENAME TO note_old;\n"
        "CREATE TABLE note (id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL,\n"
        "                   tag TEXT CHECK (tag IS NULL OR tag <> ''));\n"
        "INSERT INTO note (id, body, tag) SELECT id, body, tag FROM note_old;\n"
        "DROP TABLE note_old;\n",
    )
    migrate(connection, directory)
    rows = connection.execute("SELECT id, body, tag FROM note ORDER BY id").fetchall()
    assert rows == [(1, "one", None), (2, "two", None)]
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO note (body, tag) VALUES ('three', '')")


PARENT_AND_CHILD = (
    "CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT NOT NULL);\n"
    "CREATE TABLE child (id INTEGER PRIMARY KEY, parent INTEGER REFERENCES parent (id));\n"
    "INSERT INTO parent VALUES (1, 'p');\n"
    "INSERT INTO child VALUES (1, 1);\n"
)


def test_a_referenced_table_can_be_rebuilt_and_foreign_keys_come_back_on(
    connection: sqlite3.Connection, chain: Callable[..., Path]
) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    directory = chain(
        PARENT_AND_CHILD,
        "CREATE TABLE parent_new (id INTEGER PRIMARY KEY, name TEXT NOT NULL, extra TEXT);\n"
        "INSERT INTO parent_new (id, name) SELECT id, name FROM parent;\n"
        "DROP TABLE parent;\n"
        "ALTER TABLE parent_new RENAME TO parent;\n",
    )
    assert migrate(connection, directory) == ["001_step.sql", "002_step.sql"]
    assert connection.execute("SELECT id, name, extra FROM parent").fetchall() == [(1, "p", None)]
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO child VALUES (2, 99)")


def test_a_rebuild_that_loses_a_parent_row_is_rolled_back(
    connection: sqlite3.Connection, chain: Callable[..., Path]
) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    directory = chain(
        PARENT_AND_CHILD,
        "CREATE TABLE parent_new (id INTEGER PRIMARY KEY, name TEXT NOT NULL);\n"
        "DROP TABLE parent;\n"
        "ALTER TABLE parent_new RENAME TO parent;\n",
    )
    with pytest.raises(MigrationError, match="break a foreign key"):
        migrate(connection, directory)
    assert connection.execute("SELECT id, name FROM parent").fetchall() == [(1, "p")]
    assert set(applied_migrations(connection)) == {"001_step.sql"}
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_rows_in_the_shipped_schema_survive_a_later_migration(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    """Re-run the shipped chain with one more migration beside it, over a populated store."""
    directory = tmp_path / "extended"
    directory.mkdir()
    (directory / "001_core.sql").write_bytes((MIGRATIONS_DIR / "001_core.sql").read_bytes())
    migrate(connection, directory)
    connection.execute(
        "INSERT INTO macro VALUES ('CPI', 'fred', '2026-07-01', '2026-08-12', 1.5, 'monthly', "
        "'index', '2026-10-09T22:30:00Z')"
    )
    (directory / "002_macro_note.sql").write_text(
        "ALTER TABLE macro ADD COLUMN note TEXT;\n", encoding="utf-8"
    )
    assert migrate(connection, directory) == ["002_macro_note.sql"]
    assert connection.execute("SELECT value, note FROM macro").fetchall() == [(1.5, None)]
