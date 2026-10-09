-- 001_initial — the schema from BUILD-BRIEF.md plus the Session 1 additions.
--
-- Design notes that are load-bearing, not decoration:
--   * chain_snapshots and curve_snapshots are APPEND-ONLY daily records. Option
--     chain and futures curve history cannot be retrieved retrospectively from
--     IBKR, so a snapshot not taken is gone permanently. The unique indexes make
--     a re-run idempotent rather than duplicating a day.
--   * macro carries BOTH reference_date and release_date. Aligning a CPI print to
--     its reference month builds look-ahead bias into every percentile derived
--     from it, so release_date is what downstream code aligns on.
--   * every market-data row records its source (mcp | cp_api), so collection can
--     be migrated wholesale if an OAuth session turns out to invalidate the MCP
--     session for the same account.
--   * position_ledger.thesis_text and .invalidation_trigger are frozen at open by
--     trigger. Changes append to reunderwrite_log. Thesis drift becomes detectable
--     rather than a matter of recollection.

PRAGMA foreign_keys = ON;

-- ─────────────────────────────  universe  ─────────────────────────────

CREATE TABLE IF NOT EXISTS universe (
    contract_id        INTEGER PRIMARY KEY,
    symbol             TEXT    NOT NULL,
    exchange           TEXT,
    currency           TEXT    NOT NULL,
    asset_class        TEXT    NOT NULL,
    market             TEXT,
    sector             TEXT,
    is_native_index    INTEGER NOT NULL DEFAULT 0 CHECK (is_native_index IN (0, 1)),
    listing_domicile   TEXT,
    priips_restricted  INTEGER NOT NULL DEFAULT 0 CHECK (priips_restricted IN (0, 1)),

    -- Session 1 additions. An instrument can be macro_signal=1 and tradable=0: it
    -- earns a grid row for regime context without ever becoming a candidate.
    -- Untradeable commodities stay in the data model; they are not excluded.
    tradable           INTEGER NOT NULL DEFAULT 1 CHECK (tradable IN (0, 1)),
    macro_signal       INTEGER NOT NULL DEFAULT 0 CHECK (macro_signal IN (0, 1)),
    untradeable_reason TEXT,

    source             TEXT    NOT NULL DEFAULT 'mcp' CHECK (source IN ('mcp', 'cp_api')),
    created_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),

    -- An instrument marked untradeable must say why; a silent exclusion is a
    -- coverage gap nobody can audit later.
    CHECK (tradable = 1 OR untradeable_reason IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ix_universe_symbol       ON universe (symbol);
CREATE INDEX IF NOT EXISTS ix_universe_market_sector ON universe (market, sector);
CREATE INDEX IF NOT EXISTS ix_universe_tradable     ON universe (tradable, macro_signal);

-- ─────────────────────────────  market data  ─────────────────────────────

CREATE TABLE IF NOT EXISTS bars (
    contract_id INTEGER NOT NULL REFERENCES universe (contract_id) ON DELETE CASCADE,
    ts          TEXT    NOT NULL,          -- ISO-8601 UTC bar start
    freq        TEXT    NOT NULL,          -- 1d | 1w | 1mo
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      REAL,
    source      TEXT    NOT NULL DEFAULT 'mcp' CHECK (source IN ('mcp', 'cp_api')),
    PRIMARY KEY (contract_id, freq, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS ix_bars_freq_ts ON bars (freq, ts);

-- Per (contract, freq) high-water mark, so backfill resumes instead of re-fetching
-- history already held.
CREATE TABLE IF NOT EXISTS bar_watermarks (
    contract_id  INTEGER NOT NULL REFERENCES universe (contract_id) ON DELETE CASCADE,
    freq         TEXT    NOT NULL,
    earliest_ts  TEXT,
    latest_ts    TEXT,
    updated_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (contract_id, freq)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS chain_snapshots (
    snapshot_date  TEXT    NOT NULL,       -- YYYY-MM-DD, the session collected
    underlying_id  INTEGER NOT NULL REFERENCES universe (contract_id) ON DELETE CASCADE,
    expiry         TEXT    NOT NULL,       -- YYYY-MM-DD
    strike         REAL    NOT NULL,
    right          TEXT    NOT NULL CHECK (right IN ('C', 'P')),
    contract_id    INTEGER,                -- the option's own conid, when resolved
    bid            REAL,
    ask            REAL,
    last           REAL,
    iv             REAL,                   -- decimal, not percent
    delta          REAL,
    open_interest  INTEGER,
    volume         INTEGER,
    source         TEXT    NOT NULL DEFAULT 'mcp' CHECK (source IN ('mcp', 'cp_api')),
    collected_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (snapshot_date, underlying_id, expiry, strike, right)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS ix_chain_underlying_date ON chain_snapshots (underlying_id, snapshot_date);
CREATE INDEX IF NOT EXISTS ix_chain_expiry          ON chain_snapshots (expiry);

CREATE TABLE IF NOT EXISTS curve_snapshots (
    snapshot_date   TEXT NOT NULL,         -- YYYY-MM-DD
    root            TEXT NOT NULL,         -- futures root, e.g. CL, NG, HG
    expiry          TEXT NOT NULL,         -- YYYY-MM-DD
    price           REAL,
    annualised_roll REAL,                  -- vs the front contract, decimal
    contract_id     INTEGER,
    exchange        TEXT,
    currency        TEXT,
    source          TEXT NOT NULL DEFAULT 'mcp' CHECK (source IN ('mcp', 'cp_api')),
    collected_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (snapshot_date, root, expiry)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS ix_curve_root_date ON curve_snapshots (root, snapshot_date);

-- ─────────────────────────────  macro  ─────────────────────────────

CREATE TABLE IF NOT EXISTS macro (
    series_id      TEXT NOT NULL,
    source         TEXT NOT NULL,          -- fred | oecd | ecb | cftc | eia
    reference_date TEXT NOT NULL,          -- the period the observation describes
    release_date   TEXT,                   -- when it became public; align on THIS
    value          REAL,
    frequency      TEXT,                   -- native frequency; never upsampled
    units          TEXT,
    collected_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (series_id, source, reference_date)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS ix_macro_release ON macro (series_id, release_date);

-- Per-series pull timestamps, so repeat runs in one day do not re-hit rate limits.
CREATE TABLE IF NOT EXISTS macro_fetch_log (
    series_id     TEXT NOT NULL,
    source        TEXT NOT NULL,
    fetched_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    observations  INTEGER,
    status        TEXT NOT NULL,           -- ok | error
    detail        TEXT,
    PRIMARY KEY (series_id, source)
) WITHOUT ROWID;

-- ─────────────────────────────  grid  ─────────────────────────────

CREATE TABLE IF NOT EXISTS grid (
    as_of      TEXT NOT NULL,              -- YYYY-MM-DD
    market     TEXT NOT NULL,
    sector     TEXT NOT NULL,
    indicator  TEXT NOT NULL,
    value      REAL,
    status     TEXT NOT NULL CHECK (status IN ('ok', 'n/a', 'stale')),
    -- A missing cell states its reason. Never a silent proxy, never an estimate
    -- presented as data.
    reason     TEXT,
    window     TEXT,                       -- the window the value was measured over
    computed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (as_of, market, sector, indicator),
    CHECK (status = 'ok' OR reason IS NOT NULL)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS ix_grid_indicator ON grid (as_of, indicator, status);

-- ─────────────────────────────  ledgers  ─────────────────────────────

CREATE TABLE IF NOT EXISTS position_ledger (
    position_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id           INTEGER NOT NULL REFERENCES universe (contract_id),
    opened_at             TEXT    NOT NULL,
    entry_price           REAL    NOT NULL,
    entry_range_pctile    REAL,
    -- Written once at open and never edited. Enforced by trigger below.
    thesis_text           TEXT    NOT NULL,
    invalidation_trigger  TEXT    NOT NULL,
    target                REAL,
    time_window_end       TEXT,
    mandatory_close_date  TEXT,
    -- P0 = N(d2) at the 3:1 target, from the STRIKE'S OWN smile IV. The zombie
    -- test compares the current probability against this.
    prob_target_at_entry  REAL,
    reunderwrite_log      TEXT    NOT NULL DEFAULT '[]',   -- JSON array of dated entries
    closed_at             TEXT,
    exit_reason           TEXT,             -- constrained to discipline.exit_reasons_allowed
    realised_pnl          REAL,
    created_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK (closed_at IS NULL OR exit_reason IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ix_position_open    ON position_ledger (closed_at, contract_id);
CREATE INDEX IF NOT EXISTS ix_position_opened  ON position_ledger (opened_at);

-- thesis_text and invalidation_trigger are frozen at entry. An UPDATE that changes
-- either is rejected here, in the store, not merely discouraged in a docstring.
CREATE TRIGGER IF NOT EXISTS trg_position_thesis_frozen
BEFORE UPDATE OF thesis_text, invalidation_trigger ON position_ledger
FOR EACH ROW
WHEN NEW.thesis_text IS NOT OLD.thesis_text
  OR NEW.invalidation_trigger IS NOT OLD.invalidation_trigger
BEGIN
    SELECT RAISE(ABORT,
        'thesis_text and invalidation_trigger are frozen at entry; append to reunderwrite_log instead');
END;

CREATE TABLE IF NOT EXISTS option_ledger (
    event_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_at               TEXT    NOT NULL,
    event_type             TEXT    NOT NULL CHECK (event_type IN (
                               'loss_detected', 'loss_closed',
                               'gain_realised', 'qualifying_equity_gain')),
    contract_id            INTEGER REFERENCES universe (contract_id),
    position_id            INTEGER REFERENCES position_ledger (position_id),
    premium_paid           REAL,
    proceeds               REAL,
    realised_pnl           REAL,
    -- Booked at zombie DETECTION, not at close. Booking at close would reward
    -- deferring the close in order to postpone the penalty.
    penalty_added          REAL    NOT NULL DEFAULT 0.0,
    penalty_balance_after  REAL    NOT NULL,
    resolved_at            TEXT,
    note                   TEXT,
    created_at             TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS ix_option_ledger_at   ON option_ledger (event_at);
CREATE INDEX IF NOT EXISTS ix_option_ledger_type ON option_ledger (event_type, event_at);

-- Two-session confirmation state for the zombie test. A single session never fires.
CREATE TABLE IF NOT EXISTS zombie_observations (
    position_id     INTEGER NOT NULL REFERENCES position_ledger (position_id) ON DELETE CASCADE,
    observed_on     TEXT    NOT NULL,      -- YYYY-MM-DD session date
    all_conditions_met INTEGER NOT NULL CHECK (all_conditions_met IN (0, 1)),
    delta           REAL,
    dte             INTEGER,
    prob_target     REAL,
    prob_ratio_vs_entry REAL,
    recoverable_pct_of_premium REAL,
    detail          TEXT,
    PRIMARY KEY (position_id, observed_on)
) WITHOUT ROWID;

-- ─────────────────────────────  external positions  ─────────────────────────────

-- IBKR is the single source of truth for the portfolio. Secondary brokers
-- (DEGIRO, Revolut, Openbank, Santander) have no readable API, so holdings there
-- are entered by hand. NAV, the 5% option sleeve and every concentration cap are
-- computed on IBKR positions PLUS these rows. Beyond
-- positions.external_data_stale_days, cap utilisation must be reported UNRELIABLE
-- rather than returning a number known to be wrong.
CREATE TABLE IF NOT EXISTS external_positions (
    external_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    broker            TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    isin              TEXT,
    quantity          REAL NOT NULL,
    currency          TEXT NOT NULL,
    market_value_base REAL NOT NULL,       -- in account.base_currency
    last_updated      TEXT NOT NULL,       -- when the HUMAN last confirmed it
    notes             TEXT,
    sector            TEXT,
    country           TEXT,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (broker, symbol, isin)
);

CREATE INDEX IF NOT EXISTS ix_external_updated ON external_positions (last_updated);

-- ─────────────────────────────  operational  ─────────────────────────────

-- What each collector run did. The collectors run unattended; this is the record.
CREATE TABLE IF NOT EXISTS collector_runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    collector    TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'error')),
    rows_written INTEGER NOT NULL DEFAULT 0,
    targets_ok   INTEGER NOT NULL DEFAULT 0,
    targets_failed INTEGER NOT NULL DEFAULT 0,
    detail       TEXT
);

CREATE INDEX IF NOT EXISTS ix_collector_runs ON collector_runs (collector, started_at);

CREATE TABLE IF NOT EXISTS universe_census (
    as_of              TEXT    NOT NULL PRIMARY KEY,
    contracts_scanned  INTEGER NOT NULL,
    passed_all_gates   INTEGER NOT NULL,
    passed_price       INTEGER,
    passed_open_interest INTEGER,
    passed_spread      INTEGER,
    passed_tenor       INTEGER,
    passed_iv_pctile   INTEGER,
    detail             TEXT
) WITHOUT ROWID;
