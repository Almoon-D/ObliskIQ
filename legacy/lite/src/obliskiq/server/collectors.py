"""``run_collectors`` — a manual trigger for the standalone collector entrypoint.

The collectors are a **separate process**, and this module keeps them one. They
hold IBKR credentials, a chain pull walks several hundred strikes under a pacing
guard, and CLAUDE.md requires them to be deployable to a small always-on VM with
no server and no human present. Importing them into the server would put
credentials in a process that binds a port, and would let one long pull occupy a
worker for an hour.

So the tool spawns ``obliskiq-collect <task>`` and reports what the child did,
read back out of ``collector_runs`` — the same run log the scheduled runs write,
so a manually triggered run is indistinguishable from a timed one afterwards.

Nothing here builds a shell command. The argv is a list, the task is checked
against ``server.collectors.allowed_tasks`` before anything is spawned, and the
extra arguments are drawn from a fixed set: a tool argument must never be able
to become a command.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..config import Config
from ..logging_setup import get_logger
from ..store.db import Store

log = get_logger("server.collectors")

#: Exit codes, shared with the collector and compute entrypoints.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_DEGRADED = 2

#: How the child's exit code reads.
OUTCOME_BY_CODE = {
    EXIT_OK: "ok",
    EXIT_FAILED: "failed",
    EXIT_DEGRADED: "degraded",
}

#: The tail of the child's stderr kept on the result. Enough to see why a run
#: failed, bounded so a chatty collector cannot flood a tool response.
LOG_TAIL_CHARS = 4000

#: Stripped from the child's environment. The collectors bind no port and have no
#: use for the server's bearer token; a credential is not handed to a process
#: that cannot need it, and on the VM the two live in separate env files for the
#: same reason.
SERVER_ONLY_PREFIX = "OBLISK_MCP_"


def child_environment(parent: Mapping[str, str]) -> dict[str, str]:
    """The environment the collector child gets: the parent's, less the token."""
    return {k: v for k, v in parent.items() if not k.startswith(SERVER_ONLY_PREFIX)}


class CollectorsDisabled(RuntimeError):
    """``server.collectors.enabled`` is false on this deployment."""


@dataclass(frozen=True)
class CollectorRunResult:
    """What the child process did."""

    task: str
    argv: tuple[str, ...]
    exit_code: int | None
    outcome: str
    started_at: str
    finished_at: str
    timed_out: bool
    stderr_tail: str
    runs: tuple[dict[str, object], ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.exit_code == EXIT_OK


class CollectorRunner:
    """Spawns the collector entrypoint, never imports it."""

    def __init__(self, config: Config) -> None:
        section = config.section("server.collectors")
        self._enabled = section.require_bool("enabled")
        self._executable = section.require_str("executable")
        self._timeout = section.require_float("timeout_seconds")
        self._allowed = tuple(str(task) for task in section.require_list("allowed_tasks"))

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def allowed_tasks(self) -> tuple[str, ...]:
        return self._allowed

    def resolve(self) -> str:
        """Where the collector entrypoint actually is, or a usable error."""
        found = shutil.which(self._executable)
        if found is None:
            raise FileNotFoundError(
                f"{self._executable!r} is not on PATH. The collectors are a separate "
                "entrypoint from this server; install the package in the environment the "
                "server runs in, or point server.collectors.executable at it."
            )
        return found

    def build_argv(
        self,
        task: str,
        *,
        only: Sequence[str] | None = None,
        skip: Sequence[str] | None = None,
        dry_run: bool = False,
        force: bool = False,
    ) -> list[str]:
        """The child's argv, as a list. No shell is involved at any point."""
        if not self._enabled:
            raise CollectorsDisabled(
                "server.collectors.enabled is false on this deployment. The collectors "
                "run on their own timer; nothing here can start them."
            )
        if task not in self._allowed:
            raise ValueError(
                f"task {task!r} is not in server.collectors.allowed_tasks {list(self._allowed)}"
            )
        argv = [self.resolve(), task]
        for symbol in only or ():
            cleaned = str(symbol).strip()
            if cleaned:
                argv.extend(["--only", cleaned])
        for step in skip or ():
            cleaned = str(step).strip()
            if cleaned:
                argv.extend(["--skip", cleaned])
        if dry_run:
            argv.append("--dry-run")
        if force:
            argv.append("--force")
        return argv

    async def run(
        self,
        task: str,
        *,
        only: Sequence[str] | None = None,
        skip: Sequence[str] | None = None,
        dry_run: bool = False,
        force: bool = False,
    ) -> CollectorRunResult:
        """Run one collector task to completion, or to the configured timeout."""
        argv = self.build_argv(task, only=only, skip=skip, dry_run=dry_run, force=force)
        started = _stamp()
        log.info("spawning the collector entrypoint", extra={"task": task})

        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_environment(os.environ),
        )
        timed_out = False
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=self._timeout)
        except TimeoutError:
            timed_out = True
            stderr = b""
            process.kill()
            await process.wait()
            log.warning(
                "the collector run exceeded server.collectors.timeout_seconds",
                extra={"task": task, "timeout_seconds": self._timeout},
            )

        exit_code = process.returncode
        outcome = (
            "timed_out"
            if timed_out
            else OUTCOME_BY_CODE.get(exit_code if exit_code is not None else EXIT_FAILED, "failed")
        )
        return CollectorRunResult(
            task=task,
            argv=tuple(argv),
            exit_code=exit_code,
            outcome=outcome,
            started_at=started,
            finished_at=_stamp(),
            timed_out=timed_out,
            stderr_tail=stderr.decode("utf-8", errors="replace")[-LOG_TAIL_CHARS:],
        )


def runs_since(store: Store, *, since: str, limit: int) -> list[dict[str, object]]:
    """Run-log rows written at or after ``since``.

    Read back from ``collector_runs`` rather than parsed out of the child's
    stdout: the run log is the record the scheduled runs write too, so a manual
    trigger leaves the same trace as a timed one.
    """
    rows = store.execute(
        "SELECT run_id, collector, started_at, finished_at, status, rows_written, "
        "targets_ok, targets_failed, detail FROM collector_runs "
        "WHERE started_at >= ? ORDER BY run_id DESC LIMIT ?",
        (since, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
