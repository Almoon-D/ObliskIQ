-- 011_measurements — the measurement store (MASTER_PLAN §5.7): one long-format table every view is
-- a query over.
--
-- Forward-only. Dates are 'YYYY-MM-DD' and stamps 'YYYY-MM-DDTHH:MM:SSZ', checked as in 001_core.

-- ───────────────────────────── measurements ─────────────────────────────

-- One leaf of one entity as of one session. An entity is an equity-grid cell ('us/broad'), a
-- macro-board cell ('us.rate_10y'), an instrument (its conid) or a correlated pair of legs
-- ('equity:us|rate:DGS10'); indicator is the family a leaf belongs to (one of the grid's fifteen,
-- or a board, instrument or pair family). A build upserts its own session, so the table is a
-- time series per leaf and a rebuild of a past session replaces that session only.
--
-- status: ok (measured), stale (measured on inputs older than their freshness window), n/a
-- (collectible and not collected, or refused on too little) and inapplicable (no collector could
-- ever fill it, as a declaration says). Every leaf that is not ok says why, with a groupable
-- reason_code; an absent leaf has no value and a measured one has a finite value.
-- n_obs is the sample actually behind the value (never the window asked for); window the span
-- and the sample, the join key of a leaf's history; source the inputs read; inputs_hash the
-- SHA-256 of their fingerprint, so a rebuild on the same store reproduces the row.
CREATE TABLE measurements (
    entity_kind        TEXT    NOT NULL CHECK (entity_kind IN (
                                   'equity_cell', 'macro_cell', 'instrument', 'correlation_pair')),
    entity_id          TEXT    NOT NULL CHECK (trim(entity_id) <> ''),
    indicator          TEXT    NOT NULL CHECK (trim(indicator) <> ''),
    leaf               TEXT    NOT NULL CHECK (trim(leaf) <> ''),
    as_of              TEXT    NOT NULL CHECK (as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    value              REAL    CHECK (value IS NULL OR (value > -9e999 AND value < 9e999)),
    status             TEXT    NOT NULL CHECK (status IN ('ok', 'n/a', 'stale', 'inapplicable')),
    reason             TEXT    CHECK (reason IS NULL OR trim(reason) <> ''),
    reason_code        TEXT    CHECK (reason_code IS NULL OR trim(reason_code) <> ''),
    n_obs              INTEGER CHECK (n_obs IS NULL OR n_obs >= 0),
    window             TEXT,
    units              TEXT,
    source             TEXT    CHECK (source IS NULL OR trim(source) <> ''),
    market_data_status TEXT    CHECK (market_data_status IS NULL
                                   OR market_data_status IN ('realtime', 'delayed', 'frozen', 'none')),
    inputs_hash        TEXT    NOT NULL CHECK (length(inputs_hash) = 64
                                   AND inputs_hash NOT GLOB '*[^0-9a-f]*'),
    computed_at        TEXT    NOT NULL CHECK (computed_at GLOB
                                   '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (entity_kind, entity_id, leaf, as_of),
    CHECK (status = 'ok' OR (reason IS NOT NULL AND reason_code IS NOT NULL)),
    CHECK ((status IN ('ok', 'stale')) = (value IS NOT NULL))
) WITHOUT ROWID;

CREATE INDEX ix_measurements_session ON measurements (as_of, entity_kind, leaf);
