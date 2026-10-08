-- 013 — a run that was killed says so, and a day half collected is identifiable
-- as such from the store alone.
--
-- WHY THIS EXISTS. collector_runs already inserts its row with status='running'
-- BEFORE the work starts, precisely so a run the machine killed leaves evidence
-- rather than nothing. But nothing ever reclaimed that row, so 'running' meant
-- both "in flight right now" and "died last Tuesday" — and an operator reading
-- the run log the morning after could not tell which.
--
-- Two changes follow.
--
-- 1. status gains 'interrupted'. It needs its own value rather than either
--    neighbour. Recorded as 'error' it would be indistinguishable from a run
--    that DID reach a verdict and reported failure, which is a different fact:
--    an error names what went wrong, an interruption names that nothing got to
--    say. Recorded as 'ok' it would report success over an unknown amount of
--    work, which is the failure this package exists to prevent. The next run
--    reaps it — see collect/runlog.reap_interrupted_runs and the age window in
--    collectors.run_stale_after_seconds.
--
-- 2. collector_sequences. `obliskiq-collect daily` runs five steps and wrote
--    five unrelated collector_runs rows: nothing tied them together, and
--    nothing recorded how many steps were MEANT to happen. So a sequence that
--    died after two of five left a store indistinguishable from one that
--    collected two steps and legitimately skipped the rest. steps_planned is
--    written on entry, before any step runs, for the same reason status='running'
--    is: the count of intended work has to survive the process that intended it.
--
--    exit_code is stored beside the status deliberately. The exit code is what
--    systemd sees and the status row is what a human reads the morning after;
--    holding both in one row is what makes the two accounts checkable against
--    each other instead of merely coexisting.
--
-- collector_runs is a rowid table with AUTOINCREMENT and SQLite cannot alter a
-- CHECK in place, so it is rebuilt exactly as 002 and 004 did, with run_id
-- carried across explicitly so existing history keeps its identifiers and the
-- foreign-key-free detail JSON stays addressable.
--
-- SPDX-License-Identifier: AGPL-3.0-or-later

PRAGMA foreign_keys = ON;

-- ── collector_runs ──────────────────────────────────────────────────────────

ALTER TABLE collector_runs RENAME TO collector_runs_004;

CREATE TABLE collector_runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    collector    TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    -- degraded:    everything collected, on a weaker input than intended.
    -- skipped:     nothing collected because a filter named none of the targets.
    -- interrupted: the process never reported a verdict at all.
    status       TEXT NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'degraded',
                                                 'skipped', 'error', 'interrupted')),
    rows_written INTEGER NOT NULL DEFAULT 0,
    targets_ok   INTEGER NOT NULL DEFAULT 0,
    targets_failed INTEGER NOT NULL DEFAULT 0,
    detail       TEXT,
    -- The sequence this run was a step of, NULL when it was invoked on its own.
    -- `obliskiq-collect chain` is not half of anything, and a NULL here says so
    -- rather than inventing a sequence of one.
    sequence_id  TEXT,
    step_index   INTEGER
);

INSERT INTO collector_runs (run_id, collector, started_at, finished_at, status,
                            rows_written, targets_ok, targets_failed, detail)
SELECT run_id, collector, started_at, finished_at, status,
       rows_written, targets_ok, targets_failed, detail
FROM collector_runs_004;

DROP TABLE collector_runs_004;

CREATE INDEX IF NOT EXISTS ix_collector_runs ON collector_runs (collector, started_at);
CREATE INDEX IF NOT EXISTS ix_collector_runs_sequence ON collector_runs (sequence_id);

-- ── collector_sequences ─────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS collector_sequences (
    sequence_id     TEXT PRIMARY KEY,
    command         TEXT NOT NULL,          -- 'daily' today; the only multi-step one
    session_date    TEXT NOT NULL,          -- YYYY-MM-DD, the day being collected FOR
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'degraded',
                                                    'skipped', 'error', 'interrupted')),
    -- Written on entry. A sequence killed on its first step still says how many
    -- steps it meant to run, which is what makes an incomplete day legible.
    steps_planned   INTEGER NOT NULL,
    -- Steps that reported a verdict of their own. A step filtered out by --only
    -- or --skip collected nothing and does not count toward this.
    steps_completed INTEGER NOT NULL DEFAULT 0,
    exit_code       INTEGER,
    detail          TEXT
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS ix_collector_sequences_session
    ON collector_sequences (session_date, started_at);
