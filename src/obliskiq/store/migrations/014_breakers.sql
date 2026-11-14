-- 014_breakers — the managed book's value over time and the hard breaker's latch
-- (MASTER_PLAN §5.11, DECISIONS D88 "Measurement").
--
-- Forward-only. Conventions as in 001_core and 002_orders (money as TEXT holding a Decimal's
-- exact digits, dates 'YYYY-MM-DD', stamps 'YYYY-MM-DDTHH:MM:SSZ').

-- ───────────────────────────── managed_nav ─────────────────────────────

-- One reading of a managed book's value per day, keyed on the day it was TAKEN, with the equity
-- reference's unit value read at the same moment (NULL: it had no quote). book is 'shadow' (the
-- simulated fills) or 'broker' (the account's real ones): two books, two series. is_close is 1
-- for the reading of the day's last decision cycle, the only one a peak is taken from; a reading
-- inside the day replaces the day's earlier one and never a close (store/breakers.py is its one
-- writer).
CREATE TABLE managed_nav (
    book            TEXT    NOT NULL CHECK (book IN ('shadow', 'broker')),
    as_of           TEXT    NOT NULL CHECK (as_of GLOB
                        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    managed         TEXT    NOT NULL CHECK (trim(managed) <> '' AND CAST(managed AS REAL) > 0),
    reference_conid INTEGER CHECK (reference_conid IS NULL OR reference_conid > 0),
    reference_unit  TEXT    CHECK (reference_unit IS NULL OR CAST(reference_unit AS REAL) > 0),
    is_close        INTEGER NOT NULL CHECK (is_close IN (0, 1)),
    recorded_at     TEXT    NOT NULL CHECK (recorded_at GLOB
                        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (book, as_of),
    CHECK ((reference_conid IS NULL) = (reference_unit IS NULL))
) WITHOUT ROWID;

-- ───────────────────────────── breaker_events ─────────────────────────────

-- The hard breaker's latch, append-only: a book's latest row says whether it is tripped. A trip
-- stays until the owner's reset, and the peak is then taken from the closes after that reset.
CREATE TABLE breaker_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT    NOT NULL CHECK (at GLOB
                 '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    book     TEXT    NOT NULL CHECK (book IN ('shadow', 'broker')),
    event    TEXT    NOT NULL CHECK (event IN ('tripped', 'reset')),
    actor    TEXT    NOT NULL CHECK (trim(actor) <> ''),
    detail   TEXT    NOT NULL CHECK (trim(detail) <> '')
);

CREATE TRIGGER breaker_events_no_update BEFORE UPDATE ON breaker_events
BEGIN
    SELECT RAISE(ABORT, 'breaker_events is append-only');
END;

CREATE TRIGGER breaker_events_no_delete BEFORE DELETE ON breaker_events
BEGIN
    SELECT RAISE(ABORT, 'breaker_events is append-only');
END;
