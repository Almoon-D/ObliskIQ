-- 001_core — instruments, market data, macro series and run records.
--
-- Forward-only: a migration that has been applied is never edited; change the schema with a
-- new numbered file.
--
-- What every table below shares:
--   * A date is 'YYYY-MM-DD' and a timestamp is 'YYYY-MM-DDTHH:MM:SSZ' in UTC, one fixed shape
--     per kind, checked here. Fixed-width text sorts as time, so a range read is an index scan.
--   * A missing value is NULL. Nothing is stored as a placeholder: a NULL implied volatility is
--     "not measured", never 0.
--   * market_data_status is the decoded kind of quote (realtime | delayed | frozen | none), NULL
--     when the route reported nothing, which is never read as realtime. market_data_raw keeps
--     the route's own flag verbatim, so a flag the decoder does not know yet is not lost.
--   * Snapshot tables (snapshots, chain_snapshots, curves) are daily records. A chain or curve
--     that was not taken on its day cannot be recovered from the broker later; the primary key
--     makes a re-run replace that day instead of duplicating it.
--   * source names the route that supplied a row. It is not constrained here: SQLite cannot
--     alter a CHECK, and the route vocabulary is not fixed yet; the domain accepts any
--     non-empty text.

-- ───────────────────────────── instruments ─────────────────────────────

-- An instrument is never deleted. A correction supersedes it: the old row keeps its history
-- and names its replacement, so a read as of an earlier date sees whether it existed and
-- whether it had been superseded by then.
CREATE TABLE instruments (
    conid            INTEGER PRIMARY KEY CHECK (conid > 0),
    symbol           TEXT    NOT NULL CHECK (trim(symbol) <> ''),
    listing_exchange TEXT    NOT NULL CHECK (trim(listing_exchange) <> ''),
    currency         TEXT    NOT NULL CHECK (currency GLOB '[A-Z][A-Z][A-Z]'),
    asset_class      TEXT    NOT NULL CHECK (trim(asset_class) <> ''),
    domicile         TEXT    CHECK (domicile IS NULL OR domicile GLOB '[A-Z][A-Z]'),
    ucits            INTEGER CHECK (ucits IS NULL OR ucits IN (0, 1)),
    kid_status       TEXT,
    share_class      TEXT,
    lot_size         INTEGER CHECK (lot_size IS NULL OR lot_size > 0),
    option_exchange  TEXT,
    history_exchange TEXT,
    roles            TEXT    NOT NULL CHECK (
                         json_valid(roles)
                         AND json_type(roles) = 'array'
                         AND json_array_length(roles) > 0),
    superseded_by    INTEGER REFERENCES instruments (conid) ON DELETE RESTRICT
                             CHECK (superseded_by IS NULL OR superseded_by <> conid),
    created_at       TEXT    NOT NULL CHECK (created_at GLOB
                         '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    superseded_at    TEXT    CHECK (superseded_at IS NULL OR superseded_at GLOB
                         '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    CHECK ((superseded_by IS NULL) = (superseded_at IS NULL)),
    CHECK (superseded_at IS NULL OR superseded_at >= created_at)
);

CREATE INDEX ix_instruments_symbol ON instruments (symbol);
CREATE INDEX ix_instruments_superseded_by ON instruments (superseded_by)
    WHERE superseded_by IS NOT NULL;

-- ───────────────────────────── bars ─────────────────────────────

-- Daily bars only, one per session of the instrument's listing venue. session_date is that
-- session's local date, so a bar belongs to exactly one day whatever the venue's time zone, and
-- a weekly or monthly bar (which would carry a period-start date and so leak the days after it)
-- cannot be stored: those are derived from daily bars when they are needed.
CREATE TABLE bars (
    conid        INTEGER NOT NULL REFERENCES instruments (conid) ON DELETE RESTRICT,
    session_date TEXT    NOT NULL CHECK (session_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    open         REAL    CHECK (open IS NULL OR open >= 0),
    high         REAL    CHECK (high IS NULL OR high >= 0),
    low          REAL    CHECK (low IS NULL OR low >= 0),
    close        REAL    NOT NULL CHECK (close >= 0),
    volume       REAL    CHECK (volume IS NULL OR volume >= 0),
    source       TEXT    NOT NULL CHECK (trim(source) <> ''),
    PRIMARY KEY (conid, session_date)
) WITHOUT ROWID;

-- ───────────────────────────── snapshots ─────────────────────────────

-- One row per underlying per session: the quote taken beside the chain, and the figures a
-- vendor or this system derived from it.
CREATE TABLE snapshots (
    snapshot_date             TEXT    NOT NULL CHECK (snapshot_date GLOB
                                  '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    conid                     INTEGER NOT NULL REFERENCES instruments (conid) ON DELETE RESTRICT,
    last                      REAL    CHECK (last IS NULL OR last >= 0),
    bid                       REAL    CHECK (bid IS NULL OR bid >= 0),
    ask                       REAL    CHECK (ask IS NULL OR ask >= 0),
    volume                    INTEGER CHECK (volume IS NULL OR volume >= 0),
    -- A figure the exchange or a vendor quoted, and the one this system inverted from a mid
    -- price, are different measurements; a series that mixed them would break at the join.
    implied_vol               REAL    CHECK (implied_vol IS NULL OR implied_vol >= 0),
    implied_vol_source        TEXT,
    historical_vol_30d        REAL    CHECK (historical_vol_30d IS NULL OR historical_vol_30d >= 0),
    dividend_yield            REAL,
    dividend_yield_basis      TEXT,
    implied_vol_solved        REAL    CHECK (implied_vol_solved IS NULL OR implied_vol_solved >= 0),
    implied_vol_solved_source TEXT,
    source                    TEXT    NOT NULL CHECK (trim(source) <> ''),
    collected_at              TEXT    NOT NULL CHECK (collected_at GLOB
                                  '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    market_data_status        TEXT    CHECK (market_data_status IS NULL
                                  OR market_data_status IN ('realtime', 'delayed', 'frozen', 'none')),
    market_data_raw           TEXT,
    quote_delay_seconds       INTEGER CHECK (quote_delay_seconds IS NULL OR quote_delay_seconds >= 0),
    PRIMARY KEY (snapshot_date, conid),
    CHECK ((dividend_yield IS NULL) = (dividend_yield_basis IS NULL)),
    CHECK ((implied_vol_solved IS NULL) = (implied_vol_solved_source IS NULL))
) WITHOUT ROWID;

CREATE INDEX ix_snapshots_conid_date ON snapshots (conid, snapshot_date);

-- ───────────────────────────── chain_snapshots ─────────────────────────────

CREATE TABLE chain_snapshots (
    snapshot_date       TEXT    NOT NULL CHECK (snapshot_date GLOB
                            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    underlying_conid    INTEGER NOT NULL REFERENCES instruments (conid) ON DELETE RESTRICT,
    expiry              TEXT    NOT NULL CHECK (expiry GLOB
                            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    strike              REAL    NOT NULL CHECK (strike > 0),
    "right"             TEXT    NOT NULL CHECK ("right" IN ('C', 'P')),
    option_conid        INTEGER CHECK (option_conid IS NULL OR option_conid > 0),
    option_symbol       TEXT,
    option_exchange     TEXT,
    bid                 REAL    CHECK (bid IS NULL OR bid >= 0),
    ask                 REAL    CHECK (ask IS NULL OR ask >= 0),
    last                REAL    CHECK (last IS NULL OR last >= 0),
    iv                  REAL    CHECK (iv IS NULL OR iv >= 0),
    delta               REAL    CHECK (delta IS NULL OR delta BETWEEN -1 AND 1),
    open_interest       INTEGER CHECK (open_interest IS NULL OR open_interest >= 0),
    volume              INTEGER CHECK (volume IS NULL OR volume >= 0),
    -- The volatility that set this row's strike band, and where it came from.
    sigma_used          REAL    CHECK (sigma_used IS NULL OR sigma_used >= 0),
    sigma_source        TEXT,
    source              TEXT    NOT NULL CHECK (trim(source) <> ''),
    collected_at        TEXT    NOT NULL CHECK (collected_at GLOB
                            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    market_data_status  TEXT    CHECK (market_data_status IS NULL
                            OR market_data_status IN ('realtime', 'delayed', 'frozen', 'none')),
    market_data_raw     TEXT,
    quote_delay_seconds INTEGER CHECK (quote_delay_seconds IS NULL OR quote_delay_seconds >= 0),
    PRIMARY KEY (snapshot_date, underlying_conid, expiry, strike, "right"),
    CHECK (expiry >= snapshot_date),
    CHECK ((sigma_used IS NULL) = (sigma_source IS NULL))
) WITHOUT ROWID;

CREATE INDEX ix_chain_underlying_date ON chain_snapshots (underlying_conid, snapshot_date);
CREATE INDEX ix_chain_expiry ON chain_snapshots (expiry);

-- ───────────────────────────── curves ─────────────────────────────

-- A futures term structure, one row per root and expiry per session. The root is data, not
-- an instrument held in this system, so there is no foreign key.
CREATE TABLE curves (
    snapshot_date       TEXT    NOT NULL CHECK (snapshot_date GLOB
                            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    root                TEXT    NOT NULL CHECK (trim(root) <> ''),
    expiry              TEXT    NOT NULL CHECK (expiry GLOB
                            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    price               REAL    CHECK (price IS NULL OR price >= 0),
    annualised_roll     REAL,
    conid               INTEGER CHECK (conid IS NULL OR conid > 0),
    exchange            TEXT,
    currency            TEXT    CHECK (currency IS NULL OR currency GLOB '[A-Z][A-Z][A-Z]'),
    source              TEXT    NOT NULL CHECK (trim(source) <> ''),
    collected_at        TEXT    NOT NULL CHECK (collected_at GLOB
                            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    market_data_status  TEXT    CHECK (market_data_status IS NULL
                            OR market_data_status IN ('realtime', 'delayed', 'frozen', 'none')),
    market_data_raw     TEXT,
    quote_delay_seconds INTEGER CHECK (quote_delay_seconds IS NULL OR quote_delay_seconds >= 0),
    PRIMARY KEY (snapshot_date, root, expiry),
    CHECK (expiry >= snapshot_date)
) WITHOUT ROWID;

CREATE INDEX ix_curves_root_date ON curves (root, snapshot_date);

-- ───────────────────────────── macro ─────────────────────────────

-- reference_date is the period an observation describes; release_date is when it became
-- public. Readers align on release_date: aligning a CPI print to the month it describes would
-- let every number derived from it know the figure weeks before the market did. One value is
-- kept per period; a re-pull overwrites it.
CREATE TABLE macro (
    series_id      TEXT NOT NULL CHECK (trim(series_id) <> ''),
    source         TEXT NOT NULL CHECK (trim(source) <> ''),
    reference_date TEXT NOT NULL CHECK (reference_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    release_date   TEXT CHECK (release_date IS NULL OR release_date GLOB
                       '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    value          REAL NOT NULL,
    frequency      TEXT,
    units          TEXT,
    collected_at   TEXT NOT NULL CHECK (collected_at GLOB
                       '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (series_id, source, reference_date),
    CHECK (release_date IS NULL OR release_date >= reference_date)
) WITHOUT ROWID;

CREATE INDEX ix_macro_release ON macro (series_id, release_date);

-- ───────────────────────────── runs ─────────────────────────────

-- A multi-step run (a daily collection, say) and what was planned for it. steps_planned is
-- written on entry, before any step runs, so a run killed on its first step still says how
-- much work it meant to do. The run id is the caller's identifier (a uuid7).
CREATE TABLE runs (
    run_id          TEXT    PRIMARY KEY CHECK (trim(run_id) <> ''),
    command         TEXT    NOT NULL CHECK (trim(command) <> ''),
    session_date    TEXT    NOT NULL CHECK (session_date GLOB
                                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    started_at      TEXT    NOT NULL CHECK (started_at GLOB
                                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    finished_at     TEXT    CHECK (finished_at IS NULL OR finished_at GLOB
                                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    -- interrupted: the process never reported a verdict at all (reaped by the next run).
    status          TEXT    NOT NULL CHECK (status IN (
                                'running', 'ok', 'partial', 'degraded', 'skipped', 'error',
                                'interrupted')),
    steps_planned   INTEGER NOT NULL CHECK (steps_planned >= 0),
    steps_completed INTEGER NOT NULL DEFAULT 0 CHECK (steps_completed >= 0),
    exit_code       INTEGER,
    detail          TEXT,
    CHECK (steps_completed <= steps_planned),
    CHECK (finished_at IS NULL OR finished_at >= started_at),
    CHECK (status = 'running' OR finished_at IS NOT NULL)
) WITHOUT ROWID;

CREATE INDEX ix_runs_session ON runs (session_date, started_at);

-- One step of work. run_id is NULL for a step invoked on its own: a standalone step is not
-- half of a run, and a NULL says so rather than inventing a run of one.
CREATE TABLE run_steps (
    step_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT    REFERENCES runs (run_id) ON DELETE RESTRICT,
    step_index     INTEGER CHECK (step_index IS NULL OR step_index >= 0),
    collector      TEXT    NOT NULL CHECK (trim(collector) <> ''),
    started_at     TEXT    NOT NULL CHECK (started_at GLOB
                               '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    finished_at    TEXT    CHECK (finished_at IS NULL OR finished_at GLOB
                               '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    status         TEXT    NOT NULL CHECK (status IN (
                               'running', 'ok', 'partial', 'degraded', 'skipped', 'error',
                               'interrupted')),
    rows_written   INTEGER NOT NULL DEFAULT 0 CHECK (rows_written >= 0),
    targets_ok     INTEGER NOT NULL DEFAULT 0 CHECK (targets_ok >= 0),
    targets_failed INTEGER NOT NULL DEFAULT 0 CHECK (targets_failed >= 0),
    detail         TEXT,
    CHECK ((run_id IS NULL) = (step_index IS NULL)),
    CHECK (finished_at IS NULL OR finished_at >= started_at),
    CHECK (status = 'running' OR finished_at IS NOT NULL)
);

CREATE INDEX ix_run_steps_collector ON run_steps (collector, started_at);
CREATE UNIQUE INDEX ux_run_steps_position ON run_steps (run_id, step_index)
    WHERE run_id IS NOT NULL;
