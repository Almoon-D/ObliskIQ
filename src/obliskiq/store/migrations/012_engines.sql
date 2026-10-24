-- 012_engines — the engines' state: the measurement store's two new entity kinds (a market's
-- four-way FX decomposition, a commodity root's curve and readings), the options sleeve's
-- positions, its penalty ledger and its zombie readings, and the would-be entries the sleeve logs
-- while it runs in shadow (DECISIONS D135-D138).
--
-- Forward-only. SQLite cannot change a CHECK in place, so measurements is rebuilt with the wider
-- entity list and its rows copied (as 005 rebuilt research_series). Conventions as in 001_core and
-- 008_capital (money as TEXT holding a Decimal's exact digits, dates 'YYYY-MM-DD', stamps
-- 'YYYY-MM-DDTHH:MM:SSZ').

-- ───────────────────────────── measurements ─────────────────────────────

-- As 011_measurements, with two more entity kinds: 'market' (a grid market, 'us') and
-- 'commodity_root' (a futures root, 'CL'). The option surface is a leaf set of its underlying,
-- an 'instrument'.
CREATE TABLE measurements_new (
    entity_kind        TEXT    NOT NULL CHECK (entity_kind IN (
                                   'equity_cell', 'macro_cell', 'instrument', 'correlation_pair',
                                   'market', 'commodity_root')),
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

INSERT INTO measurements_new SELECT * FROM measurements;

-- Dropping the table drops its index; the index is made again on the rebuilt table.
DROP TABLE measurements;
ALTER TABLE measurements_new RENAME TO measurements;

CREATE INDEX ix_measurements_session ON measurements (as_of, entity_kind, leaf);

-- ───────────────────────────── the options sleeve ─────────────────────────────

-- One position of the sleeve: a long option bought in one block (MASTER_PLAN §5.6). premium_base
-- is the whole premium paid in the base currency, the figure the sleeve's pool and the penalty
-- are measured in; fx_to_base is the rate it was converted at (NULL when the premium was in the
-- base currency). close_by is the session it must be sold by: the last tradable session of the
-- option and its underlying less tenor.mandatory_close_trading_days. A position closes once, with
-- the reason it closed for, and nothing else about it ever changes.
CREATE TABLE option_positions (
    position_id          INTEGER PRIMARY KEY,
    underlying_conid     INTEGER NOT NULL CHECK (underlying_conid > 0),
    option_conid         INTEGER CHECK (option_conid IS NULL OR option_conid > 0),
    expiry               TEXT    NOT NULL CHECK (expiry GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    strike               REAL    NOT NULL CHECK (strike > 0),
    "right"              TEXT    NOT NULL CHECK ("right" IN ('C', 'P')),
    contracts            INTEGER NOT NULL CHECK (contracts > 0),
    multiplier           REAL    NOT NULL CHECK (multiplier > 0),
    premium_per_unit     TEXT    NOT NULL CHECK (trim(premium_per_unit) <> ''
                             AND CAST(premium_per_unit AS REAL) > 0),
    currency             TEXT    NOT NULL CHECK (currency GLOB '[A-Z][A-Z][A-Z]'),
    premium_base         TEXT    NOT NULL CHECK (trim(premium_base) <> ''
                             AND CAST(premium_base AS REAL) > 0),
    fx_to_base           TEXT    CHECK (fx_to_base IS NULL OR CAST(fx_to_base AS REAL) > 0),
    opened_on            TEXT    NOT NULL CHECK (opened_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    close_by             TEXT    NOT NULL CHECK (close_by GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    target               REAL    CHECK (target IS NULL OR target > 0),
    prob_target_at_entry REAL    CHECK (prob_target_at_entry IS NULL
                             OR (prob_target_at_entry >= 0 AND prob_target_at_entry <= 1)),
    catalyst_date        TEXT    CHECK (catalyst_date IS NULL
                             OR catalyst_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    intent_id            TEXT    REFERENCES intents (intent_id),
    closed_on            TEXT    CHECK (closed_on IS NULL
                             OR closed_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    exit_reason          TEXT    CHECK (exit_reason IS NULL OR trim(exit_reason) <> ''),
    CHECK ((closed_on IS NULL) = (exit_reason IS NULL)),
    CHECK (closed_on IS NULL OR closed_on >= opened_on),
    CHECK (close_by >= opened_on AND close_by <= expiry)
);

CREATE INDEX ix_option_positions_open ON option_positions (closed_on);

CREATE TRIGGER option_positions_identity_fixed BEFORE UPDATE OF position_id, underlying_conid,
    option_conid, expiry, strike, "right", contracts, multiplier, premium_per_unit, currency,
    premium_base, fx_to_base, opened_on, close_by, target, prob_target_at_entry, catalyst_date,
    intent_id ON option_positions
BEGIN
    SELECT RAISE(ABORT, 'an option position''s terms never change');
END;

CREATE TRIGGER option_positions_closed_once BEFORE UPDATE ON option_positions
WHEN OLD.closed_on IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'a closed option position never changes');
END;

CREATE TRIGGER option_positions_no_delete BEFORE DELETE ON option_positions
BEGIN
    SELECT RAISE(ABORT, 'an option position is never deleted');
END;

-- The penalty ledger (lite's option_ledger): every event that moves the sleeve's pool or its
-- penalty balance, in the base currency, append-only. The balance is replayed from penalty_added
-- (floored at zero after each event); penalty_balance_after is the audit trail the replay is
-- checked against. A commitment ties up premium while its position is open and is released by
-- the position closing, never by a second row (a release nobody wrote would free budget that is
-- still spent). amount is the event's magnitude: the premium committed, the loss detected or
-- realised, the gain realised. A qualifying gain is a non-option position's (conid) exited for
-- one of option_sleeve.qualifying_exit_reasons.
CREATE TABLE option_ledger (
    event_id              INTEGER PRIMARY KEY,
    event_on              TEXT    NOT NULL CHECK (event_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    event_type            TEXT    NOT NULL CHECK (event_type IN ('premium_committed',
                              'loss_detected', 'loss_closed', 'qualifying_gain', 'option_gain')),
    position_id           INTEGER REFERENCES option_positions (position_id),
    conid                 INTEGER CHECK (conid IS NULL OR conid > 0),
    amount                TEXT    NOT NULL CHECK (trim(amount) <> '' AND CAST(amount AS REAL) >= 0),
    penalty_added         TEXT    NOT NULL CHECK (trim(penalty_added) <> ''),
    penalty_balance_after TEXT    NOT NULL CHECK (trim(penalty_balance_after) <> ''
                              AND CAST(penalty_balance_after AS REAL) >= 0),
    exit_reason           TEXT    CHECK (exit_reason IS NULL OR trim(exit_reason) <> ''),
    quote_currency        TEXT    CHECK (quote_currency IS NULL
                              OR quote_currency GLOB '[A-Z][A-Z][A-Z]'),
    quote_amount          TEXT,
    fx_to_base            TEXT    CHECK (fx_to_base IS NULL OR CAST(fx_to_base AS REAL) > 0),
    note                  TEXT    NOT NULL CHECK (trim(note) <> ''),
    recorded_at           TEXT    NOT NULL CHECK (recorded_at GLOB
                              '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    CHECK (event_type = 'qualifying_gain' OR position_id IS NOT NULL),
    CHECK (event_type <> 'qualifying_gain' OR (conid IS NOT NULL AND exit_reason IS NOT NULL
        AND position_id IS NULL)),
    CHECK ((quote_currency IS NULL) = (quote_amount IS NULL)
        AND (quote_currency IS NULL) = (fx_to_base IS NULL))
);

CREATE INDEX ix_option_ledger_position ON option_ledger (position_id, event_type);

CREATE TRIGGER option_ledger_no_update BEFORE UPDATE ON option_ledger
BEGIN
    SELECT RAISE(ABORT, 'option_ledger is append-only');
END;

CREATE TRIGGER option_ledger_no_delete BEFORE DELETE ON option_ledger
BEGIN
    SELECT RAISE(ABORT, 'option_ledger is append-only');
END;

-- One review of an open position against the zombie test (lite's zombie_observations): whether
-- all four conditions held that session, the readings and their thresholds. The confirmation
-- counts consecutive review sessions, so a session is reviewed once (a re-review replaces it).
CREATE TABLE zombie_observations (
    position_id                INTEGER NOT NULL REFERENCES option_positions (position_id),
    observed_on                TEXT    NOT NULL CHECK (observed_on GLOB
                                   '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    all_conditions_met         INTEGER NOT NULL CHECK (all_conditions_met IN (0, 1)),
    delta                      REAL,
    dte                        INTEGER,
    prob_target                REAL    CHECK (prob_target IS NULL
                                   OR (prob_target >= 0 AND prob_target <= 1)),
    prob_ratio_vs_entry        REAL,
    recoverable_pct_of_premium REAL,
    detail                     TEXT    NOT NULL CHECK (json_valid(detail)),
    recorded_at                TEXT    NOT NULL CHECK (recorded_at GLOB
                                   '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (position_id, observed_on)
) WITHOUT ROWID;

-- The sleeve's would-be entries while its activation condition fails (DECISIONS D66, D138): every
-- contract of the session's chain that failed no candidate gate, with its quote, its premium in
-- the base currency when a rate was known, the gates it left untested and why the sleeve did
-- not trade. H8's forward record. A rebuild of a session replaces that session's rows.
CREATE TABLE option_shadow_entries (
    as_of              TEXT    NOT NULL CHECK (as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    underlying_conid   INTEGER NOT NULL CHECK (underlying_conid > 0),
    expiry             TEXT    NOT NULL CHECK (expiry GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    strike             REAL    NOT NULL CHECK (strike > 0),
    "right"            TEXT    NOT NULL CHECK ("right" IN ('C', 'P')),
    snapshot_date      TEXT    NOT NULL CHECK (snapshot_date GLOB
                           '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND snapshot_date <= as_of),
    option_conid       INTEGER CHECK (option_conid IS NULL OR option_conid > 0),
    bid                REAL    CHECK (bid IS NULL OR bid >= 0),
    ask                REAL    CHECK (ask IS NULL OR ask >= 0),
    mid                REAL    NOT NULL CHECK (mid > 0),
    iv                 REAL    CHECK (iv IS NULL OR iv >= 0),
    delta              REAL    CHECK (delta IS NULL OR (delta >= -1 AND delta <= 1)),
    multiplier         REAL    CHECK (multiplier IS NULL OR multiplier > 0),
    currency           TEXT    NOT NULL CHECK (currency GLOB '[A-Z][A-Z][A-Z]'),
    premium_base       TEXT    CHECK (premium_base IS NULL OR CAST(premium_base AS REAL) > 0),
    iv_percentile      REAL    CHECK (iv_percentile IS NULL
                           OR (iv_percentile >= 0 AND iv_percentile <= 1)),
    market_data_status TEXT    CHECK (market_data_status IS NULL
                           OR market_data_status IN ('realtime', 'delayed', 'frozen', 'none')),
    untested           TEXT    NOT NULL CHECK (json_valid(untested) AND json_type(untested) = 'array'),
    activation         TEXT    NOT NULL CHECK (trim(activation) <> ''),
    computed_at        TEXT    NOT NULL CHECK (computed_at GLOB
                           '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (as_of, underlying_conid, expiry, strike, "right")
) WITHOUT ROWID;
