"""The SQLite connection: write-ahead logging, foreign keys, a busy timeout, transactions.

Several processes may touch one database file (a collector writing while the cockpit reads),
so it runs in write-ahead-logging mode with a busy timeout. A connection is opened with
``autocommit=True``: Python never starts a transaction behind the caller's back, so every
transaction is one this module began and one it ends. A ``Store`` belongs to the thread that
opened it (``check_same_thread`` stays on); async callers run its methods through
``asyncio.to_thread`` and keep to one thread per store.

Ported from ObliskIQ-lite-MCP's ``store/db.py``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from ..config import ObliskConfig
from ..errors import StoreError
from ..logging_setup import get_logger
from .migrations import migrate

log = get_logger("store")

#: The database path that is not a file; write-ahead logging does not apply to it.
MEMORY = ":memory:"

#: The durability levels the store accepts. SQLite ignores a level it does not know instead of
#: refusing it, so an unknown one is refused here. ``OFF`` is not offered: a crash could corrupt
#: the database.
_SYNCHRONOUS_LEVELS = ("NORMAL", "FULL", "EXTRA")


class Store:
    """An open database whose schema is current.

    ``transaction()`` is the only way to write atomically: ``BEGIN IMMEDIATE`` at the outermost
    level, so the write lock is taken up front rather than upgraded half way through, and a
    savepoint for each level inside. There is no ``commit()``: with autocommit on it would be a
    no-op, and a method that looks like it commits and does not is worse than none.
    """

    def __init__(self, connection: sqlite3.Connection, path: str) -> None:
        self._connection = connection
        self._path = path
        self._depth = 0

    @property
    def path(self) -> str:
        return self._path

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def execute(self, sql: str, parameters: Sequence[object] = ()) -> sqlite3.Cursor:
        return self._connection.execute(sql, parameters)

    def executemany(self, sql: str, rows: Sequence[Sequence[object]]) -> sqlite3.Cursor:
        return self._connection.executemany(sql, rows)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Commit on success, roll back on any exception. Nests.

        Nesting is the point. Two writes that must not be separable live in two modules, each
        opening its own transaction. The outermost level begins and commits; each level inside
        takes a savepoint, so an inner failure rolls back to its own savepoint and re-raises,
        and the outer level rolls back too unless it deliberately swallows the error, which is
        then a decision somebody wrote and not an accident of nesting.
        """
        depth = self._depth
        savepoint = f"oblisk_sp_{depth}"
        self._begin(depth, savepoint)
        self._depth = depth + 1
        try:
            yield self._connection
        except BaseException as exc:
            self._depth = depth
            self._rollback(depth, savepoint)
            if isinstance(exc, sqlite3.Error):
                raise StoreError(f"transaction failed: {exc}") from exc
            raise
        self._depth = depth
        try:
            self._connection.execute("COMMIT" if depth == 0 else f"RELEASE {savepoint}")
        except sqlite3.Error as exc:
            self._rollback(depth, savepoint)
            raise StoreError(f"transaction failed: {exc}") from exc

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """One consistent view for several reads.

        Inside a transaction the view is the transaction's. Outside, a deferred transaction
        gives the reads a single snapshot, so two statements cannot straddle another process's
        write.
        """
        if self._open_transaction():
            yield self._connection
            return
        try:
            self._connection.execute("BEGIN")
        except sqlite3.Error as exc:
            raise StoreError(f"read failed: {exc}") from exc
        try:
            yield self._connection
        finally:
            if self._open_transaction():
                self._connection.execute("ROLLBACK")  # nothing was written; this ends the view

    def _open_transaction(self) -> bool:
        return self._connection.in_transaction

    def _begin(self, depth: int, savepoint: str) -> None:
        try:
            self._connection.execute("BEGIN IMMEDIATE" if depth == 0 else f"SAVEPOINT {savepoint}")
        except sqlite3.Error as exc:
            raise StoreError(f"transaction failed: {exc}") from exc

    def _rollback(self, depth: int, savepoint: str) -> None:
        try:
            if depth == 0:
                if self._open_transaction():
                    self._connection.execute("ROLLBACK")
            else:
                self._connection.execute(f"ROLLBACK TO {savepoint}")
                self._connection.execute(f"RELEASE {savepoint}")
        except sqlite3.Error:
            pass  # the rollback failing must not mask what went wrong

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


def open_database(
    path: str | Path,
    *,
    busy_timeout_ms: int,
    synchronous: str,
    migrations_directory: Path | None = None,
) -> Store:
    """Open (creating if needed) the database at ``path`` and bring it to the current schema.

    Refuses to run without write-ahead logging (a file database) or foreign keys: either
    would be silently weaker than the store's guarantees assume.
    """
    target = str(path) if str(path) == MEMORY else str(Path(path).expanduser())
    if target != MEMORY:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    try:
        connection = sqlite3.connect(target, autocommit=True)
    except sqlite3.Error as exc:
        raise StoreError(f"could not open the store at {target}: {exc}") from exc
    try:
        connection.row_factory = sqlite3.Row
        _apply_pragmas(connection, target, busy_timeout_ms, synchronous)
        applied = migrate(connection, migrations_directory)
    except BaseException:
        connection.close()
        raise
    if applied:
        log.info("store migrated", extra={"db_path": target, "migrations": applied})
    return Store(connection, target)


def open_store(
    settings: ObliskConfig, data_dir: Path, *, migrations_directory: Path | None = None
) -> Store:
    """The configured store: ``paths.database`` inside the data directory, ``store`` pragmas."""
    store_settings = settings.store
    return open_database(
        data_dir / settings.paths.database,
        busy_timeout_ms=store_settings.busy_timeout_ms,
        synchronous=store_settings.synchronous,
        migrations_directory=migrations_directory,
    )


def _apply_pragmas(
    connection: sqlite3.Connection, target: str, busy_timeout_ms: int, synchronous: str
) -> None:
    try:
        mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if target != MEMORY and str(mode).lower() != "wal":
            raise StoreError(
                f"the store at {target} would not enter write-ahead logging (got {mode!r}); "
                "refusing to run without it"
            )
        if synchronous not in _SYNCHRONOUS_LEVELS:
            raise StoreError(
                f"synchronous must be one of {', '.join(_SYNCHRONOUS_LEVELS)}, got {synchronous!r}"
            )
        connection.execute(f"PRAGMA synchronous = {synchronous}")
        if busy_timeout_ms <= 0:
            raise StoreError(f"busy_timeout_ms must be positive, got {busy_timeout_ms}")
        connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise StoreError("SQLite would not enable foreign keys; refusing to run without them")
    except sqlite3.Error as exc:
        raise StoreError(f"could not configure the store at {target}: {exc}") from exc
