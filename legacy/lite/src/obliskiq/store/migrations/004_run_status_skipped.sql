-- 004 — collector_runs gains 'skipped'.
--
-- A run scoped with --only can name none of a collector's targets. The
-- collectors do not share a namespace: --only SPY is meaningful to the chain
-- and backfill collectors and meaningless to the curve collector, which tracks
-- futures roots. Inside the 'daily' sequence that is the filter not naming this
-- step, not a failed collection.
--
-- It needs its own status rather than either neighbour. Recorded as 'error' it
-- would make a scoped manual run indistinguishable, in the run log, from the
-- curve collector genuinely failing overnight — and curve shape cannot be
-- recovered later, so that is the one alert that must stay trustworthy.
-- Recorded as 'ok' it would report success over zero rows, which is the failure
-- this package exists to prevent.
--
-- SQLite cannot alter a CHECK constraint in place, so the table is rebuilt as
-- 002 did. Existing rows carry across unchanged.

PRAGMA foreign_keys = ON;

ALTER TABLE collector_runs RENAME TO collector_runs_002;

CREATE TABLE collector_runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    collector    TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    -- degraded: everything was collected, but on a weaker input than intended.
    -- skipped:  nothing was collected because --only named none of the targets.
    status       TEXT NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'degraded', 'skipped', 'error')),
    rows_written INTEGER NOT NULL DEFAULT 0,
    targets_ok   INTEGER NOT NULL DEFAULT 0,
    targets_failed INTEGER NOT NULL DEFAULT 0,
    detail       TEXT
);

INSERT INTO collector_runs (run_id, collector, started_at, finished_at, status,
                            rows_written, targets_ok, targets_failed, detail)
SELECT run_id, collector, started_at, finished_at, status,
       rows_written, targets_ok, targets_failed, detail
FROM collector_runs_002;

DROP TABLE collector_runs_002;

CREATE INDEX IF NOT EXISTS ix_collector_runs ON collector_runs (collector, started_at);
