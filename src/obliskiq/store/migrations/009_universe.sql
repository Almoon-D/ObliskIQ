-- 009_universe — what the universe's ingestion refused, the issuers' holdings files, the market
-- scanner's rows, stock candidates' resolutions, and the multiplier of a recorded option.
--
-- Forward-only. Dates are 'YYYY-MM-DD' and stamps 'YYYY-MM-DDTHH:MM:SSZ', checked as in 001_core.

-- ───────────────────────────── universe_rejections ─────────────────────────────

-- An instrument refused at ingestion (a leveraged or inverse product by its resolved
-- description), recorded so an exclusion is auditable rather than a row that quietly never
-- appeared. origin: declared (config/universe.yaml), holdings (an issuer's file), scanner.
CREATE TABLE universe_rejections (
    rejected_at      TEXT NOT NULL CHECK (rejected_at GLOB
                         '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    symbol           TEXT NOT NULL CHECK (trim(symbol) <> ''),
    origin           TEXT NOT NULL CHECK (origin IN ('declared', 'holdings', 'scanner')),
    listing_exchange TEXT,
    conid            INTEGER CHECK (conid IS NULL OR conid > 0),
    description      TEXT,
    reason           TEXT NOT NULL CHECK (trim(reason) <> ''),
    PRIMARY KEY (rejected_at, symbol, origin)
) WITHOUT ROWID;

-- ───────────────────────────── holdings ─────────────────────────────

-- One issuer holdings file as it was downloaded. as_of is the date the file says its holdings
-- are of; a stock enters a cell on that date, never on the download's.
CREATE TABLE holdings_files (
    fund_symbol    TEXT    NOT NULL CHECK (trim(fund_symbol) <> ''),
    as_of          TEXT    NOT NULL CHECK (as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    issuer         TEXT    NOT NULL CHECK (trim(issuer) <> ''),
    url            TEXT    NOT NULL CHECK (trim(url) <> ''),
    downloaded_at  TEXT    NOT NULL CHECK (downloaded_at GLOB
                               '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    content_sha256 TEXT    NOT NULL CHECK (length(content_sha256) = 64),
    rows           INTEGER NOT NULL CHECK (rows >= 0),
    PRIMARY KEY (fund_symbol, as_of)
) WITHOUT ROWID;

-- One line of a holdings file. The issuer's own sector, location and exchange are kept beside
-- what they map to (NULL where the map has no entry, never a guess). weight is a fraction of the
-- fund; candidate_key groups the listings of one company (NULL for a line that is not a share).
CREATE TABLE holdings (
    fund_symbol    TEXT    NOT NULL,
    as_of          TEXT    NOT NULL,
    line           INTEGER NOT NULL CHECK (line >= 0),
    ticker         TEXT    NOT NULL CHECK (trim(ticker) <> ''),
    name           TEXT    NOT NULL CHECK (trim(name) <> ''),
    asset_class    TEXT    NOT NULL CHECK (trim(asset_class) <> ''),
    issuer_sector  TEXT,
    sector         TEXT,
    location       TEXT,
    country        TEXT    CHECK (country IS NULL OR country GLOB '[A-Z][A-Z]'),
    exchange       TEXT,
    ibkr_exchange  TEXT,
    currency       TEXT    CHECK (currency IS NULL OR currency GLOB '[A-Z][A-Z][A-Z]'),
    weight         REAL    CHECK (weight IS NULL OR (weight >= -1 AND weight <= 1)),
    candidate_key  TEXT,
    PRIMARY KEY (fund_symbol, as_of, line),
    FOREIGN KEY (fund_symbol, as_of) REFERENCES holdings_files (fund_symbol, as_of)
        ON DELETE RESTRICT
) WITHOUT ROWID;

CREATE INDEX ix_holdings_candidate ON holdings (candidate_key, as_of)
    WHERE candidate_key IS NOT NULL;

-- ───────────────────────────── scanner_rows ─────────────────────────────

-- One contract an IBKR market-scanner run returned, ranked as returned.
CREATE TABLE scanner_rows (
    scanned_on       TEXT    NOT NULL CHECK (scanned_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    location         TEXT    NOT NULL CHECK (trim(location) <> ''),
    scan_type        TEXT    NOT NULL CHECK (trim(scan_type) <> ''),
    rank             INTEGER NOT NULL CHECK (rank >= 0),
    market           TEXT    NOT NULL CHECK (trim(market) <> ''),
    conid            INTEGER NOT NULL CHECK (conid > 0),
    symbol           TEXT,
    listing_exchange TEXT,
    company_name     TEXT,
    candidate_key    TEXT    NOT NULL CHECK (trim(candidate_key) <> ''),
    scanned_at       TEXT    NOT NULL CHECK (scanned_at GLOB
                                 '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (scanned_on, location, scan_type, rank)
) WITHOUT ROWID;

-- ───────────────────────────── candidate_resolutions ─────────────────────────────

-- A stock candidate resolved through IBKR's contract search on demand: the conid, or why none
-- (failure). pinned: a thesis needs it, so it is kept resolved and collected.
CREATE TABLE candidate_resolutions (
    candidate_key    TEXT    PRIMARY KEY CHECK (trim(candidate_key) <> ''),
    resolved_at      TEXT    NOT NULL CHECK (resolved_at GLOB
                                 '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    conid            INTEGER CHECK (conid IS NULL OR conid > 0),
    symbol           TEXT,
    listing_exchange TEXT,
    currency         TEXT    CHECK (currency IS NULL OR currency GLOB '[A-Z][A-Z][A-Z]'),
    pinned           INTEGER NOT NULL CHECK (pinned IN (0, 1)),
    pinned_for       TEXT,
    failure          TEXT,
    CHECK ((conid IS NULL) = (failure IS NOT NULL)),
    CHECK (pinned = 0 OR conid IS NOT NULL)
) WITHOUT ROWID;

-- ───────────────────────────── chain_snapshots.multiplier ─────────────────────────────

-- What one option contract delivers per unit of its price, as IBKR's contract details state it;
-- NULL when they did not. An option without one is never described to the order path.
ALTER TABLE chain_snapshots ADD COLUMN multiplier REAL CHECK (multiplier IS NULL OR multiplier > 0);
