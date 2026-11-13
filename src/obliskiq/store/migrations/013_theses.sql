-- 013_theses — the thesis book (MASTER_PLAN §5.5, DECISIONS D156): written ideas, their
-- append-only log and the grade of each closed one.
--
-- Forward-only. Conventions as in 001_core and 002_orders (prices and money as TEXT holding a
-- Decimal's exact digits, dates 'YYYY-MM-DD', stamps 'YYYY-MM-DDTHH:MM:SSZ').

-- ───────────────────────────── theses ─────────────────────────────

-- One written idea. Its text (the expectation) and its triggers (the instrument, the target, the
-- invalidation, the horizon and the entry rule) change only while it is a candidate: a stock
-- candidate is pinned to its conid then. From its approval they never change again; the mode and
-- the size are set once, with the approval. An ended thesis never changes, and none is deleted.
CREATE TABLE theses (
    thesis_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source           TEXT    NOT NULL CHECK (source IN ('owner', 'analyst')),
    conviction       TEXT    NOT NULL CHECK (conviction IN ('normal', 'high', 'max')),
    view             TEXT    NOT NULL CHECK (view IN ('bullish', 'bearish')),
    symbol           TEXT    NOT NULL CHECK (trim(symbol) <> ''),
    candidate_key    TEXT    CHECK (candidate_key IS NULL OR trim(candidate_key) <> ''),
    conid            INTEGER CHECK (conid IS NULL OR conid > 0),
    listing_exchange TEXT,
    currency         TEXT    CHECK (currency IS NULL OR currency GLOB '[A-Z][A-Z][A-Z]'),
    asset_class      TEXT    NOT NULL CHECK (trim(asset_class) <> ''),
    description      TEXT    NOT NULL CHECK (trim(description) <> ''),
    sector           TEXT,
    country          TEXT    CHECK (country IS NULL OR country GLOB '[A-Z][A-Z]'),
    defensive        INTEGER NOT NULL CHECK (defensive IN (0, 1)),
    isin             TEXT,
    expectation      TEXT    NOT NULL CHECK (trim(expectation) <> ''),
    target           TEXT    NOT NULL CHECK (CAST(target AS REAL) > 0),
    invalidation     TEXT    NOT NULL CHECK (CAST(invalidation AS REAL) > 0),
    horizon          TEXT    NOT NULL CHECK (horizon GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    entry_max        TEXT    CHECK (entry_max IS NULL OR CAST(entry_max AS REAL) > 0),
    enter_by         TEXT    NOT NULL CHECK (enter_by GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    state            TEXT    NOT NULL CHECK (state IN ('candidate', 'approved', 'active', 'closed',
                         'refused', 'withdrawn', 'expired')),
    mode             TEXT    CHECK (mode IS NULL OR mode IN ('shadow', 'paper', 'live-small', 'live')),
    size_base        TEXT    CHECK (size_base IS NULL OR CAST(size_base AS REAL) > 0),
    exit_reason      TEXT,
    created_at       TEXT    NOT NULL CHECK (created_at GLOB
                         '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    approved_at      TEXT    CHECK (approved_at IS NULL OR approved_at GLOB
                         '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    opened_at        TEXT    CHECK (opened_at IS NULL OR opened_at GLOB
                         '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    closed_at        TEXT    CHECK (closed_at IS NULL OR closed_at GLOB
                         '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    CHECK (CAST(target AS REAL) > CAST(invalidation AS REAL)),
    CHECK ((mode IS NULL) = (size_base IS NULL) AND (mode IS NULL) = (approved_at IS NULL)),
    CHECK (state NOT IN ('approved', 'active', 'closed') OR approved_at IS NOT NULL),
    CHECK (state NOT IN ('approved', 'active', 'closed') OR conid IS NOT NULL)
);

CREATE INDEX ix_theses_state ON theses (state, thesis_id);

CREATE TRIGGER theses_identity_fixed BEFORE UPDATE OF thesis_id, source, created_at ON theses
BEGIN
    SELECT RAISE(ABORT, 'a thesis''s identity never changes');
END;

CREATE TRIGGER theses_text_fixed BEFORE UPDATE OF conviction, view, symbol, candidate_key, conid,
    listing_exchange, currency, asset_class, description, sector, country, defensive, isin,
    expectation, target, invalidation, horizon, entry_max, enter_by ON theses
WHEN OLD.state <> 'candidate'
BEGIN
    SELECT RAISE(ABORT, 'an approved thesis''s text and triggers never change');
END;

CREATE TRIGGER theses_approved_once BEFORE UPDATE OF mode, size_base, approved_at ON theses
WHEN OLD.approved_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'a thesis is approved once: its mode and size never change');
END;

CREATE TRIGGER theses_ended_once BEFORE UPDATE ON theses
WHEN OLD.state IN ('closed', 'refused', 'withdrawn', 'expired')
BEGIN
    SELECT RAISE(ABORT, 'an ended thesis never changes');
END;

CREATE TRIGGER theses_no_delete BEFORE DELETE ON theses
BEGIN
    SELECT RAISE(ABORT, 'a thesis is never deleted');
END;

-- ───────────────────────────── the log ─────────────────────────────

-- Everything that happened to a thesis, in order: its intake, a pin, why it was withheld, each
-- change of state with its reason codes, each order, and every re-underwrite. Append-only.
CREATE TABLE thesis_log (
    thesis_id INTEGER NOT NULL REFERENCES theses (thesis_id),
    seq       INTEGER NOT NULL CHECK (seq >= 1),
    at        TEXT    NOT NULL CHECK (at GLOB
                  '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    actor     TEXT    NOT NULL CHECK (trim(actor) <> ''),
    kind      TEXT    NOT NULL CHECK (kind IN ('intake', 'resolved', 'withheld', 'state', 'order',
                  'reunderwrite')),
    detail    TEXT    NOT NULL CHECK (trim(detail) <> ''),
    PRIMARY KEY (thesis_id, seq)
) WITHOUT ROWID;

CREATE TRIGGER thesis_log_no_update BEFORE UPDATE ON thesis_log
BEGIN
    SELECT RAISE(ABORT, 'thesis_log is append-only');
END;

CREATE TRIGGER thesis_log_no_delete BEFORE DELETE ON thesis_log
BEGIN
    SELECT RAISE(ABORT, 'thesis_log is append-only');
END;

-- ───────────────────────────── grades ─────────────────────────────

-- A closed thesis against what it wrote. One row per thesis, written when it closes.
CREATE TABLE thesis_grades (
    thesis_id     INTEGER PRIMARY KEY REFERENCES theses (thesis_id),
    graded_at     TEXT    NOT NULL CHECK (graded_at GLOB
                      '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    mode          TEXT    NOT NULL CHECK (mode IN ('shadow', 'paper', 'live-small', 'live')),
    source        TEXT    NOT NULL CHECK (source IN ('owner', 'analyst')),
    conviction    TEXT    NOT NULL CHECK (conviction IN ('normal', 'high', 'max')),
    exit_reason   TEXT    NOT NULL CHECK (trim(exit_reason) <> ''),
    quantity      TEXT    NOT NULL CHECK (CAST(quantity AS REAL) > 0),
    entry_price   TEXT    NOT NULL CHECK (CAST(entry_price AS REAL) > 0),
    exit_price    TEXT    NOT NULL CHECK (CAST(exit_price AS REAL) > 0),
    reached       INTEGER NOT NULL CHECK (reached IN (0, 1)),
    payoff        TEXT    NOT NULL CHECK (trim(payoff) <> ''),
    stated_payoff TEXT    NOT NULL CHECK (trim(stated_payoff) <> ''),
    days_held     INTEGER NOT NULL CHECK (days_held >= 0),
    horizon_days  INTEGER NOT NULL CHECK (horizon_days >= 0)
);

CREATE TRIGGER thesis_grades_no_update BEFORE UPDATE ON thesis_grades
BEGIN
    SELECT RAISE(ABORT, 'a grade is written once');
END;

CREATE TRIGGER thesis_grades_no_delete BEFORE DELETE ON thesis_grades
BEGIN
    SELECT RAISE(ABORT, 'a grade is never deleted');
END;
