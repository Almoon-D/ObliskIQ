-- 003_compute — everything the computation layer reads and writes.
-- Forward-only: 001 and 002 are never edited.
--
-- Design notes that are load-bearing, not decoration:
--   * underlying_snapshots is the per-underlying twin of chain_snapshots. The
--     chain collector already fetches a quote per underlying to size its strike
--     band and then throws it away; keeping it is what makes an IV percentile
--     self-computable later, and what carries the vendor figure today.
--   * grid gains indicator_family. A family from SKILL.md's fixed set ("returns")
--     emits several leaf cells ("return_21d_base"), so the leaf stays addressable
--     while the fifteen families stay countable.
--   * grid_coverage is one row per market x sector, not per indicator. Which
--     instrument measured a cell, and its ETF disclosure, do not vary across the
--     fifteen families and must not be stored fifteen times.
--   * commodity_state keeps cot_window_years and cycle_window_years as separate
--     columns on purpose. A 5-year positioning percentile must never be readable
--     as cycle position on a 10-20 year cycle; separate columns make that a
--     schema fact rather than a convention someone remembers.
--   * every derived table carries status + reason, matching grid. A missing cell
--     states why it is missing; it is never absent and never a silent proxy.

PRAGMA foreign_keys = ON;

-- ─────────────────────────  underlying snapshots  ─────────────────────────

-- One row per underlying per session: the quote the chain collector already
-- fetches, kept rather than discarded.
--
-- iv_percentile_52w is the VENDOR figure and is NULL until
-- ibkr.cp_api.marketdata.underlying_snapshot_fields.implied_vol_percentile names
-- a verified field id. A NULL here is a coverage gap the grid reports; a guessed
-- field id would write some other quantity into a percentile column and nobody
-- would see it. See references/ibkr-playbook.md.
CREATE TABLE IF NOT EXISTS underlying_snapshots (
    snapshot_date      TEXT    NOT NULL,       -- YYYY-MM-DD
    contract_id        INTEGER NOT NULL REFERENCES universe (contract_id) ON DELETE CASCADE,
    last               REAL,
    bid                REAL,
    ask                REAL,
    volume             INTEGER,
    implied_vol        REAL,                   -- decimal, not percent
    iv_percentile_52w  REAL,                   -- vendor; decimal 0-1. NULL = not served
    historical_vol_30d REAL,                   -- vendor; decimal. Cross-check only
    source             TEXT    NOT NULL DEFAULT 'cp_api' CHECK (source IN ('mcp', 'cp_api')),
    collected_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (snapshot_date, contract_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS ix_underlying_snap_contract
    ON underlying_snapshots (contract_id, snapshot_date);

-- ─────────────────────────────  grid  ─────────────────────────────

-- The SKILL.md family this leaf belongs to. Fifteen families; more leaves.
ALTER TABLE grid ADD COLUMN indicator_family TEXT;

-- Which instrument produced the cell. Null for a cell computed from macro alone.
ALTER TABLE grid ADD COLUMN contract_id INTEGER;

-- What the value means: 'decimal' (0.031 = 3.1%), 'ratio', 'percentile',
-- 'days', 'index'. A number without its unit is not data.
ALTER TABLE grid ADD COLUMN units TEXT;

-- A short, groupable classification of why a cell is absent. The cell's own
-- `reason` names the specific market, series or count; this is what a coverage
-- report groups on. Without it the histogram is one line per market x sector
-- saying the same thing 141 times, which is a report nobody reads.
ALTER TABLE grid ADD COLUMN reason_code TEXT;

CREATE INDEX IF NOT EXISTS ix_grid_family ON grid (as_of, indicator_family);

-- One row per cell, not per indicator: which instrument measured this
-- market x sector, and — when it is an ETF — the disclosure the mandate requires.
CREATE TABLE IF NOT EXISTS grid_coverage (
    as_of              TEXT NOT NULL,
    market             TEXT NOT NULL,
    sector             TEXT NOT NULL,
    contract_id        INTEGER,
    symbol             TEXT,
    -- native_index | etf | equity | none
    instrument_kind    TEXT NOT NULL,
    is_native_index    INTEGER NOT NULL DEFAULT 0 CHECK (is_native_index IN (0, 1)),
    -- Top two holdings as a share of the fund. Above
    -- data.etf_top2_concentration_flag the cell is a two-stock read, not a
    -- country read, and the note says so.
    top2_concentration REAL,
    hedged             INTEGER CHECK (hedged IN (0, 1)),
    coverage_note      TEXT,
    computed_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (as_of, market, sector)
) WITHOUT ROWID;

-- The ranked views. The session reads these, never the raw cells.
CREATE TABLE IF NOT EXISTS grid_rankings (
    as_of      TEXT    NOT NULL,
    -- most_extended | most_compressed | widest_iv_hv | largest_correlation_shift
    dimension  TEXT    NOT NULL,
    rank       INTEGER NOT NULL,
    market     TEXT    NOT NULL,
    sector     TEXT    NOT NULL,
    indicator  TEXT    NOT NULL,
    value      REAL,
    window     TEXT,
    detail     TEXT,
    computed_at TEXT   NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (as_of, dimension, rank)
) WITHOUT ROWID;

-- Cross-market correlation at every window in data.correlation_windows_days,
-- against each PAIR'S OWN norm rather than a shared one: two markets that are
-- always correlated at 0.9 and two that are never correlated both deviate
-- meaningfully, and a single global threshold sees only the first.
CREATE TABLE IF NOT EXISTS correlation_matrix (
    as_of       TEXT    NOT NULL,
    market_a    TEXT    NOT NULL,
    market_b    TEXT    NOT NULL,
    window_days INTEGER NOT NULL,
    correlation REAL,
    norm_mean   REAL,
    norm_sd     REAL,
    z_score     REAL,
    flagged     INTEGER NOT NULL DEFAULT 0 CHECK (flagged IN (0, 1)),
    status      TEXT    NOT NULL CHECK (status IN ('ok', 'n/a', 'stale')),
    reason      TEXT,
    computed_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (as_of, market_a, market_b, window_days),
    CHECK (status = 'ok' OR reason IS NOT NULL)
) WITHOUT ROWID;

-- ─────────────────────────────  fx  ─────────────────────────────

-- The four-way decomposition, stored in full. Ranking consumes base_real ONLY;
-- local_nominal is kept because it must be DISPLAYED, never because it may be
-- ranked on.
CREATE TABLE IF NOT EXISTS fx_state (
    as_of                   TEXT    NOT NULL,
    market                  TEXT    NOT NULL,
    horizon_days            INTEGER NOT NULL,
    currency                TEXT    NOT NULL,
    local_nominal           REAL,
    local_real              REAL,
    base_nominal            REAL,
    base_real               REAL,
    cpi_yoy                 REAL,
    base_cpi_yoy            REAL,
    fx_return               REAL,
    depreciation_annualised REAL,
    high_inflation          INTEGER NOT NULL DEFAULT 0 CHECK (high_inflation IN (0, 1)),
    high_depreciation       INTEGER NOT NULL DEFAULT 0 CHECK (high_depreciation IN (0, 1)),
    status                  TEXT    NOT NULL CHECK (status IN ('ok', 'n/a', 'stale')),
    reason                  TEXT,
    computed_at             TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (as_of, market, horizon_days),
    CHECK (status = 'ok' OR reason IS NOT NULL)
) WITHOUT ROWID;

-- Under covered interest parity the forward discount approximates the rate
-- differential, so a hedged high-yield bond returns roughly the base rate. This
-- table is the record of that test having been run, per currency.
CREATE TABLE IF NOT EXISTS carry_tests (
    as_of              TEXT NOT NULL,
    currency           TEXT NOT NULL,
    nominal_yield      REAL,
    local_rate         REAL,
    base_rate          REAL,
    rate_differential  REAL,
    hedged_equivalent  REAL,
    base_real_yield    REAL,
    -- illusion | opportunity | n/a
    verdict            TEXT NOT NULL,
    reason             TEXT NOT NULL,
    computed_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (as_of, currency)
) WITHOUT ROWID;

-- ─────────────────────────────  commodities  ─────────────────────────────

CREATE TABLE IF NOT EXISTS commodity_state (
    as_of                     TEXT NOT NULL,
    root                      TEXT NOT NULL,
    bucket                    TEXT,
    front_expiry              TEXT,
    front_price               REAL,
    deferred_expiry           TEXT,
    deferred_price            REAL,
    annualised_roll           REAL,
    -- contango | backwardation | flat | n/a
    curve_shape               TEXT,
    -- The wrapper to use, from commodities.preferred_wrapper_order.
    wrapper_recommendation    TEXT,
    -- Set when the futures-tracking wrapper was rejected, with the reason.
    wrapper_rejected          TEXT,
    wrapper_reason            TEXT,
    -- 5-year positioning percentile. cot_window_years travels with it, always.
    cot_percentile            REAL,
    cot_window_years          INTEGER,
    cot_extreme               INTEGER NOT NULL DEFAULT 0 CHECK (cot_extreme IN (0, 1)),
    price_confirmation_required INTEGER NOT NULL DEFAULT 1
                                CHECK (price_confirmation_required IN (0, 1)),
    -- Deviation from the SEASONAL band (same week in prior years), not a rolling
    -- window. Its own window travels with it too.
    inventory_deviation       REAL,
    inventory_window_years    INTEGER,
    -- Position in the LONG cycle, from the monthly history. Never the 5-year
    -- percentile above: an oil cycle runs 10-20 years.
    cycle_position_pctile     REAL,
    cycle_window_years        INTEGER,
    status                    TEXT NOT NULL CHECK (status IN ('ok', 'n/a', 'stale')),
    reason                    TEXT,
    computed_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (as_of, root),
    CHECK (status = 'ok' OR reason IS NOT NULL)
) WITHOUT ROWID;

-- Monthly beta and R-squared of a proxy equity against its reference commodity
-- series. Below commodities.proxies.min_r2 the proxy is unreliable and must not
-- be presented as commodity exposure at all.
CREATE TABLE IF NOT EXISTS commodity_proxies (
    as_of            TEXT    NOT NULL,
    contract_id      INTEGER NOT NULL REFERENCES universe (contract_id) ON DELETE CASCADE,
    symbol           TEXT    NOT NULL,
    reference_series TEXT    NOT NULL,
    beta             REAL,
    r2               REAL,
    observations     INTEGER,
    window_months    INTEGER,
    reliable         INTEGER NOT NULL DEFAULT 0 CHECK (reliable IN (0, 1)),
    reason           TEXT,
    computed_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (as_of, contract_id, reference_series)
) WITHOUT ROWID;

-- ─────────────────────────────  portfolio  ─────────────────────────────

-- NAV over time, for the rolling-month drawdown circuit breaker. Populated from
-- the account in Session 4; until then the breaker reports n/a with a reason
-- rather than a comforting zero.
CREATE TABLE IF NOT EXISTS nav_history (
    as_of      TEXT NOT NULL PRIMARY KEY,      -- YYYY-MM-DD
    nav_base   REAL NOT NULL,                  -- in account.base_currency
    source     TEXT NOT NULL DEFAULT 'ibkr',
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
) WITHOUT ROWID;

-- The commodity bucket, orthogonal to sector. Gold is a precious_monetary
-- bucket and a materials sector; pooling the two axes produces meaningless
-- correlations, which is why commodities.buckets exists at all.
ALTER TABLE universe ADD COLUMN bucket TEXT;

-- This row is the measurement instrument for its market x sector grid cell. At
-- most one per cell. Everything else in the universe is a candidate or a
-- collection target, and must never become a country's proxy by accident.
ALTER TABLE universe ADD COLUMN grid_measures INTEGER NOT NULL DEFAULT 0
    CHECK (grid_measures IN (0, 1));

-- ETF disclosure, required wherever a fund measures a cell. top2_concentration
-- is NULL until a sourced, dated holdings disclosure supplies it: there is no
-- free global holdings feed, and an invented figure would defeat the very
-- warning it feeds.
ALTER TABLE universe ADD COLUMN top2_concentration REAL;
ALTER TABLE universe ADD COLUMN hedged INTEGER CHECK (hedged IN (0, 1));
ALTER TABLE universe ADD COLUMN coverage_note TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS ux_universe_grid_cell
    ON universe (market, sector) WHERE grid_measures = 1;

-- Futures roots carry the same tradable / macro_signal distinction as universe
-- rows. tradable is false on every root — a futures contract is prohibited —
-- and macro_signal is true on every root, because the curve is a regime input
-- whether or not the commodity can be held in any form.
ALTER TABLE futures_roots ADD COLUMN tradable INTEGER NOT NULL DEFAULT 0
    CHECK (tradable IN (0, 1));
ALTER TABLE futures_roots ADD COLUMN macro_signal INTEGER NOT NULL DEFAULT 1
    CHECK (macro_signal IN (0, 1));
ALTER TABLE futures_roots ADD COLUMN untradeable_reason TEXT;

-- Which declared macro series carry this root's positioning, its inventory and
-- its long price history. Named here rather than inferred from the root code:
-- a mapping the code guessed would be a mapping nobody could audit.
--
-- history_series is the LONG one. A 5-year positioning percentile and a 20-year
-- cycle position are different measurements over different windows, and the
-- schema keeps them apart so one can never be printed as the other.
ALTER TABLE futures_roots ADD COLUMN cot_series TEXT;
ALTER TABLE futures_roots ADD COLUMN inventory_series TEXT;
ALTER TABLE futures_roots ADD COLUMN history_series TEXT;

-- Declared proxy -> reference series mapping, seeded from universe.yaml.
-- reference_kind: macro (a macro.series id) | curve (front-month curve history)
-- | none (no free reference price exists; the proxy is unreliable by
-- declaration, and says so).
CREATE TABLE IF NOT EXISTS commodity_proxy_map (
    symbol         TEXT NOT NULL PRIMARY KEY,
    root           TEXT,
    reference      TEXT,
    reference_kind TEXT NOT NULL CHECK (reference_kind IN ('macro', 'curve', 'none')),
    note           TEXT,
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK (reference_kind = 'none' OR reference IS NOT NULL)
) WITHOUT ROWID;

-- Which country a holding is exposed to, for the country cap. market is a
-- measurement grouping; country is a cap dimension, and conflating them makes
-- an ADR count against the wrong one.
ALTER TABLE universe ADD COLUMN country TEXT;

-- ─────────────────────────────  ingestion audit  ─────────────────────────────

-- Leveraged and inverse ETPs are rejected AT INGESTION, not at recommendation
-- time. A rejection that leaves no trace is indistinguishable from a resolver
-- failure, so each one is recorded with its reason.
CREATE TABLE IF NOT EXISTS universe_rejections (
    rejected_at TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    exchange    TEXT,
    origin      TEXT NOT NULL,
    reason      TEXT NOT NULL,
    detail      TEXT,
    PRIMARY KEY (rejected_at, symbol, origin)
) WITHOUT ROWID;

-- ─────────────────────────────  census  ─────────────────────────────

-- What "enough" was on the day, and the verdict. If the count is routinely below
-- the floor, that is reported — the thresholds are never quietly relaxed.
ALTER TABLE universe_census ADD COLUMN min_required INTEGER;
ALTER TABLE universe_census ADD COLUMN verdict TEXT;
