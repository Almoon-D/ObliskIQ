-- 018_h13_sessions — what H13's record and the promotion gate's last L1 criterion read
-- (research/preregistrations/thesis-book.md; MASTER_PLAN §5.19; DECISIONS D171): the broker
-- session's repairs, the thesis book's capital each day it was read, and H13's looks.
--
-- Forward-only. Conventions as in 001_core and 017_scoreboard (money as TEXT holding a Decimal's
-- exact digits, dates 'YYYY-MM-DD', stamps 'YYYY-MM-DDTHH:MM:SSZ'). Every table here is a record:
-- append-only, enforced by trigger.

-- ───────────────────────────── session_events ─────────────────────────────

-- What the broker session reported about itself, as the service heard it: a competing session, a
-- session lost (IBKR's daily reset, a logout, a timeout), a re-login that succeeded and one that
-- failed. Every re-login here was made by the bot itself, unattended.
CREATE TABLE session_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT    NOT NULL CHECK (at GLOB
                 '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    kind     TEXT    NOT NULL CHECK (kind IN ('competing_session', 'session_lost',
                 'reauthenticated', 'reauthentication_failed')),
    detail   TEXT    NOT NULL CHECK (trim(detail) <> '')
);

CREATE INDEX ix_session_events_at ON session_events (at, event_id);

CREATE TRIGGER session_events_no_update BEFORE UPDATE ON session_events
BEGIN
    SELECT RAISE(ABORT, 'session_events is append-only');
END;

CREATE TRIGGER session_events_no_delete BEFORE DELETE ON session_events
BEGIN
    SELECT RAISE(ABORT, 'session_events is append-only');
END;

-- ───────────────────────────── book_capital ─────────────────────────────

-- The thesis book's capital in the base currency, in the mode it was read in, on each day a
-- decision cycle read it: H13's K (the book's capital on the last session before a month). The
-- first reading of a day is kept.
CREATE TABLE book_capital (
    mode        TEXT NOT NULL CHECK (mode IN ('shadow', 'paper', 'live-small', 'live')),
    on_day      TEXT NOT NULL CHECK (on_day GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    capital     TEXT NOT NULL CHECK (CAST(capital AS REAL) >= 0),
    recorded_at TEXT NOT NULL CHECK (recorded_at GLOB
                    '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (mode, on_day)
) WITHOUT ROWID;

CREATE TRIGGER book_capital_no_update BEFORE UPDATE ON book_capital
BEGIN
    SELECT RAISE(ABORT, 'book_capital is append-only');
END;

CREATE TRIGGER book_capital_no_delete BEFORE DELETE ON book_capital
BEGIN
    SELECT RAISE(ABORT, 'book_capital is append-only');
END;

-- ───────────────────────────── h13_looks ─────────────────────────────

-- One look of H13 at one record (shadow, paper, live) and one group ('book', 'source:<s>',
-- 'analyst:<segment>', 'conviction:<c>'), at the end of the month the look falls on: the months
-- and theses it read, the mean and standard deviation of x, the two statistics and their
-- boundaries, and what it decided. A look not evaluable carries no statistic and decides nothing.
-- Written once, never repeated.
CREATE TABLE h13_looks (
    record    TEXT    NOT NULL CHECK (record IN ('shadow', 'paper', 'live')),
    grp       TEXT    NOT NULL CHECK (trim(grp) <> ''),
    month     TEXT    NOT NULL CHECK (month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
    look      INTEGER NOT NULL CHECK (look >= 1),
    months    INTEGER NOT NULL CHECK (months >= 1),
    closed    INTEGER NOT NULL CHECK (closed >= 0),
    mean      REAL,
    sd        REAL,
    t_up      REAL,
    t_down    REAL,
    c_up      REAL,
    c_down    REAL,
    without_best REAL,
    step      INTEGER NOT NULL CHECK (step IN (0, 1)),
    back      INTEGER NOT NULL CHECK (back IN (0, 1)),
    evaluable INTEGER NOT NULL CHECK (evaluable IN (0, 1)),
    series    TEXT    NOT NULL CHECK (json_valid(series)),
    detail    TEXT    NOT NULL CHECK (trim(detail) <> ''),
    at        TEXT    NOT NULL CHECK (at GLOB
                  '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (record, grp, month),
    CHECK (evaluable = 1 OR (mean IS NULL AND step = 0 AND back = 0)),
    CHECK (step = 0 OR (record = 'live' AND grp = 'book'))
) WITHOUT ROWID;

CREATE TRIGGER h13_looks_no_update BEFORE UPDATE ON h13_looks
BEGIN
    SELECT RAISE(ABORT, 'an H13 look is written once');
END;

CREATE TRIGGER h13_looks_no_delete BEFORE DELETE ON h13_looks
BEGIN
    SELECT RAISE(ABORT, 'an H13 look is written once');
END;
