-- 008_capital — capital and cash flows, and the tax engine's state on the live path.
--
-- The owner's cash-flow ledger (deposits, withdrawals, the adjustments that classify a broker cash
-- change), withdrawal reservations and their history, the questions the bot asks before cutting
-- what it should not, the broker cash changes nothing explains, the holdings kept at brokers with
-- no readable API (lite's external_positions), the lot engine's state beside its lots (loss
-- windows, the ledger, the lot numbering; DECISIONS D74), the realisations each fill made, and the
-- day's exchange rates to the base currency.
--
-- Forward-only. Conventions as in 001_core and 002_orders (money as TEXT holding a Decimal's
-- exact digits, dates 'YYYY-MM-DD', stamps 'YYYY-MM-DDTHH:MM:SSZ').

-- ───────────────────────────── reservations ─────────────────────────────

-- Money the owner will take out by due_date. Only the amount, the due date, the state and its
-- stamp change (a reduction, a postponement, the end), each one recorded in reservation_events;
-- a closed reservation never changes again.
CREATE TABLE withdrawal_reservations (
    reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    amount         TEXT    NOT NULL CHECK (trim(amount) <> '' AND CAST(amount AS REAL) > 0),
    currency       TEXT    NOT NULL CHECK (currency GLOB '[A-Z][A-Z][A-Z]'),
    due_date       TEXT    NOT NULL CHECK (due_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    reason         TEXT    NOT NULL CHECK (trim(reason) <> ''),
    state          TEXT    NOT NULL CHECK (state IN ('open', 'withdrawn', 'cancelled')),
    created_at     TEXT    NOT NULL CHECK (created_at GLOB
                       '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    created_by     TEXT    NOT NULL CHECK (trim(created_by) <> ''),
    updated_at     TEXT    NOT NULL CHECK (updated_at GLOB
                       '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
);

CREATE INDEX ix_withdrawal_reservations_state ON withdrawal_reservations (state, due_date);

CREATE TRIGGER withdrawal_reservations_identity_fixed BEFORE UPDATE OF reservation_id, currency,
    reason, created_at, created_by ON withdrawal_reservations
BEGIN
    SELECT RAISE(ABORT, 'a reservation''s identity never changes');
END;

CREATE TRIGGER withdrawal_reservations_closed_once BEFORE UPDATE ON withdrawal_reservations
WHEN OLD.state <> 'open'
BEGIN
    SELECT RAISE(ABORT, 'a closed reservation never changes');
END;

CREATE TRIGGER withdrawal_reservations_no_delete BEFORE DELETE ON withdrawal_reservations
BEGIN
    SELECT RAISE(ABORT, 'a reservation is never deleted');
END;

CREATE TABLE reservation_events (
    reservation_id INTEGER NOT NULL REFERENCES withdrawal_reservations (reservation_id),
    seq            INTEGER NOT NULL CHECK (seq >= 1),
    at             TEXT    NOT NULL CHECK (at GLOB
                       '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    actor          TEXT    NOT NULL CHECK (trim(actor) <> ''),
    detail         TEXT    NOT NULL CHECK (trim(detail) <> ''),
    PRIMARY KEY (reservation_id, seq)
) WITHOUT ROWID;

CREATE TRIGGER reservation_events_no_update BEFORE UPDATE ON reservation_events
BEGIN
    SELECT RAISE(ABORT, 'reservation_events is append-only');
END;

CREATE TRIGGER reservation_events_no_delete BEFORE DELETE ON reservation_events
BEGIN
    SELECT RAISE(ABORT, 'reservation_events is append-only');
END;

-- ───────────────────────────── unexplained cash ─────────────────────────────

-- A change in the broker's cash that neither the bot's fills nor a recorded flow explains. One
-- pending event per currency: reconciliation refreshes its amount until the owner classifies it.
CREATE TABLE cash_events (
    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    currency      TEXT    NOT NULL CHECK (currency GLOB '[A-Z][A-Z][A-Z]'),
    amount        TEXT    NOT NULL CHECK (trim(amount) <> ''),
    detected_at   TEXT    NOT NULL CHECK (detected_at GLOB
                      '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    updated_at    TEXT    NOT NULL CHECK (updated_at GLOB
                      '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    state         TEXT    NOT NULL CHECK (state IN ('pending', 'classified')),
    classified_at TEXT    CHECK (classified_at IS NULL OR classified_at GLOB
                      '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    classified_by TEXT,
    CHECK ((state = 'classified') = (classified_at IS NOT NULL)),
    CHECK ((state = 'classified') = (classified_by IS NOT NULL))
);

CREATE UNIQUE INDEX ux_cash_events_pending ON cash_events (currency) WHERE state = 'pending';

CREATE TRIGGER cash_events_classified_once BEFORE UPDATE ON cash_events
WHEN OLD.state = 'classified'
BEGIN
    SELECT RAISE(ABORT, 'a classified cash event never changes');
END;

CREATE TRIGGER cash_events_no_delete BEFORE DELETE ON cash_events
BEGIN
    SELECT RAISE(ABORT, 'a cash event is never deleted');
END;

-- ───────────────────────────── the cash-flow ledger ─────────────────────────────

-- Deposits, withdrawals and adjustments. amount is a positive magnitude for a deposit and a
-- withdrawal, and signed for an adjustment (money in is positive). matched_at: when the broker's
-- cash showed the flow. A flow is never deleted: it is cancelled, once, and only matched_at and
-- cancelled_at ever change.
CREATE TABLE cash_flows (
    flow_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT    NOT NULL CHECK (kind IN ('deposit', 'withdrawal', 'adjustment')),
    category       TEXT    CHECK (category IS NULL OR category IN ('dividend', 'interest', 'fee',
                       'withholding_tax', 'correction', 'other')),
    currency       TEXT    NOT NULL CHECK (currency GLOB '[A-Z][A-Z][A-Z]'),
    amount         TEXT    NOT NULL CHECK (trim(amount) <> ''),
    on_date        TEXT    NOT NULL CHECK (on_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    recorded_at    TEXT    NOT NULL CHECK (recorded_at GLOB
                       '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    recorded_by    TEXT    NOT NULL CHECK (trim(recorded_by) <> ''),
    matched_at     TEXT    CHECK (matched_at IS NULL OR matched_at GLOB
                       '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    cancelled_at   TEXT    CHECK (cancelled_at IS NULL OR cancelled_at GLOB
                       '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    in_book        INTEGER NOT NULL CHECK (in_book IN (0, 1)),
    reservation_id INTEGER REFERENCES withdrawal_reservations (reservation_id),
    event_id       INTEGER REFERENCES cash_events (event_id),
    note           TEXT    CHECK (note IS NULL OR trim(note) <> ''),
    CHECK ((kind = 'adjustment') = (category IS NOT NULL)),
    CHECK (kind = 'withdrawal' OR reservation_id IS NULL),
    CHECK (kind = 'adjustment' OR event_id IS NULL),
    CHECK (kind = 'adjustment' OR CAST(amount AS REAL) > 0)
);

CREATE INDEX ix_cash_flows_on ON cash_flows (on_date);

CREATE TRIGGER cash_flows_identity_fixed BEFORE UPDATE OF flow_id, kind, category, currency,
    amount, on_date, recorded_at, recorded_by, in_book, reservation_id, event_id, note
    ON cash_flows
BEGIN
    SELECT RAISE(ABORT, 'a cash flow never changes; cancel it and record another');
END;

CREATE TRIGGER cash_flows_stamped_once BEFORE UPDATE OF matched_at, cancelled_at ON cash_flows
WHEN (OLD.matched_at IS NOT NULL AND NEW.matched_at IS NOT OLD.matched_at)
    OR (OLD.cancelled_at IS NOT NULL AND NEW.cancelled_at IS NOT OLD.cancelled_at)
BEGIN
    SELECT RAISE(ABORT, 'a cash flow is matched once and cancelled once');
END;

CREATE TRIGGER cash_flows_no_delete BEFORE DELETE ON cash_flows
BEGIN
    SELECT RAISE(ABORT, 'a cash flow is never deleted');
END;

-- ───────────────────────────── owner questions ─────────────────────────────

-- A decision the bot needs from the owner. Only the state and the answer change, from open, once;
-- an open question's deadline passing applies its default (expired). One open question per
-- reservation and kind. question_events is the audit.
CREATE TABLE owner_questions (
    question_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT    NOT NULL CHECK (kind IN ('protected_cut', 'later_date')),
    reservation_id INTEGER NOT NULL REFERENCES withdrawal_reservations (reservation_id),
    context        TEXT    NOT NULL CHECK (trim(context) <> ''),
    options        TEXT    NOT NULL CHECK (json_valid(options) AND json_type(options) = 'array'),
    default_option TEXT    NOT NULL CHECK (trim(default_option) <> ''),
    deadline       TEXT    NOT NULL CHECK (deadline GLOB
                       '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    state          TEXT    NOT NULL CHECK (state IN ('open', 'answered', 'expired')),
    answer         TEXT,
    answered_at    TEXT    CHECK (answered_at IS NULL OR answered_at GLOB
                       '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    answered_by    TEXT,
    created_at     TEXT    NOT NULL CHECK (created_at GLOB
                       '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    CHECK ((state = 'open') = (answer IS NULL)),
    CHECK ((answer IS NULL) = (answered_at IS NULL)),
    CHECK ((answer IS NULL) = (answered_by IS NULL))
);

CREATE UNIQUE INDEX ux_owner_questions_open ON owner_questions (reservation_id, kind)
    WHERE state = 'open';

CREATE TRIGGER owner_questions_identity_fixed BEFORE UPDATE OF question_id, kind, reservation_id,
    context, options, default_option, deadline, created_at ON owner_questions
BEGIN
    SELECT RAISE(ABORT, 'a question never changes; only its answer is recorded');
END;

CREATE TRIGGER owner_questions_closed_once BEFORE UPDATE ON owner_questions
WHEN OLD.state <> 'open'
BEGIN
    SELECT RAISE(ABORT, 'an answered or expired question never changes');
END;

CREATE TRIGGER owner_questions_no_delete BEFORE DELETE ON owner_questions
BEGIN
    SELECT RAISE(ABORT, 'a question is never deleted');
END;

CREATE TABLE question_events (
    question_id INTEGER NOT NULL REFERENCES owner_questions (question_id),
    seq         INTEGER NOT NULL CHECK (seq >= 1),
    at          TEXT    NOT NULL CHECK (at GLOB
                    '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    actor       TEXT    NOT NULL CHECK (trim(actor) <> ''),
    detail      TEXT    NOT NULL CHECK (trim(detail) <> ''),
    PRIMARY KEY (question_id, seq)
) WITHOUT ROWID;

CREATE TRIGGER question_events_no_update BEFORE UPDATE ON question_events
BEGIN
    SELECT RAISE(ABORT, 'question_events is append-only');
END;

CREATE TRIGGER question_events_no_delete BEFORE DELETE ON question_events
BEGIN
    SELECT RAISE(ABORT, 'question_events is append-only');
END;

-- ───────────────────────────── external holdings ─────────────────────────────

-- lite's external_positions: holdings at brokers with no readable API, entered by hand.
-- last_updated is when a person last confirmed the figure; staleness is measured from it. Keyed on
-- (broker, symbol, isin), a missing ISIN matching a missing ISIN (lite matched with IS, because
-- SQLite treats NULLs as distinct in a UNIQUE constraint; the expression index says it here).
CREATE TABLE external_positions (
    holding_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    broker            TEXT    NOT NULL CHECK (trim(broker) <> ''),
    symbol            TEXT    NOT NULL CHECK (trim(symbol) <> ''),
    isin              TEXT    CHECK (isin IS NULL OR trim(isin) <> ''),
    quantity          TEXT    NOT NULL CHECK (trim(quantity) <> ''),
    currency          TEXT    NOT NULL CHECK (currency GLOB '[A-Z][A-Z][A-Z]'),
    market_value_base TEXT    NOT NULL CHECK (trim(market_value_base) <> ''),
    last_updated      TEXT    NOT NULL CHECK (last_updated GLOB
                          '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    notes             TEXT,
    sector            TEXT,
    country           TEXT    CHECK (country IS NULL OR country GLOB '[A-Z][A-Z]'),
    recorded_at       TEXT    NOT NULL CHECK (recorded_at GLOB
                          '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
);

CREATE UNIQUE INDEX ux_external_positions_key
    ON external_positions (broker, symbol, coalesce(isin, ''));
CREATE INDEX ix_external_positions_updated ON external_positions (last_updated);

-- ───────────────────────────── the tax engine's state ─────────────────────────────

-- Saved whole beside tax_lots, in the same transaction (D46, D74): the loss units a homogeneous
-- purchase may still take, the ledger's open years, the negative balances carried forward, the
-- settled years and the lot numbering.
CREATE TABLE tax_loss_windows (
    position      INTEGER PRIMARY KEY CHECK (position >= 1),
    security      TEXT    NOT NULL CHECK (trim(security) <> ''),
    sold_on       TEXT    NOT NULL CHECK (sold_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    closes_on     TEXT    NOT NULL CHECK (closes_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    loss_per_unit TEXT    NOT NULL,
    units         TEXT    NOT NULL,
    CHECK (closes_on >= sold_on)
);

CREATE TABLE tax_ledger_years (
    tax_year       INTEGER PRIMARY KEY,
    income         TEXT    NOT NULL,
    gains          TEXT    NOT NULL,
    withheld       TEXT    NOT NULL,
    foreign_income TEXT    NOT NULL
);

CREATE TABLE tax_carry (
    position     INTEGER PRIMARY KEY CHECK (position >= 1),
    component    TEXT    NOT NULL CHECK (component IN ('income', 'gains')),
    arising_year INTEGER NOT NULL,
    amount       TEXT    NOT NULL
);

CREATE TABLE tax_settled (
    tax_year INTEGER PRIMARY KEY,
    summary  TEXT    NOT NULL CHECK (json_valid(summary))
);

-- One row: the lot engine's running number and when the state was saved.
CREATE TABLE tax_engine (
    singleton    INTEGER PRIMARY KEY CHECK (singleton = 1),
    lot_sequence INTEGER NOT NULL CHECK (lot_sequence >= 0),
    saved_at     TEXT    NOT NULL CHECK (saved_at GLOB
                     '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
);

-- Every fill the tax book has taken: applied to the lots, or held with why (an instrument without
-- tax facts, a rate it cannot date, a sale of units it holds no lots for). A fill is taken once.
CREATE TABLE tax_fills (
    execution_id TEXT    PRIMARY KEY REFERENCES fills (execution_id),
    outcome      TEXT    NOT NULL CHECK (outcome IN ('applied', 'held', 'incomplete')),
    detail       TEXT    NOT NULL CHECK (trim(detail) <> ''),
    taken_at     TEXT    NOT NULL CHECK (taken_at GLOB
                     '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
) WITHOUT ROWID;

-- The realised results of each disposal (a security's or a currency's), for the year's report.
-- Append-only.
CREATE TABLE tax_realisations (
    realisation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id   TEXT    NOT NULL REFERENCES fills (execution_id),
    security       TEXT    NOT NULL CHECK (trim(security) <> ''),
    lot_id         TEXT    NOT NULL CHECK (trim(lot_id) <> ''),
    acquired_on    TEXT    NOT NULL CHECK (acquired_on GLOB
                       '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    disposed_on    TEXT    NOT NULL CHECK (disposed_on GLOB
                       '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    quantity       TEXT    NOT NULL,
    proceeds       TEXT    NOT NULL,
    cost           TEXT    NOT NULL,
    released       TEXT    NOT NULL,
    deferred       TEXT    NOT NULL
);

CREATE INDEX ix_tax_realisations_disposed ON tax_realisations (disposed_on);

CREATE TRIGGER tax_realisations_no_update BEFORE UPDATE ON tax_realisations
BEGIN
    SELECT RAISE(ABORT, 'tax_realisations is append-only');
END;

CREATE TRIGGER tax_realisations_no_delete BEFORE DELETE ON tax_realisations
BEGIN
    SELECT RAISE(ABORT, 'tax_realisations is append-only');
END;

-- ───────────────────────────── exchange rates ─────────────────────────────

-- One unit of a currency in the base currency, as the broker's ledger gave it on a day: the rate
-- a fill of that day is valued at (FX at the trade date). A later reading of the day replaces it.
CREATE TABLE fx_rates (
    currency     TEXT NOT NULL CHECK (currency GLOB '[A-Z][A-Z][A-Z]'),
    on_date      TEXT NOT NULL CHECK (on_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    rate_to_base TEXT NOT NULL CHECK (trim(rate_to_base) <> '' AND CAST(rate_to_base AS REAL) > 0),
    source       TEXT NOT NULL CHECK (trim(source) <> ''),
    observed_at  TEXT NOT NULL CHECK (observed_at GLOB
                     '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (currency, on_date)
) WITHOUT ROWID;
