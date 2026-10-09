"""Process-wide server state, and the seam that keeps SQLite safe.

``store.db.Store`` is explicitly not thread-safe — ``check_same_thread`` stays
on — so a connection cannot be shared across the worker threads a tool call may
land on. The rule here: open one migrating store at startup so a broken schema
fails the process rather than the first request, then hand every tool call its
own short-lived connection. WAL, already set by ``runtime.sqlite.journal_mode``,
is what makes that safe against the collectors writing at the same time.

Tool handlers are ``async`` and push their blocking work through
:func:`run_blocking`, so a slow read never occupies the event loop and thread
affinity never becomes a question anybody has to reason about.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import TypeVar

import anyio.to_thread

from ..config import Config, load_config
from ..env import Settings, load_settings
from ..logging_setup import get_logger
from ..store import Store, open_store
from ..util.timeutil import parse_date, utc_today

log = get_logger("server.context")

T = TypeVar("T")


@dataclass(frozen=True)
class ServerContext:
    """Everything a tool needs that outlives a single call."""

    settings: Settings
    config: Config

    @classmethod
    def load(cls, *, config_path: str | None = None) -> ServerContext:
        """Resolve the environment and config, then bring the schema up.

        Migrating at startup is deliberate: a store that cannot migrate should
        stop the server, not surface as a puzzling failure on whichever tool the
        session happens to call first.
        """
        settings = load_settings()
        config = load_config(config_path or settings.config_path)
        context = cls(settings=settings, config=config)
        with open_store(settings.db_path, config) as store:
            log.info(
                "store ready",
                extra={"db_path": str(store.path), "tables": len(store.table_names())},
            )
        return context

    @contextmanager
    def store(self) -> Iterator[Store]:
        """A connection for the life of one tool call, and no longer.

        Migrations are not re-applied: startup already did that, and re-running
        the check on every call would serialise readers behind a write lock for
        no benefit.
        """
        with open_store(self.settings.db_path, self.config, apply_migrations=False) as store:
            yield store

    def as_of(self, raw: str | None) -> date:
        """The session date. Defaults to today in UTC, as the CLIs do."""
        if not raw:
            return utc_today()
        parsed = parse_date(raw)
        if parsed is None:
            raise ValueError(f"as_of {raw!r} is not a date in YYYY-MM-DD form")
        return parsed

    @property
    def max_rows(self) -> int:
        return self.config.require_int("server.max_rows_per_response")

    @property
    def stale_after_days(self) -> int:
        return self.config.require_int("server.stale_after_days")

    def bounded(self, limit: int | None) -> int:
        """A caller's row limit, clamped to ``server.max_rows_per_response``.

        No tool returns an unbounded table: a truncated answer that says it was
        truncated beats a complete one nobody reads.
        """
        ceiling = self.max_rows
        if limit is None or limit <= 0:
            return ceiling
        return min(limit, ceiling)


async def run_blocking(fn: Callable[[], T]) -> T:
    """Run synchronous store work off the event loop."""
    return await anyio.to_thread.run_sync(fn)
