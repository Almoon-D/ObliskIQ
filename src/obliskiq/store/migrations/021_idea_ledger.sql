-- 021_idea_ledger — the idea ledger (DECISIONS D187; research/preregistrations/idea-ledger.md,
-- H14): every idea that passes the gate's own checks is a call graded forward at its own levels,
-- whether or not it trades; the probability a source stated with its idea; and each segment's
-- calibration as the daily grading found it, which may order the book's free slots.
--
-- Conventions as in 001_core (money as TEXT holding a Decimal's exact digits, dates
-- 'YYYY-MM-DD', stamps 'YYYY-MM-DDTHH:MM:SSZ'). Every table is a record: append-only, enforced
-- by trigger.
-- SPDX-License-Identifier: AGPL-3.0-or-later

-- The probability its source stated that an idea's target comes first (before its invalidation
-- and its horizon), stored with the idea at intake.
CREATE TABLE thesis_probabilities (
    thesis_id   INTEGER PRIMARY KEY REFERENCES theses (thesis_id),
    probability REAL    NOT NULL CHECK (probability >= 0 AND probability <= 1),
    known_at    TEXT    NOT NULL CHECK (known_at GLOB
                    '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
);

CREATE TRIGGER thesis_probabilities_no_update BEFORE UPDATE ON thesis_probabilities
BEGIN SELECT RAISE(ABORT, 'thesis_probabilities is append-only'); END;
CREATE TRIGGER thesis_probabilities_no_delete BEFORE DELETE ON thesis_probabilities
BEGIN SELECT RAISE(ABORT, 'thesis_probabilities is append-only'); END;

-- A call: an idea written into the ledger at the first cycle its checks passed. Its levels are
-- the thesis's; rises says whether the view is that the instrument rises; event_tags is a JSON
-- array of strings (empty until events are collected).
CREATE TABLE idea_calls (
    call_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    thesis_id    INTEGER NOT NULL UNIQUE REFERENCES theses (thesis_id),
    conid        INTEGER NOT NULL CHECK (conid > 0),
    symbol       TEXT    NOT NULL CHECK (trim(symbol) <> ''),
    source       TEXT    NOT NULL CHECK (source IN ('owner', 'analyst')),
    segment      TEXT    NOT NULL CHECK (trim(segment) <> ''),
    event_tags   TEXT    NOT NULL CHECK (json_valid(event_tags) AND json_type(event_tags) = 'array'),
    rises        INTEGER NOT NULL CHECK (rises IN (0, 1)),
    target       TEXT    NOT NULL CHECK (CAST(target AS REAL) > 0),
    invalidation TEXT    NOT NULL CHECK (CAST(invalidation AS REAL) > 0),
    horizon      TEXT    NOT NULL CHECK (horizon GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    probability  REAL    CHECK (probability IS NULL OR (probability >= 0 AND probability <= 1)),
    known_at     TEXT    NOT NULL CHECK (known_at GLOB
                     '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    CHECK (target <> invalidation)
);

CREATE INDEX ix_idea_calls_known ON idea_calls (known_at, call_id);

CREATE TRIGGER idea_calls_no_update BEFORE UPDATE ON idea_calls
BEGIN SELECT RAISE(ABORT, 'idea_calls is append-only'); END;
CREATE TRIGGER idea_calls_no_delete BEFORE DELETE ON idea_calls
BEGIN SELECT RAISE(ABORT, 'idea_calls is append-only'); END;

-- A segment's calibration as a day's grading found it: passed, the shrink of its stated edge and
-- its base rate (NULL below the minimum), and why. One row a segment a day.
CREATE TABLE ledger_segments (
    segment   TEXT    NOT NULL CHECK (trim(segment) <> ''),
    as_of     TEXT    NOT NULL CHECK (as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    passed    INTEGER NOT NULL CHECK (passed IN (0, 1)),
    shrink    REAL    NOT NULL CHECK (shrink >= 0 AND shrink <= 1),
    base_rate REAL    CHECK (base_rate IS NULL OR (base_rate >= 0 AND base_rate <= 1)),
    reason    TEXT    NOT NULL CHECK (trim(reason) <> ''),
    known_at  TEXT    NOT NULL CHECK (known_at GLOB
                  '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (segment, as_of),
    CHECK (passed = 0 OR base_rate IS NOT NULL)
);

CREATE TRIGGER ledger_segments_no_update BEFORE UPDATE ON ledger_segments
BEGIN SELECT RAISE(ABORT, 'ledger_segments is append-only'); END;
CREATE TRIGGER ledger_segments_no_delete BEFORE DELETE ON ledger_segments
BEGIN SELECT RAISE(ABORT, 'ledger_segments is append-only'); END;
