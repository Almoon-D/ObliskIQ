-- 019_stocks — the thesis book's stock screen, collected forward (MASTER_PLAN §5.5; DECISIONS
-- D180): every snapshot of the SEC's listings and the names that left them, each company's SIC,
-- the XBRL frame values the screen reads as they were read, and Yahoo's daily bars.
--
-- Forward-only. Dates are 'YYYY-MM-DD' and stamps 'YYYY-MM-DDTHH:MM:SSZ', checked as in 001_core.
-- Every table is a record: append-only, enforced by trigger. A read as of a date sees the rows
-- whose known_at falls before its end.

-- ───────────────────────────── listings ─────────────────────────────

-- One read of company_tickers_exchange.json: when, how many names it kept, what was downloaded.
CREATE TABLE stock_snapshots (
    snapshot_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    known_at       TEXT    NOT NULL UNIQUE CHECK (known_at GLOB
                       '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    listed         INTEGER NOT NULL CHECK (listed > 0),
    content_sha256 TEXT    NOT NULL CHECK (length(content_sha256) = 64)
);

-- Every name each snapshot kept: a snapshot is read back whole.
CREATE TABLE stock_members (
    snapshot_id INTEGER NOT NULL REFERENCES stock_snapshots (snapshot_id) ON DELETE RESTRICT,
    ticker      TEXT    NOT NULL CHECK (trim(ticker) <> ''),
    cik         INTEGER NOT NULL CHECK (cik > 0),
    PRIMARY KEY (snapshot_id, ticker, cik)
) WITHOUT ROWID;

-- A name's words, as the first snapshot that listed it gave them.
CREATE TABLE stock_names (
    ticker   TEXT    NOT NULL CHECK (trim(ticker) <> ''),
    cik      INTEGER NOT NULL CHECK (cik > 0),
    name     TEXT    NOT NULL CHECK (trim(name) <> ''),
    exchange TEXT    NOT NULL CHECK (trim(exchange) <> ''),
    venue    TEXT    NOT NULL CHECK (trim(venue) <> ''),
    known_at TEXT    NOT NULL CHECK (known_at GLOB
                 '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (ticker, cik)
) WITHOUT ROWID;

-- A name in one snapshot and absent from the next: left_listing, with both snapshots.
CREATE TABLE stock_left (
    ticker        TEXT    NOT NULL CHECK (trim(ticker) <> ''),
    cik           INTEGER NOT NULL CHECK (cik > 0),
    last_snapshot INTEGER NOT NULL REFERENCES stock_snapshots (snapshot_id) ON DELETE RESTRICT,
    left_snapshot INTEGER NOT NULL REFERENCES stock_snapshots (snapshot_id) ON DELETE RESTRICT,
    CHECK (last_snapshot < left_snapshot),
    PRIMARY KEY (ticker, cik, left_snapshot)
) WITHOUT ROWID;

-- Each company's SIC as its submissions first gave it (NULL: the SEC states none).
CREATE TABLE stock_companies (
    cik      INTEGER PRIMARY KEY CHECK (cik > 0),
    sic      INTEGER CHECK (sic IS NULL OR sic BETWEEN 0 AND 9999),
    known_at TEXT    NOT NULL CHECK (known_at GLOB
                 '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
);

-- ───────────────────────────── fundamentals ─────────────────────────────

-- A frame's value for one company as read; a later read that differs is a new row.
CREATE TABLE stock_facts (
    cik        INTEGER NOT NULL CHECK (cik > 0),
    input      TEXT    NOT NULL CHECK (trim(input) <> ''),
    period     TEXT    NOT NULL CHECK (trim(period) <> ''),
    period_end TEXT    NOT NULL CHECK (period_end GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    value      REAL    NOT NULL,
    accession  TEXT    NOT NULL CHECK (trim(accession) <> ''),
    known_at   TEXT    NOT NULL CHECK (known_at GLOB
                   '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (cik, input, period, known_at)
) WITHOUT ROWID;

-- ───────────────────────────── bars ─────────────────────────────

-- A session as Yahoo served it the first time it was stored.
CREATE TABLE stock_bars (
    symbol   TEXT NOT NULL CHECK (trim(symbol) <> ''),
    session  TEXT NOT NULL CHECK (session GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    close    REAL NOT NULL CHECK (close > 0),
    volume   REAL CHECK (volume IS NULL OR volume >= 0),
    change   REAL CHECK (change IS NULL OR change > -1),
    known_at TEXT NOT NULL CHECK (known_at GLOB
                 '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (symbol, session)
) WITHOUT ROWID;

CREATE INDEX ix_stock_bars_session ON stock_bars (session, symbol);

-- ───────────────────────────── append-only ─────────────────────────────

CREATE TRIGGER stock_snapshots_no_update BEFORE UPDATE ON stock_snapshots
BEGIN SELECT RAISE(ABORT, 'stock_snapshots is append-only'); END;
CREATE TRIGGER stock_snapshots_no_delete BEFORE DELETE ON stock_snapshots
BEGIN SELECT RAISE(ABORT, 'stock_snapshots is append-only'); END;
CREATE TRIGGER stock_members_no_update BEFORE UPDATE ON stock_members
BEGIN SELECT RAISE(ABORT, 'stock_members is append-only'); END;
CREATE TRIGGER stock_members_no_delete BEFORE DELETE ON stock_members
BEGIN SELECT RAISE(ABORT, 'stock_members is append-only'); END;
CREATE TRIGGER stock_names_no_update BEFORE UPDATE ON stock_names
BEGIN SELECT RAISE(ABORT, 'stock_names is append-only'); END;
CREATE TRIGGER stock_names_no_delete BEFORE DELETE ON stock_names
BEGIN SELECT RAISE(ABORT, 'stock_names is append-only'); END;
CREATE TRIGGER stock_left_no_update BEFORE UPDATE ON stock_left
BEGIN SELECT RAISE(ABORT, 'stock_left is append-only'); END;
CREATE TRIGGER stock_left_no_delete BEFORE DELETE ON stock_left
BEGIN SELECT RAISE(ABORT, 'stock_left is append-only'); END;
CREATE TRIGGER stock_companies_no_update BEFORE UPDATE ON stock_companies
BEGIN SELECT RAISE(ABORT, 'stock_companies is append-only'); END;
CREATE TRIGGER stock_companies_no_delete BEFORE DELETE ON stock_companies
BEGIN SELECT RAISE(ABORT, 'stock_companies is append-only'); END;
CREATE TRIGGER stock_facts_no_update BEFORE UPDATE ON stock_facts
BEGIN SELECT RAISE(ABORT, 'stock_facts is append-only'); END;
CREATE TRIGGER stock_facts_no_delete BEFORE DELETE ON stock_facts
BEGIN SELECT RAISE(ABORT, 'stock_facts is append-only'); END;
CREATE TRIGGER stock_bars_no_update BEFORE UPDATE ON stock_bars
BEGIN SELECT RAISE(ABORT, 'stock_bars is append-only'); END;
CREATE TRIGGER stock_bars_no_delete BEFORE DELETE ON stock_bars
BEGIN SELECT RAISE(ABORT, 'stock_bars is append-only'); END;
