-- 010_sources — the sources besides IBKR: macro observations that may be released before the
-- period they describe ends, CFTC positioning, SEC Form 4 filings, prediction-market snapshots,
-- GDELT's daily article counts, news items and what the LLM extracted from them, earnings dates,
-- the halt feed's last good state and each source's health.
--
-- Forward-only. Dates are 'YYYY-MM-DD' and stamps 'YYYY-MM-DDTHH:MM:SSZ', checked as in 001_core.

-- ───────────────────────────── macro ─────────────────────────────

-- 001's macro table refused a release before the reference date, which a flash estimate dated
-- at its period's end, or a policy rate announced before the day it applies, legitimately has.
-- Readers still align on release_date <= as_of; the reference date only orders a series.
CREATE TABLE macro_released (
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
    PRIMARY KEY (series_id, source, reference_date)
) WITHOUT ROWID;

INSERT INTO macro_released
    (series_id, source, reference_date, release_date, value, frequency, units, collected_at)
SELECT series_id, source, reference_date, release_date, value, frequency, units, collected_at
FROM macro;

DROP TABLE macro;
ALTER TABLE macro_released RENAME TO macro;
CREATE INDEX ix_macro_release ON macro (series_id, release_date);

-- ───────────────────────────── positioning ─────────────────────────────

-- One weekly CFTC report of one contract (legacy futures-only). report_date is the Tuesday the
-- positions are of; release_date is when the report was public (the source's release rule).
-- A leg the report leaves blank is NULL, never zero.
CREATE TABLE cot_reports (
    contract_code TEXT    NOT NULL CHECK (length(contract_code) = 6),
    report_date   TEXT    NOT NULL CHECK (report_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    release_date  TEXT    NOT NULL CHECK (release_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    market_name   TEXT    NOT NULL CHECK (trim(market_name) <> ''),
    open_interest INTEGER NOT NULL CHECK (open_interest > 0),
    noncomm_long  INTEGER CHECK (noncomm_long IS NULL OR noncomm_long >= 0),
    noncomm_short INTEGER CHECK (noncomm_short IS NULL OR noncomm_short >= 0),
    comm_long     INTEGER CHECK (comm_long IS NULL OR comm_long >= 0),
    comm_short    INTEGER CHECK (comm_short IS NULL OR comm_short >= 0),
    nonrept_long  INTEGER CHECK (nonrept_long IS NULL OR nonrept_long >= 0),
    nonrept_short INTEGER CHECK (nonrept_short IS NULL OR nonrept_short >= 0),
    source        TEXT    NOT NULL CHECK (source IN ('cftc_annual', 'cftc_socrata')),
    collected_at  TEXT    NOT NULL CHECK (collected_at GLOB
                              '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (contract_code, report_date),
    CHECK (release_date >= report_date)
) WITHOUT ROWID;

CREATE INDEX ix_cot_release ON cot_reports (contract_code, release_date);

-- ───────────────────────────── insiders ─────────────────────────────

-- One Form 4 filing as EDGAR disseminated it. filed_on is EDGAR's filing date (what the filing
-- is public by); accepted_at its acceptance stamp.
CREATE TABLE insider_filings (
    accession        TEXT    PRIMARY KEY CHECK (accession GLOB
                                 '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]'),
    form             TEXT    NOT NULL CHECK (trim(form) <> ''),
    issuer_cik       INTEGER NOT NULL CHECK (issuer_cik > 0),
    issuer_symbol    TEXT,
    filed_on         TEXT    NOT NULL CHECK (filed_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    accepted_at      TEXT    NOT NULL CHECK (accepted_at GLOB
                                 '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    period_of_report TEXT    CHECK (period_of_report IS NULL OR period_of_report GLOB
                                 '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    collected_at     TEXT    NOT NULL CHECK (collected_at GLOB
                                 '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
) WITHOUT ROWID;

CREATE INDEX ix_insider_filings_issuer ON insider_filings (issuer_cik, filed_on);

-- The reporting owners of a filing and their relationship to the issuer.
CREATE TABLE insider_owners (
    accession          TEXT    NOT NULL REFERENCES insider_filings (accession) ON DELETE RESTRICT,
    owner_cik          INTEGER NOT NULL CHECK (owner_cik > 0),
    owner_name         TEXT    NOT NULL CHECK (trim(owner_name) <> ''),
    is_director        INTEGER NOT NULL CHECK (is_director IN (0, 1)),
    is_officer         INTEGER NOT NULL CHECK (is_officer IN (0, 1)),
    is_ten_percent     INTEGER NOT NULL CHECK (is_ten_percent IN (0, 1)),
    is_other           INTEGER NOT NULL CHECK (is_other IN (0, 1)),
    officer_title      TEXT,
    PRIMARY KEY (accession, owner_cik)
) WITHOUT ROWID;

-- The transactions a filing reports, in its order; derivative ones flagged.
CREATE TABLE insider_transactions (
    accession        TEXT    NOT NULL REFERENCES insider_filings (accession) ON DELETE RESTRICT,
    line             INTEGER NOT NULL CHECK (line >= 0),
    derivative       INTEGER NOT NULL CHECK (derivative IN (0, 1)),
    security_title   TEXT    NOT NULL CHECK (trim(security_title) <> ''),
    transaction_date TEXT    NOT NULL CHECK (transaction_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    code             TEXT    NOT NULL CHECK (trim(code) <> ''),
    shares           REAL    CHECK (shares IS NULL OR shares >= 0),
    price            REAL    CHECK (price IS NULL OR price >= 0),
    acquired         INTEGER CHECK (acquired IS NULL OR acquired IN (0, 1)),
    shares_after     REAL    CHECK (shares_after IS NULL OR shares_after >= 0),
    direct           INTEGER CHECK (direct IS NULL OR direct IN (0, 1)),
    PRIMARY KEY (accession, line)
) WITHOUT ROWID;

-- ───────────────────────────── prediction markets ─────────────────────────────

-- A market seen on a prediction venue: curated (named in sources.yaml) or found by tag.
CREATE TABLE event_markets (
    venue         TEXT    NOT NULL CHECK (venue IN ('polymarket', 'kalshi')),
    market_id     TEXT    NOT NULL CHECK (trim(market_id) <> ''),
    event_id      TEXT,
    question      TEXT    NOT NULL CHECK (trim(question) <> ''),
    outcome       TEXT    NOT NULL CHECK (trim(outcome) <> ''),
    closes_at     TEXT    CHECK (closes_at IS NULL OR closes_at GLOB
                              '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    curated       INTEGER NOT NULL CHECK (curated IN (0, 1)),
    tags          TEXT    NOT NULL,
    first_seen_at TEXT    NOT NULL CHECK (first_seen_at GLOB
                              '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    last_seen_at  TEXT    NOT NULL CHECK (last_seen_at GLOB
                              '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (venue, market_id),
    CHECK (last_seen_at >= first_seen_at)
) WITHOUT ROWID;

-- The probability of the recorded outcome when it was read. Snapshots are never revised.
CREATE TABLE event_snapshots (
    venue         TEXT NOT NULL,
    market_id     TEXT NOT NULL,
    observed_at   TEXT NOT NULL CHECK (observed_at GLOB
                      '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    probability   REAL NOT NULL CHECK (probability >= 0 AND probability <= 1),
    bid           REAL CHECK (bid IS NULL OR (bid >= 0 AND bid <= 1)),
    ask           REAL CHECK (ask IS NULL OR (ask >= 0 AND ask <= 1)),
    last_price    REAL CHECK (last_price IS NULL OR (last_price >= 0 AND last_price <= 1)),
    volume        REAL CHECK (volume IS NULL OR volume >= 0),
    liquidity     REAL CHECK (liquidity IS NULL OR liquidity >= 0),
    PRIMARY KEY (venue, market_id, observed_at),
    FOREIGN KEY (venue, market_id) REFERENCES event_markets (venue, market_id) ON DELETE RESTRICT
) WITHOUT ROWID;

-- ───────────────────────────── GDELT ─────────────────────────────

-- One complete UTC day of one query: matching articles, every article GDELT monitored that day,
-- and the matching articles' mean tone. The first complete reading is kept (GDELT revises
-- counts as it indexes; a later read never replaces what was known).
CREATE TABLE gdelt_days (
    query_id     TEXT    NOT NULL CHECK (trim(query_id) <> ''),
    day          TEXT    NOT NULL CHECK (day GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    articles     INTEGER NOT NULL CHECK (articles >= 0),
    monitored    INTEGER NOT NULL CHECK (monitored > 0),
    tone         REAL,
    collected_at TEXT    NOT NULL CHECK (collected_at GLOB
                             '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (query_id, day)
) WITHOUT ROWID;

-- ───────────────────────────── news ─────────────────────────────

-- One feed item, deduplicated across feeds by its id (the hash of its guid, else its link, else
-- its title and date). raw is the item as received, for the extractor; fallback_tone is VADER's
-- compound score, an offline tone used only where no extraction exists.
CREATE TABLE news_items (
    item_id       TEXT PRIMARY KEY CHECK (length(item_id) = 64),
    feed          TEXT NOT NULL CHECK (trim(feed) <> ''),
    kind          TEXT NOT NULL CHECK (kind IN ('central_bank', 'regulator', 'issuer')),
    region        TEXT,
    symbol        TEXT,
    guid          TEXT,
    link          TEXT,
    title         TEXT NOT NULL CHECK (trim(title) <> ''),
    summary       TEXT,
    published_at  TEXT CHECK (published_at IS NULL OR published_at GLOB
                      '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    collected_at  TEXT NOT NULL CHECK (collected_at GLOB
                      '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    raw           TEXT NOT NULL,
    fallback_tone REAL CHECK (fallback_tone IS NULL OR (fallback_tone >= -1 AND fallback_tone <= 1))
) WITHOUT ROWID;

CREATE INDEX ix_news_collected ON news_items (collected_at);

-- Every call the EventExtractor made for an item (DECISIONS D70): what it produced, when, by
-- which model and prompt, and what it cost. H7's evidence is this series, so it is append-only.
-- status: ok (a validated ExtractedEvent in output), invalid (no valid output after the retry;
-- the item fails closed), failed (the call itself failed; the item stays queued).
CREATE TABLE event_extractions (
    extraction_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id        TEXT    NOT NULL REFERENCES news_items (item_id) ON DELETE RESTRICT,
    produced_at    TEXT    NOT NULL CHECK (produced_at GLOB
                               '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    provider       TEXT    NOT NULL CHECK (trim(provider) <> ''),
    model_id       TEXT    NOT NULL CHECK (trim(model_id) <> ''),
    prompt_version INTEGER NOT NULL CHECK (prompt_version > 0),
    prompt_hash    TEXT    NOT NULL CHECK (length(prompt_hash) = 64),
    status         TEXT    NOT NULL CHECK (status IN ('ok', 'invalid', 'failed')),
    attempts       INTEGER NOT NULL CHECK (attempts >= 0),
    output         TEXT,
    error          TEXT,
    input_tokens   INTEGER NOT NULL CHECK (input_tokens >= 0),
    output_tokens  INTEGER NOT NULL CHECK (output_tokens >= 0),
    cost_usd       REAL    NOT NULL CHECK (cost_usd >= 0),
    CHECK ((status = 'ok') = (output IS NOT NULL)),
    CHECK (status = 'ok' OR error IS NOT NULL)
);

CREATE INDEX ix_extractions_item ON event_extractions (item_id, model_id, prompt_hash);
CREATE INDEX ix_extractions_produced ON event_extractions (produced_at);

CREATE TRIGGER event_extractions_no_update BEFORE UPDATE ON event_extractions
BEGIN
    SELECT RAISE(ABORT, 'event_extractions is append-only');
END;

CREATE TRIGGER event_extractions_no_delete BEFORE DELETE ON event_extractions
BEGIN
    SELECT RAISE(ABORT, 'event_extractions is append-only');
END;

-- ───────────────────────────── earnings ─────────────────────────────

-- The earnings dates one read of a research-grade source listed for a symbol (past reports and
-- the upcoming, possibly estimated, one). A read as of a date uses the newest read held by then.
CREATE TABLE earnings_dates (
    symbol       TEXT NOT NULL CHECK (trim(symbol) <> ''),
    collected_on TEXT NOT NULL CHECK (collected_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    report_at    TEXT NOT NULL CHECK (report_at GLOB
                     '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    source       TEXT NOT NULL CHECK (trim(source) <> ''),
    grade        TEXT NOT NULL CHECK (grade IN ('research')),
    PRIMARY KEY (symbol, collected_on, report_at)
) WITHOUT ROWID;

-- ───────────────────────────── halts ─────────────────────────────

-- The last state a halt feed answered with, so a restart during a feed outage keeps it in force.
CREATE TABLE halt_feed_state (
    origin     TEXT PRIMARY KEY CHECK (trim(origin) <> ''),
    fetched_at TEXT NOT NULL CHECK (fetched_at GLOB
                   '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    incidents  TEXT NOT NULL
) WITHOUT ROWID;

-- ───────────────────────────── source health ─────────────────────────────

-- Each source's last run: when it was attempted, when it last succeeded, its verdict and rows.
CREATE TABLE source_health (
    source               TEXT    PRIMARY KEY CHECK (trim(source) <> ''),
    last_attempt_at      TEXT    NOT NULL CHECK (last_attempt_at GLOB
                                     '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    last_success_at      TEXT    CHECK (last_success_at IS NULL OR last_success_at GLOB
                                     '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    last_status          TEXT    NOT NULL CHECK (last_status IN
                                     ('ok', 'degraded', 'partial', 'error', 'skipped')),
    last_rows            INTEGER NOT NULL CHECK (last_rows >= 0),
    consecutive_failures INTEGER NOT NULL CHECK (consecutive_failures >= 0),
    detail               TEXT,
    CHECK (last_success_at IS NULL OR last_success_at <= last_attempt_at)
) WITHOUT ROWID;

-- Each target of a source (a macro series, a contract, a feed): its last attempt and success, so
-- a repeat run inside the refetch window asks nothing again.
CREATE TABLE source_targets (
    source          TEXT    NOT NULL CHECK (trim(source) <> ''),
    target          TEXT    NOT NULL CHECK (trim(target) <> ''),
    last_attempt_at TEXT    NOT NULL CHECK (last_attempt_at GLOB
                                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    last_success_at TEXT    CHECK (last_success_at IS NULL OR last_success_at GLOB
                                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    last_ok         INTEGER NOT NULL CHECK (last_ok IN (0, 1)),
    last_rows       INTEGER NOT NULL CHECK (last_rows >= 0),
    detail          TEXT,
    PRIMARY KEY (source, target)
) WITHOUT ROWID;
