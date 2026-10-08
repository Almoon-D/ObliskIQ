"""The process was killed mid-run. What does the store say the next morning?

An unattended collector on a small VM is killed for reasons nobody records: the
machine reboots, the timer's ``TimeoutStartSec`` fires, the OOM killer picks it.
``collector_runs`` was written to leave evidence of that — the row goes in as
``running`` before any work starts — but nothing ever reclaimed it, so a run that
died last Tuesday and a run happening right now were the same row.

Worse, ``daily`` wrote five unrelated rows with nothing tying them together
and nothing saying how many steps were meant to happen. A day half collected was
indistinguishable, from the store alone, from a day collected in full.

Two things are checked here:

* a ``running`` row older than ``collectors.run_stale_after_seconds`` is reaped
  to ``interrupted`` by the next run, and one inside the window is left alone;
* one ``collector_sequences`` row per invocation carries the session, the steps
  planned, the steps that reported, the aggregate status and the exit code.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from obliskiq.collect.runlog import CollectorRun, RunStatus, reap_interrupted_runs
from obliskiq.collect.sequence import SequenceRun
from obliskiq.errors import StoreError
from obliskiq.util.timeutil import utc_stamp

SESSION = "2026-08-24"


def _abandoned_run(store, collector, *, age_seconds):
    """A row exactly as a killed process leaves it: 'running', never finished."""
    started = datetime.now(UTC) - timedelta(seconds=age_seconds)
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO collector_runs (collector, started_at, status) VALUES (?, ?, ?)",
            (collector, utc_stamp(started), RunStatus.RUNNING),
        )
    return store.execute("SELECT MAX(run_id) FROM collector_runs").fetchone()[0]


def _status_of(store, run_id):
    return store.execute(
        "SELECT status FROM collector_runs WHERE run_id = ?", (run_id,)
    ).fetchone()["status"]


# ── the reaper ──


def test_interrupted_is_a_status_the_schema_admits(store):
    """Its own value, not 'error'. A run that failed reported a verdict; this one
    never got the chance, and conflating them loses the difference."""
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO collector_runs (collector, started_at, status) VALUES (?, ?, ?)",
            ("chain", "2026-08-24T22:30:00Z", RunStatus.INTERRUPTED),
        )
    row = store.execute("SELECT status FROM collector_runs ORDER BY run_id DESC").fetchone()
    assert row["status"] == RunStatus.INTERRUPTED


@pytest.mark.parametrize("table", ["collector_runs", "collector_sequences"])
@pytest.mark.parametrize("status", RunStatus.ALL)
def test_the_store_accepts_every_status_the_code_can_produce(store, table, status):
    """Both directions of the same agreement.

    RunStatus is a Python tuple and the CHECK constraint is the real authority,
    and they are written in different files by different migrations. A value the
    code can emit but the store rejects surfaces as a failed write on the way OUT
    of a run — after the work is done, at 22:30, with nobody watching. That is
    how the `skipped` status was found.
    """
    columns = (
        "(sequence_id, command, session_date, started_at, status, steps_planned) "
        "VALUES ('s', 'daily', '2026-08-24', '2026-08-24T22:30:00Z', ?, 1)"
        if table == "collector_sequences"
        else "(collector, started_at, status) VALUES ('chain', '2026-08-24T22:30:00Z', ?)"
    )
    with store.transaction() as connection:
        connection.execute(f"INSERT INTO {table} {columns}", (status,))


@pytest.mark.parametrize("table", ["collector_runs", "collector_sequences"])
def test_the_store_rejects_a_status_the_code_cannot_produce(store, table):
    """The other direction: the CHECK is not merely permissive."""
    ddl = store.execute(
        f"SELECT sql FROM sqlite_master WHERE type = 'table' AND name = '{table}'"
    ).fetchone()["sql"]
    # The status CHECK alone. Scanning the whole DDL would pick up every other
    # quoted literal in it — 'daily' from a column comment, say — and call them
    # statuses.
    clause = re.search(r"status\s+TEXT\s+NOT NULL\s+CHECK\s*\(status IN \(([^)]*)\)", ddl)
    assert clause is not None, f"{table} has no status CHECK to compare against"
    admitted = set(re.findall(r"'([a-z_]+)'", clause.group(1)))

    assert admitted == set(RunStatus.ALL), (
        f"{table}'s CHECK and RunStatus disagree. Only the store admits: "
        f"{admitted - set(RunStatus.ALL)}. Only the code names: "
        f"{set(RunStatus.ALL) - admitted}. Either is a status one half cannot honour."
    )


def test_an_invented_status_is_still_refused(store):
    """The CHECK is widened by migration 013, not removed."""
    with (
        pytest.raises(StoreError, match="CHECK constraint failed"),
        store.transaction() as connection,
    ):
        connection.execute(
            "INSERT INTO collector_runs (collector, started_at, status) VALUES (?, ?, ?)",
            ("chain", "2026-08-24T22:30:00Z", "probably_fine"),
        )


def test_a_stale_running_row_is_reaped_by_the_next_run(store, config):
    window = config.require_int("collectors.run_stale_after_seconds")
    run_id = _abandoned_run(store, "chain", age_seconds=window + 60)

    assert reap_interrupted_runs(store, config) == 1
    assert _status_of(store, run_id) == RunStatus.INTERRUPTED


def test_a_reaped_row_says_it_never_reported_rather_than_inventing_a_count(store, config):
    window = config.require_int("collectors.run_stale_after_seconds")
    run_id = _abandoned_run(store, "chain", age_seconds=window + 60)
    reap_interrupted_runs(store, config)

    row = store.execute(
        "SELECT finished_at, rows_written, detail FROM collector_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    assert row["finished_at"] is not None, "the reaper closes the row; it does not leave it open"
    assert row["rows_written"] == 0, "nothing is known about what it wrote"
    assert "interrupted" in row["detail"]


def test_a_run_still_inside_the_window_is_left_alone(store, config):
    """Two units can fire close together. Reaping a live run would rewrite the
    verdict of a process still working."""
    window = config.require_int("collectors.run_stale_after_seconds")
    run_id = _abandoned_run(store, "macro", age_seconds=max(window - 60, 1))

    assert reap_interrupted_runs(store, config) == 0
    assert _status_of(store, run_id) == RunStatus.RUNNING


def test_the_reaper_never_touches_a_finished_row(store, config):
    with CollectorRun(store, "curve") as run:
        run.succeeded("CL", 12)
    run_id = run.run_id

    reap_interrupted_runs(store, config)
    assert _status_of(store, run_id) == RunStatus.OK


# ── the sequence row ──


def test_a_sequence_records_what_it_planned_before_it_runs_a_step(store):
    """Written on entry, like the run row, so a machine killed on the first step
    still leaves the count of what was meant to happen."""
    with SequenceRun(store, "daily", session_date=SESSION, steps=("chain", "curve")) as sequence:
        row = store.execute(
            "SELECT command, session_date, status, steps_planned, steps_completed "
            "FROM collector_sequences WHERE sequence_id = ?",
            (sequence.sequence_id,),
        ).fetchone()
        assert row["command"] == "daily"
        assert row["session_date"] == SESSION
        assert row["status"] == RunStatus.RUNNING
        assert row["steps_planned"] == 2
        assert row["steps_completed"] == 0
        sequence.step("chain", RunStatus.OK)
        sequence.step("curve", RunStatus.OK)


def test_a_completed_sequence_carries_its_exit_code(store):
    with SequenceRun(store, "daily", session_date=SESSION, steps=("chain",)) as sequence:
        sequence.step("chain", RunStatus.DEGRADED, reason="fallback sigma on FCX")

    row = store.execute(
        "SELECT status, steps_completed, exit_code, finished_at FROM collector_sequences"
    ).fetchone()
    assert row["status"] == RunStatus.DEGRADED
    assert row["steps_completed"] == 1
    assert row["exit_code"] == 2, "the code systemd sees is also the code the store holds"
    assert row["finished_at"] is not None


def test_a_failed_step_outranks_a_degraded_one_in_the_sequence_verdict(store):
    with SequenceRun(store, "daily", session_date=SESSION, steps=("chain", "curve")) as sequence:
        sequence.step("chain", RunStatus.DEGRADED)
        sequence.step("curve", RunStatus.ERROR, reason="no futures roots")

    row = store.execute("SELECT status, exit_code FROM collector_sequences").fetchone()
    assert row["status"] == RunStatus.PARTIAL
    assert row["exit_code"] == 1


def test_a_sequence_that_ran_no_step_at_all_is_not_reported_as_success(store):
    """The failure this package exists to prevent, one level up."""
    with SequenceRun(store, "daily", session_date=SESSION, steps=("chain", "curve")) as sequence:
        sequence.step("chain", RunStatus.SKIPPED)
        sequence.step("curve", RunStatus.SKIPPED)

    row = store.execute("SELECT status, steps_completed FROM collector_sequences").fetchone()
    assert row["status"] == RunStatus.SKIPPED
    assert row["steps_completed"] == 0


def test_a_half_collected_day_is_identifiable_from_the_store_alone(store, config):
    """The whole point. A sequence killed after two of five steps leaves
    steps_completed < steps_planned and a status nobody can read as 'ok'."""
    steps = ("backfill", "chain", "curve", "vendor-chain", "macro")
    sequence = SequenceRun(store, "daily", session_date=SESSION, steps=steps)
    sequence.__enter__()
    sequence.step("backfill", RunStatus.OK)
    sequence.step("chain", RunStatus.OK)
    # and here the machine dies: no __exit__, nothing else written.

    row = store.execute(
        "SELECT status, steps_planned, steps_completed, finished_at FROM collector_sequences"
    ).fetchone()
    assert row["status"] == RunStatus.RUNNING
    assert row["finished_at"] is None
    assert (row["steps_completed"], row["steps_planned"]) == (2, 5)

    # And the next run says so out loud rather than leaving it looking live.
    reap_interrupted_runs(store, config, now=datetime.now(UTC) + timedelta(days=1))
    assert (
        store.execute("SELECT status FROM collector_sequences").fetchone()["status"]
        == RunStatus.INTERRUPTED
    )


def test_each_run_names_the_sequence_it_belonged_to(store):
    with SequenceRun(store, "daily", session_date=SESSION, steps=("chain",)) as sequence:
        with CollectorRun(store, "chain", sequence=sequence, step_index=0) as run:
            run.succeeded("ASML", 120)
        sequence.step("chain", run.status())

    row = store.execute(
        "SELECT sequence_id, step_index FROM collector_runs WHERE collector = 'chain'"
    ).fetchone()
    assert row["sequence_id"] == sequence.sequence_id
    assert row["step_index"] == 0


def test_a_standalone_run_belongs_to_no_sequence(store):
    """`obliskiq-collect chain` on its own is not half of anything."""
    with CollectorRun(store, "chain") as run:
        run.succeeded("ASML", 120)
    row = store.execute(
        "SELECT sequence_id FROM collector_runs WHERE run_id = ?", (run.run_id,)
    ).fetchone()
    assert row["sequence_id"] is None
