"""The run log: ``runs`` (one row per job the service runs) and ``run_steps`` (one per step).

Ported in shape from lite's ``collector_sequences`` and ``collector_runs`` (001_core): a run's row
goes in as ``running`` with ``steps_planned`` **before** any step runs, and each step's row as
``running`` before the step works, so a process killed mid-step leaves evidence of what it meant
to do. :func:`reap_running` closes what a dead process left open as ``interrupted``; the service
calls it when it takes the service lock, the moment no other process can still be writing them.

These are the system's own records, read whole like the order path's state (DECISIONS D46); the
status reads take a window, not an ``as_of``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .db import Store
from .mappers import date_text, read_date, read_optional_stamp, read_stamp, stamp_text

#: The statuses a run or step may hold (001_core's CHECK).
RUNNING = "running"
INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class RunRow:
    """One run as the log holds it."""

    run_id: str
    command: str
    session_date: date
    started_at: datetime
    finished_at: datetime | None
    status: str
    steps_planned: int
    steps_completed: int
    exit_code: int | None
    detail: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StepRow:
    """One step of a run."""

    run_id: str | None
    step_index: int | None
    name: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    detail: dict[str, Any]


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _detail(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except TypeError, ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def begin_run(
    store: Store,
    *,
    run_id: str,
    command: str,
    session_date: date,
    started_at: datetime,
    steps: tuple[str, ...],
) -> None:
    """The run's row, written before any step runs."""
    with store.transaction():
        store.execute(
            "INSERT INTO runs (run_id, command, session_date, started_at, status, steps_planned, "
            "steps_completed, detail) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            (
                run_id,
                command,
                date_text(session_date),
                stamp_text(started_at),
                RUNNING,
                len(steps),
                _json({"planned": list(steps), "steps": []}),
            ),
        )


def begin_step(
    store: Store, *, run_id: str, step_index: int, name: str, started_at: datetime
) -> int:
    """A step's row, written before the step works; returns its id."""
    with store.transaction():
        cursor = store.execute(
            "INSERT INTO run_steps (run_id, step_index, collector, started_at, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, step_index, name, stamp_text(started_at), RUNNING),
        )
    return int(cursor.lastrowid or 0)


def finish_step(
    store: Store,
    *,
    step_id: int,
    run_id: str,
    finished_at: datetime,
    status: str,
    detail: dict[str, Any],
    steps_completed: int,
    run_detail: dict[str, Any],
) -> None:
    """A step's verdict, and the run's running count, in one transaction."""
    with store.transaction():
        store.execute(
            "UPDATE run_steps SET finished_at = ?, status = ?, detail = ? WHERE step_id = ?",
            (stamp_text(finished_at), status, _json(detail), step_id),
        )
        store.execute(
            "UPDATE runs SET steps_completed = ?, detail = ? WHERE run_id = ?",
            (steps_completed, _json(run_detail), run_id),
        )


def finish_run(
    store: Store,
    *,
    run_id: str,
    finished_at: datetime,
    status: str,
    exit_code: int,
    steps_completed: int,
    detail: dict[str, Any],
) -> None:
    with store.transaction():
        store.execute(
            "UPDATE runs SET finished_at = ?, status = ?, exit_code = ?, steps_completed = ?, "
            "detail = ? WHERE run_id = ?",
            (stamp_text(finished_at), status, exit_code, steps_completed, _json(detail), run_id),
        )


def reap_running(store: Store, *, at: datetime, detail: str) -> int:
    """Close every run and step still ``running`` as ``interrupted``. Returns how many runs.

    Only for a caller that knows no live process can be writing them (the service lock).
    """
    stamp = stamp_text(at)
    reason = _json({"error": detail})
    with store.transaction():
        cursor = store.execute(
            "UPDATE runs SET status = ?, finished_at = max(?, started_at), exit_code = NULL, "
            "detail = json_set(coalesce(detail, '{}'), '$.interrupted', ?) WHERE status = ?",
            (INTERRUPTED, stamp, detail, RUNNING),
        )
        store.execute(
            "UPDATE run_steps SET status = ?, finished_at = max(?, started_at), detail = ? "
            "WHERE status = ?",
            (INTERRUPTED, stamp, reason, RUNNING),
        )
    return cursor.rowcount


def _run_from_row(row: Any) -> RunRow:
    return RunRow(
        run_id=row["run_id"],
        command=row["command"],
        session_date=read_date(row["session_date"], "runs", "session_date"),
        started_at=read_stamp(row["started_at"], "runs", "started_at"),
        finished_at=read_optional_stamp(row["finished_at"], "runs", "finished_at"),
        status=row["status"],
        steps_planned=int(row["steps_planned"]),
        steps_completed=int(row["steps_completed"]),
        exit_code=row["exit_code"],
        detail=_detail(row["detail"]),
    )


_RUN_COLUMNS = (
    "run_id, command, session_date, started_at, finished_at, status, steps_planned, "
    "steps_completed, exit_code, detail"
)


def runs_for(store: Store, *, command: str, session_date: date) -> list[RunRow]:
    """Every run of ``command`` for one session date, oldest first."""
    rows = store.execute(
        f"SELECT {_RUN_COLUMNS} FROM runs WHERE command = ? AND session_date = ? "
        "ORDER BY started_at, run_id",
        (command, date_text(session_date)),
    ).fetchall()
    return [_run_from_row(row) for row in rows]


def runs_since(store: Store, *, since: date) -> list[RunRow]:
    """Every run whose session date is ``since`` or later, most recent first."""
    rows = store.execute(
        f"SELECT {_RUN_COLUMNS} FROM runs WHERE session_date >= ? "
        "ORDER BY started_at DESC, run_id DESC",
        (date_text(since),),
    ).fetchall()
    return [_run_from_row(row) for row in rows]


def steps_of(store: Store, run_id: str) -> list[StepRow]:
    rows = store.execute(
        "SELECT run_id, step_index, collector, started_at, finished_at, status, detail "
        "FROM run_steps WHERE run_id = ? ORDER BY step_index",
        (run_id,),
    ).fetchall()
    return [
        StepRow(
            run_id=row["run_id"],
            step_index=row["step_index"],
            name=row["collector"],
            started_at=read_stamp(row["started_at"], "run_steps", "started_at"),
            finished_at=read_optional_stamp(row["finished_at"], "run_steps", "finished_at"),
            status=row["status"],
            detail=_detail(row["detail"]),
        )
        for row in rows
    ]
