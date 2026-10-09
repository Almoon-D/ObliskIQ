"""Forward-only, checksummed migrations.

A migration is a numbered ``.sql`` file (``001_core.sql``) applied once, in order, and recorded
in ``schema_migrations`` with the SHA-256 of its text. The runner refuses to go on when the
history it finds is not the history it was given:

* an applied migration whose file changed (a migration is never edited; add a new one);
* an applied migration whose file is gone;
* an unapplied migration numbered below one already applied (it would run out of order);
* a gap or a repeat in the numbering;
* a script that ends its own transaction or changes ``foreign_keys`` (neither can be undone
  by the runner's rollback, so a half-applied migration could be left behind).

Each migration runs inside one ``BEGIN IMMEDIATE`` transaction together with its
``schema_migrations`` row, so it is applied whole or not at all, and the applied set is read
again inside the write lock: two processes starting on an empty database cannot both apply
the first migration.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import hashlib
import re
import sqlite3
from pathlib import Path

from ..errors import MigrationError
from ..logging_setup import get_logger

log = get_logger("store")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_NAME_RE = re.compile(r"(\d{3})_[a-z0-9_]+\.sql")
_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_TRANSACTION_WORDS = frozenset({"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"})
_PRAGMAS_THAT_NEED_NO_TRANSACTION = re.compile(
    r"PRAGMA\s+(foreign_keys|journal_mode|synchronous|busy_timeout)\b", re.IGNORECASE
)

_MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name        TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TEXT NOT NULL
)
"""


def available_migrations(directory: Path | None = None) -> list[Path]:
    """The migration files in apply order. Names must run 001, 002, ... without a gap."""
    target = directory or MIGRATIONS_DIR
    if not target.is_dir():
        raise MigrationError(f"migrations directory not found: {target}")
    files = sorted(target.glob("*.sql"))
    for position, path in enumerate(files, start=1):
        match = _NAME_RE.fullmatch(path.name)
        if match is None:
            raise MigrationError(
                f"migration file {path.name!r} must be named NNN_lower_case_words.sql"
            )
        if int(match.group(1)) != position:
            raise MigrationError(
                f"migration numbering must run 001, 002, ... without a gap or a repeat; "
                f"found {path.name} at position {position}"
            )
    return files


def checksum(text: str) -> str:
    """SHA-256 of the text with line endings normalised, so a checkout on any system agrees."""
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def applied_migrations(connection: sqlite3.Connection) -> dict[str, str]:
    """Name to checksum of every migration already applied."""
    connection.execute(_MIGRATION_TABLE)
    rows = connection.execute("SELECT name, checksum FROM schema_migrations").fetchall()
    return {row[0]: row[1] for row in rows}


def _statements(script: str) -> list[str]:
    """The script split into complete statements (a trigger body keeps its inner semicolons)."""
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer)
            buffer = ""
    if buffer.strip():
        statements.append(buffer)
    return statements


def _refuse_unsafe(name: str, script: str) -> None:
    bare = _COMMENT_RE.sub(" ", script)
    for statement in _statements(bare):
        words = statement.split(None, 1)
        if words and words[0].upper().rstrip(";") in _TRANSACTION_WORDS:
            raise MigrationError(
                f"migration {name} contains transaction control ({words[0].upper()}); the runner "
                "owns the transaction"
            )
    if _PRAGMAS_THAT_NEED_NO_TRANSACTION.search(bare):
        raise MigrationError(
            f"migration {name} sets a connection pragma; connection settings belong to the store"
        )


def _verify_history(files: list[Path], applied: dict[str, str], texts: dict[str, str]) -> None:
    on_disk = {path.name for path in files}
    missing = sorted(set(applied) - on_disk)
    if missing:
        raise MigrationError(
            f"migration {missing[0]} was applied but its file is missing; "
            "the history on disk is not the history in the database"
        )
    for name, recorded in applied.items():
        if recorded != checksum(texts[name]):
            raise MigrationError(
                f"migration {name} has changed since it was applied. "
                "Migrations are forward-only; add a new file instead of editing one."
            )
    if applied:
        newest = max(applied)
        late = sorted(name for name in on_disk - set(applied) if name < newest)
        if late:
            raise MigrationError(
                f"migration {late[0]} is not applied but {newest} is; "
                "migrations cannot be applied out of order"
            )


def migrate(connection: sqlite3.Connection, directory: Path | None = None) -> list[str]:
    """Apply pending migrations. Returns the names applied by this call.

    The connection must be in autocommit mode (``sqlite3.connect(..., autocommit=True)``): the
    runner begins and ends each transaction itself.
    """
    files = available_migrations(directory)
    texts = {path.name: path.read_text(encoding="utf-8") for path in files}
    try:
        applied = applied_migrations(connection)
    except sqlite3.Error as exc:
        raise MigrationError(f"could not read the migration history: {exc}") from exc
    _verify_history(files, applied, texts)

    done: list[str] = []
    for path in files:
        if path.name in applied:
            continue
        if _apply(connection, path.name, texts[path.name]):
            done.append(path.name)
            log.info("migration applied", extra={"migration": path.name})
    return done


def _apply(connection: sqlite3.Connection, name: str, script: str) -> bool:
    """Apply one migration inside its own transaction; ``False`` when it was already applied.

    Foreign keys are switched off for the duration, as SQLite's own procedure for rebuilding a
    table requires (the switch has no effect inside a transaction, so it is made before the
    transaction begins). The cost is checked instead of waived: ``PRAGMA foreign_key_check``
    runs before the commit and any violation rolls the migration back.
    """
    _refuse_unsafe(name, script)
    digest = checksum(script)
    foreign_keys_were_on = bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT checksum FROM schema_migrations WHERE name = ?", (name,)
            ).fetchone()
            if row is not None:  # another process applied it while this one waited for the lock
                if row[0] != digest:
                    raise MigrationError(f"migration {name} has changed since it was applied")
                connection.execute("ROLLBACK")
                return False
            connection.executescript(script)
            if not connection.in_transaction:
                raise MigrationError(f"migration {name} ended the transaction the runner began")
            connection.execute(
                "INSERT INTO schema_migrations (name, checksum, applied_at) "
                "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))",
                (name, digest),
            )
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise MigrationError(f"migration {name} left rows that break a foreign key")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
    except sqlite3.Error as exc:
        raise MigrationError(f"migration {name} failed: {exc}") from exc
    finally:
        if foreign_keys_were_on:
            connection.execute("PRAGMA foreign_keys = ON")
    return True
