-- 002_collectors — everything the unattended collectors need that 001 did not
-- anticipate. Forward-only: 001_initial.sql is never edited.
--
-- Design notes that are load-bearing, not decoration:
--   * universe.source keeps meaning "which API route this row's DATA came from"
--     (mcp | cp_api), because CLAUDE.md depends on it to migrate collection
--     wholesale. HOW a row entered the universe is a different axis and gets its
--     own column, origin. Overloading source would blind the migration signal
--     for exactly the rows the account actually holds.
--   * chain_snapshots records the sigma that set its strike bounds AND which
--     rung of the fallback ladder produced it. A band is then auditable after
--     the fact rather than reconstructed from whatever the store holds today.
--   * curve collection is keyed by futures ROOT, not by conid, so it cannot live
--     in universe, whose primary key is contract_id NOT NULL.
--   * collector_runs gains 'degraded': a run that collected everything but had
--     to fall back to a weaker sigma source is neither 'ok' nor 'partial', and
--     collapsing it into either hides the one thing worth alerting on.

PRAGMA foreign_keys = ON;

-- ─────────────────────────────  universe  ─────────────────────────────

-- How the row entered the universe. 'declared' rows come from universe.yaml and
-- are never reconciled away; 'portfolio_sync' rows are folded in from the live
-- account and drop out of collection once the position is gone, so a closed
-- position stops consuming API budget forever.
ALTER TABLE universe ADD COLUMN origin TEXT NOT NULL DEFAULT 'declared'
    CHECK (origin IN ('declared', 'portfolio_sync'));

-- Last time the resolver saw this row in its input (the YAML, or the account).
ALTER TABLE universe ADD COLUMN last_seen_at TEXT;

-- What to collect for this row. Chain collection keys off collect_chain and
-- NEVER off priips_restricted: an EU retail account cannot buy a US cash ETF for
-- want of a KID, but may trade listed options on it. PRIIPs gates the candidate
-- list, never collection.
ALTER TABLE universe ADD COLUMN collect_chain INTEGER NOT NULL DEFAULT 0
    CHECK (collect_chain IN (0, 1));
ALTER TABLE universe ADD COLUMN collect_bars  INTEGER NOT NULL DEFAULT 1
    CHECK (collect_bars IN (0, 1));

-- Set when the instrument began trading, where known. A sigma fallback on a
-- genuinely new listing is expected; on anything else it means the backfill is
-- not keeping up, which is what the 'degraded' run status exists to surface.
ALTER TABLE universe ADD COLUMN listed_on TEXT;

CREATE INDEX IF NOT EXISTS ix_universe_collect ON universe (collect_chain, collect_bars);
CREATE INDEX IF NOT EXISTS ix_universe_origin  ON universe (origin, last_seen_at);

-- ─────────────────────────────  chain snapshots  ─────────────────────────────

-- The sigma that set this row's strike bounds, and the rung of the fallback
-- ladder that produced it (realised_30d | realised_10d | vendor_iv | default).
ALTER TABLE chain_snapshots ADD COLUMN sigma_used   REAL;
ALTER TABLE chain_snapshots ADD COLUMN sigma_source TEXT;

-- ─────────────────────────────  futures roots  ─────────────────────────────

-- The tracked commodity roots for curve_collector. search_futures / the CP
-- /trsrv/futures equivalent is a DATA call and stays permitted despite the
-- futures prohibition — see instruments.futures_data_use in config.yaml.
CREATE TABLE IF NOT EXISTS futures_roots (
    root        TEXT PRIMARY KEY,       -- CL, NG, HG, GC …
    exchange    TEXT,                   -- native exchange; SMART returns empty
    currency    TEXT,
    bucket      TEXT,                   -- one of commodities.buckets
    description TEXT,
    tracked     INTEGER NOT NULL DEFAULT 1 CHECK (tracked IN (0, 1)),
    seasonal    INTEGER NOT NULL DEFAULT 0 CHECK (seasonal IN (0, 1)),
    last_seen_at TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS ix_futures_roots_tracked ON futures_roots (tracked);

-- ─────────────────────────────  collector state  ─────────────────────────────

-- Small key/value scratch for facts a collector learns at runtime and should not
-- re-learn on every run. The probed daily-bar per-call cap lives here: the
-- documented figure is unverified, so it is measured and remembered rather than
-- assumed.
CREATE TABLE IF NOT EXISTS collector_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
) WITHOUT ROWID;

-- ─────────────────────────────  collector runs  ─────────────────────────────

-- SQLite cannot alter a CHECK constraint in place, so the table is rebuilt to
-- admit 'degraded'. Existing rows are carried across unchanged.
ALTER TABLE collector_runs RENAME TO collector_runs_001;

CREATE TABLE collector_runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    collector    TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    -- degraded: everything was collected, but on a weaker input than intended.
    -- Distinct from partial (some targets failed) and from ok.
    status       TEXT NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'degraded', 'error')),
    rows_written INTEGER NOT NULL DEFAULT 0,
    targets_ok   INTEGER NOT NULL DEFAULT 0,
    targets_failed INTEGER NOT NULL DEFAULT 0,
    detail       TEXT
);

INSERT INTO collector_runs (run_id, collector, started_at, finished_at, status,
                            rows_written, targets_ok, targets_failed, detail)
SELECT run_id, collector, started_at, finished_at, status,
       rows_written, targets_ok, targets_failed, detail
FROM collector_runs_001;

DROP TABLE collector_runs_001;

CREATE INDEX IF NOT EXISTS ix_collector_runs ON collector_runs (collector, started_at);
