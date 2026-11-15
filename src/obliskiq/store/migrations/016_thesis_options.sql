-- 016_thesis_options — a thesis held as a long call or a long put (MASTER_PLAN §5.5, §5.6;
-- DECISIONS D157, D167): what a thesis may be held as and what it is held as, the option chosen
-- for it with its twin in the underlying, and what a grade was the grade of.
--
-- Forward-only. SQLite cannot change a CHECK in place, so theses is rebuilt (as 012 rebuilt
-- measurements): a thesis whose levels describe a fall is now a row, held as a long put or as
-- nothing. Conventions as in 013_theses.

-- ───────────────────────────── theses ─────────────────────────────

-- As 013_theses, with two columns. expression is what the idea may be held as: 'shares' (its
-- instrument), 'option' (a long call, or a long put when its levels describe a fall) or 'auto'
-- (the option when the options sleeve's gates pass, otherwise the instrument). held_as is what
-- its approval fixed. A target below the invalidation is a fall of the instrument: only a
-- bearish idea writes one, it is never held as shares (the book never shorts) and it names no
-- entry limit.
CREATE TABLE theses_new (
    thesis_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source           TEXT    NOT NULL CHECK (source IN ('owner', 'analyst')),
    conviction       TEXT    NOT NULL CHECK (conviction IN ('normal', 'high', 'max')),
    view             TEXT    NOT NULL CHECK (view IN ('bullish', 'bearish')),
    expression       TEXT    NOT NULL CHECK (expression IN ('shares', 'option', 'auto')),
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
    held_as          TEXT    CHECK (held_as IS NULL OR held_as IN ('shares', 'option')),
    exit_reason      TEXT,
    created_at       TEXT    NOT NULL CHECK (created_at GLOB
                         '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    approved_at      TEXT    CHECK (approved_at IS NULL OR approved_at GLOB
                         '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    opened_at        TEXT    CHECK (opened_at IS NULL OR opened_at GLOB
                         '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    closed_at        TEXT    CHECK (closed_at IS NULL OR closed_at GLOB
                         '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    CHECK (CAST(target AS REAL) <> CAST(invalidation AS REAL)),
    CHECK (CAST(target AS REAL) > CAST(invalidation AS REAL)
           OR (view = 'bearish' AND expression <> 'shares' AND entry_max IS NULL)),
    CHECK ((mode IS NULL) = (size_base IS NULL) AND (mode IS NULL) = (approved_at IS NULL)
           AND (mode IS NULL) = (held_as IS NULL)),
    CHECK (held_as IS NULL OR expression = 'auto' OR held_as = expression),
    CHECK (held_as IS NOT 'shares' OR CAST(target AS REAL) > CAST(invalidation AS REAL)),
    CHECK (state NOT IN ('approved', 'active', 'closed') OR approved_at IS NOT NULL),
    CHECK (state NOT IN ('approved', 'active', 'closed') OR conid IS NOT NULL)
);


INSERT INTO theses_new (thesis_id, source, conviction, view, symbol, candidate_key, conid, listing_exchange, currency, asset_class, description, sector, country, defensive, isin, expectation, target, invalidation, horizon, entry_max, enter_by, state, mode, size_base, exit_reason, created_at, approved_at, opened_at, closed_at, expression, held_as)
SELECT thesis_id, source, conviction, view, symbol, candidate_key, conid, listing_exchange, currency, asset_class, description, sector, country, defensive, isin, expectation, target, invalidation, horizon, entry_max, enter_by, state, mode, size_base, exit_reason, created_at, approved_at, opened_at, closed_at, 'shares', CASE WHEN mode IS NULL THEN NULL ELSE 'shares' END FROM theses;

-- Dropping the table drops its index and its triggers; both are made again on the rebuilt table.
DROP TABLE theses;
ALTER TABLE theses_new RENAME TO theses;

CREATE INDEX ix_theses_state ON theses (state, thesis_id);

CREATE TRIGGER theses_identity_fixed BEFORE UPDATE OF thesis_id, source, created_at ON theses
BEGIN
    SELECT RAISE(ABORT, 'a thesis''s identity never changes');
END;

CREATE TRIGGER theses_text_fixed BEFORE UPDATE OF conviction, view, expression, symbol, candidate_key, conid,
    listing_exchange, currency, asset_class, description, sector, country, defensive, isin,
    expectation, target, invalidation, horizon, entry_max, enter_by ON theses
WHEN OLD.state <> 'candidate'
BEGIN
    SELECT RAISE(ABORT, 'an approved thesis''s text and triggers never change');
END;

CREATE TRIGGER theses_approved_once BEFORE UPDATE OF mode, size_base, held_as, approved_at ON theses
WHEN OLD.approved_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'a thesis is approved once: its mode, its size and what it is held as never change');
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


-- ───────────────────────────── grades ─────────────────────────────

-- What the graded position was: the instrument, or an option on it (its payoff is then the
-- return on the premium paid).
ALTER TABLE thesis_grades ADD COLUMN held_as TEXT NOT NULL DEFAULT 'shares'
    CHECK (held_as IN ('shares', 'option'));

-- ───────────────────────────── the option and its twin ─────────────────────────────

-- The option a thesis is held as, chosen at its approval (one block, MASTER_PLAN §5.6), and its
-- twin: the same thesis in its underlying at the book's base size, entered at underlying_entry
-- (the underlying's price when the option was chosen). The twin is a figure, never a position:
-- it leaves by the thesis's own exits and is graded beside the option (H8's forward record).
-- position_id is set once, when the block's fills open the sleeve's position; the twin closes
-- once; nothing else changes and no row is deleted.
CREATE TABLE thesis_options (
    thesis_id            INTEGER PRIMARY KEY REFERENCES theses (thesis_id),
    option_conid         INTEGER NOT NULL CHECK (option_conid > 0),
    underlying_conid     INTEGER NOT NULL CHECK (underlying_conid > 0),
    symbol               TEXT    NOT NULL CHECK (trim(symbol) <> ''),
    exchange             TEXT    NOT NULL CHECK (trim(exchange) <> ''),
    description          TEXT    NOT NULL CHECK (trim(description) <> ''),
    currency             TEXT    NOT NULL CHECK (currency GLOB '[A-Z][A-Z][A-Z]'),
    expiry               TEXT    NOT NULL CHECK (expiry GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    strike               REAL    NOT NULL CHECK (strike > 0),
    "right"              TEXT    NOT NULL CHECK ("right" IN ('C', 'P')),
    multiplier           REAL    NOT NULL CHECK (multiplier > 0),
    contracts            INTEGER NOT NULL CHECK (contracts > 0),
    close_by             TEXT    NOT NULL CHECK (close_by GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    prob_target_at_entry REAL    CHECK (prob_target_at_entry IS NULL
                             OR (prob_target_at_entry >= 0 AND prob_target_at_entry <= 1)),
    underlying_entry     TEXT    NOT NULL CHECK (CAST(underlying_entry AS REAL) > 0),
    twin_size_base       TEXT    NOT NULL CHECK (CAST(twin_size_base AS REAL) > 0),
    position_id          INTEGER UNIQUE REFERENCES option_positions (position_id),
    twin_exit            TEXT    CHECK (twin_exit IS NULL OR CAST(twin_exit AS REAL) > 0),
    twin_exit_reason     TEXT    CHECK (twin_exit_reason IS NULL OR trim(twin_exit_reason) <> ''),
    twin_closed_at       TEXT    CHECK (twin_closed_at IS NULL OR twin_closed_at GLOB
                             '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    CHECK (close_by <= expiry),
    CHECK ((twin_exit IS NULL) = (twin_exit_reason IS NULL)
           AND (twin_exit IS NULL) = (twin_closed_at IS NULL))
);

CREATE TRIGGER thesis_options_terms_fixed BEFORE UPDATE OF thesis_id, option_conid,
    underlying_conid, symbol, exchange, description, currency, expiry, strike, "right", multiplier,
    contracts, close_by, prob_target_at_entry, underlying_entry, twin_size_base ON thesis_options
BEGIN
    SELECT RAISE(ABORT, 'the option chosen for a thesis never changes');
END;

CREATE TRIGGER thesis_options_position_once BEFORE UPDATE OF position_id ON thesis_options
WHEN OLD.position_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'a thesis''s option position is opened once');
END;

CREATE TRIGGER thesis_options_twin_once BEFORE UPDATE OF twin_exit, twin_exit_reason,
    twin_closed_at ON thesis_options
WHEN OLD.twin_exit IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'a twin closes once');
END;

CREATE TRIGGER thesis_options_no_delete BEFORE DELETE ON thesis_options
BEGIN
    SELECT RAISE(ABORT, 'a thesis''s option is never deleted');
END;
