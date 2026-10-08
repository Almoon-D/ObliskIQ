"""SQLite connection and migration runner.

The collectors write while the server reads, from separate processes, so the
database runs in WAL mode with a busy timeout. Both come from
``runtime.sqlite`` in config.

Migrations are plain ``.sql`` files applied in filename order and recorded in
``schema_migrations``. Forward-only: a migration that has run is never re-run and
never edited in place.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ..config import Config
from ..errors import StoreError
from ..logging_setup import get_logger

log = get_logger("store")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name        TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TEXT NOT NULL
)
"""


def available_migrations(directory: Path | None = None) -> list[Path]:
    """Migration files in apply order."""
    target = directory or MIGRATIONS_DIR
    if not target.is_dir():
        raise StoreError(f"migrations directory not found: {target}")
    return sorted(target.glob("*.sql"))


def applied_migrations(connection: sqlite3.Connection) -> dict[str, str]:
    """Name -> checksum for migrations already applied."""
    connection.execute(_MIGRATION_TABLE)
    rows = connection.execute("SELECT name, checksum FROM schema_migrations").fetchall()
    return {row[0]: row[1] for row in rows}


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Store:
    """A connection to the store, with migrations applied.

    Not thread-safe by itself: ``check_same_thread`` stays on, so open a Store per
    thread or per process. WAL makes concurrent readers and one writer safe at the
    file level, which is what the collector/server split needs.
    """

    def __init__(self, connection: sqlite3.Connection, path: Path) -> None:
        self._connection = connection
        self._path = path
        self._depth = 0

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    @property
    def path(self) -> Path:
        return self._path

    def execute(self, sql: str, parameters: Sequence[object] = ()) -> sqlite3.Cursor:
        return self._connection.execute(sql, parameters)

    def executemany(self, sql: str, seq: Sequence[Sequence[object]]) -> sqlite3.Cursor:
        return self._connection.executemany(sql, seq)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Commit on success, roll back on any exception. **Nests.**

        Nesting is the point. Two writes that must not be separable — a position
        and the premium it commits to the sleeve, a close and the penalty it
        settles — live in two different modules, each of which opens its own
        transaction. With a plain ``with connection:`` the inner block commits
        the outer one, so a failure between them leaves half the pair standing:
        a position consuming a concurrency slot with no commitment against it,
        or a closed position whose loss never reached the penalty balance.

        Every level opens a SAVEPOINT and the outermost commits. An inner
        failure rolls back to its own savepoint and re-raises, so the outer
        level rolls back too unless it deliberately swallows the error — which
        is then a decision somebody wrote, not an accident of nesting.
        """
        depth = self._depth
        name = f"oblisk_sp_{depth}"
        try:
            self._connection.execute(f"SAVEPOINT {name}")
        except sqlite3.Error as exc:
            raise StoreError(f"transaction failed: {exc}") from exc
        self._depth = depth + 1
        try:
            yield self._connection
        except BaseException as exc:
            self._depth = depth
            try:
                self._connection.execute(f"ROLLBACK TO {name}")
                self._connection.execute(f"RELEASE {name}")
            except sqlite3.Error:
                # The rollback itself failing must not mask what went wrong.
                pass
            if isinstance(exc, sqlite3.Error):
                raise StoreError(f"transaction failed: {exc}") from exc
            raise
        self._depth = depth
        try:
            # Releasing the outermost savepoint commits when SQLite started the
            # transaction; commit() covers the case where the driver had already
            # opened one before the savepoint was taken.
            self._connection.execute(f"RELEASE {name}")
            if depth == 0:
                self._connection.commit()
        except sqlite3.Error as exc:
            raise StoreError(f"transaction failed: {exc}") from exc

    def table_names(self) -> list[str]:
        rows = self._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        return [row[0] for row in rows]

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _apply_pragmas(connection: sqlite3.Connection, config: Config) -> None:
    sqlite_cfg = config.section("runtime.sqlite")
    journal_mode = sqlite_cfg.require_str("journal_mode")
    synchronous = sqlite_cfg.require_str("synchronous")
    busy_timeout_ms = sqlite_cfg.require_int("busy_timeout_ms")
    foreign_keys = sqlite_cfg.require_bool("foreign_keys")

    connection.execute(f"PRAGMA journal_mode = {journal_mode}")
    connection.execute(f"PRAGMA synchronous = {synchronous}")
    connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    connection.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")


def migrate(connection: sqlite3.Connection, directory: Path | None = None) -> list[str]:
    """Apply pending migrations. Returns the names applied by this call."""
    already = applied_migrations(connection)
    applied_now: list[str] = []

    for migration in available_migrations(directory):
        sql = migration.read_text(encoding="utf-8")
        checksum = _checksum(sql)
        if migration.name in already:
            if already[migration.name] != checksum:
                raise StoreError(
                    f"migration {migration.name} has changed since it was applied. "
                    "Migrations are forward-only; add a new file instead of editing one."
                )
            continue
        try:
            with connection:
                connection.executescript(sql)
                connection.execute(
                    "INSERT INTO schema_migrations (name, checksum, applied_at) VALUES (?, ?, ?)",
                    (
                        migration.name,
                        checksum,
                        datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    ),
                )
        except sqlite3.Error as exc:
            raise StoreError(f"migration {migration.name} failed: {exc}") from exc
        applied_now.append(migration.name)
        log.info("migration applied", extra={"migration": migration.name})

    return applied_now


def open_store(
    db_path: str | Path,
    config: Config,
    *,
    apply_migrations: bool = True,
    directory: Path | None = None,
) -> Store:
    """Open (creating if needed) the store at ``db_path`` and bring it up to schema."""
    path = Path(db_path).expanduser()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)

    try:
        connection = sqlite3.connect(str(path), isolation_level="DEFERRED")
    except sqlite3.Error as exc:
        raise StoreError(f"could not open the store at {path}: {exc}") from exc

    connection.row_factory = sqlite3.Row
    _apply_pragmas(connection, config)

    if apply_migrations:
        applied = migrate(connection, directory)
        if applied:
            log.info("store migrated", extra={"db_path": str(path), "migrations": applied})

    return Store(connection, path)
