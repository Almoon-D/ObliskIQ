-- 023_analyst_replay — the analyst replayed after its models' training cutoff (H15,
-- research/preregistrations/analyst-replay.md; DECISIONS D191, D199). The replay keeps its own
-- store file (llm.replay.store_file), built by these same migrations: its record is never pooled
-- with the forward one. Each analyst call names its record; the SEC's filings index and today's
-- tickers make each sheet date's universe; the memory probe's questions and answers; and each
-- segment's run on one sheet date, so a stopped replay repeats no paid call.
--
-- Conventions as in 001_core (dates 'YYYY-MM-DD', stamps 'YYYY-MM-DDTHH:MM:SSZ'). Every table is a
-- record: append-only, enforced by trigger.
-- SPDX-License-Identifier: AGPL-3.0-or-later

-- Every call made so far is a forward one.
ALTER TABLE analyst_calls ADD COLUMN record TEXT NOT NULL DEFAULT 'forward'
    CHECK (record IN ('forward', 'replay'));

-- A filing of one of llm.replay.filer_forms as the SEC's quarterly full index lists it, known
-- from the end of the day it was filed (the index dates a filing by day, not by acceptance time).
CREATE TABLE replay_filings (
    cik      INTEGER NOT NULL CHECK (cik > 0),
    form     TEXT    NOT NULL CHECK (trim(form) <> ''),
    filed    TEXT    NOT NULL CHECK (filed GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    name     TEXT    NOT NULL CHECK (trim(name) <> ''),
    known_at TEXT    NOT NULL CHECK (known_at GLOB
                 '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (cik, form, filed)
) WITHOUT ROWID;

CREATE INDEX ix_replay_filings_filed ON replay_filings (filed);

CREATE TRIGGER replay_filings_no_update BEFORE UPDATE ON replay_filings
BEGIN SELECT RAISE(ABORT, 'replay_filings is append-only'); END;
CREATE TRIGGER replay_filings_no_delete BEFORE DELETE ON replay_filings
BEGIN SELECT RAISE(ABORT, 'replay_filings is append-only'); END;

-- The ticker the SEC's records give a filing company when the replay read them (one per
-- company, the first the SEC lists), or none: a company without one has no price and is not
-- evaluable. Read once; a company that has delisted is usually without one (the lean to
-- survivors the pre-registration states).
CREATE TABLE replay_names (
    cik      INTEGER PRIMARY KEY CHECK (cik > 0),
    ticker   TEXT    CHECK (ticker IS NULL OR trim(ticker) <> ''),
    exchange TEXT    CHECK (exchange IS NULL OR trim(exchange) <> ''),
    read_at  TEXT    NOT NULL CHECK (read_at GLOB
                 '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
) WITHOUT ROWID;

CREATE TRIGGER replay_names_no_update BEFORE UPDATE ON replay_names
BEGIN SELECT RAISE(ABORT, 'replay_names is append-only'); END;
CREATE TRIGGER replay_names_no_delete BEFORE DELETE ON replay_names
BEGIN SELECT RAISE(ABORT, 'replay_names is append-only'); END;

-- One question of the memory probe and the model's answer: a name and a sheet date of one
-- quarter, asked with nothing but the ticker, the company's name and the date for the
-- probability that the name closed higher after the horizon. ``rose`` is what the prices show; it
-- is never sent. A question is asked once per model.
CREATE TABLE replay_probes (
    probe_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    provider      TEXT    NOT NULL CHECK (trim(provider) <> ''),
    model_id      TEXT    NOT NULL CHECK (trim(model_id) <> ''),
    quarter       TEXT    NOT NULL CHECK (quarter GLOB '[0-9][0-9][0-9][0-9]Q[1-4]'),
    seq           INTEGER NOT NULL CHECK (seq >= 1),
    sheet_date    TEXT    NOT NULL CHECK (sheet_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    ticker        TEXT    NOT NULL CHECK (trim(ticker) <> ''),
    rose          INTEGER NOT NULL CHECK (rose IN (0, 1)),
    status        TEXT    NOT NULL CHECK (status IN ('ok', 'invalid', 'failed')),
    probability   REAL    CHECK (probability IS NULL OR (probability >= 0 AND probability <= 1)),
    error         TEXT,
    input_tokens  INTEGER NOT NULL CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
    cost_usd      REAL    NOT NULL CHECK (cost_usd >= 0),
    produced_at   TEXT    NOT NULL CHECK (produced_at GLOB
                      '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    record        TEXT    NOT NULL CHECK (record = 'replay'),
    UNIQUE (model_id, quarter, seq),
    CHECK ((status = 'ok') = (probability IS NOT NULL)),
    CHECK (status = 'ok' OR (error IS NOT NULL AND trim(error) <> ''))
);

CREATE TRIGGER replay_probes_no_update BEFORE UPDATE ON replay_probes
BEGIN SELECT RAISE(ABORT, 'replay_probes is append-only'); END;
CREATE TRIGGER replay_probes_no_delete BEFORE DELETE ON replay_probes
BEGIN SELECT RAISE(ABORT, 'replay_probes is append-only'); END;

-- One segment's run on one sheet date: done (``ok``), or found ``interrupted`` (its calls are
-- stored, its drafts are not) and never run again. Its calls are the analyst calls of its
-- segment, prompt hash and date.
CREATE TABLE replay_units (
    label       TEXT    NOT NULL CHECK (trim(label) <> ''),
    segment     TEXT    NOT NULL CHECK (trim(segment) <> ''),
    sheet_date  TEXT    NOT NULL CHECK (sheet_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    side        TEXT    NOT NULL CHECK (side IN ('after', 'before')),
    state       TEXT    NOT NULL CHECK (state IN ('ok', 'interrupted')),
    reason      TEXT    NOT NULL CHECK (trim(reason) <> ''),
    cost_usd    REAL    NOT NULL CHECK (cost_usd >= 0),
    finished_at TEXT    NOT NULL CHECK (finished_at GLOB
                    '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    record      TEXT    NOT NULL CHECK (record = 'replay'),
    PRIMARY KEY (label, sheet_date)
) WITHOUT ROWID;

CREATE TRIGGER replay_units_no_update BEFORE UPDATE ON replay_units
BEGIN SELECT RAISE(ABORT, 'replay_units is append-only'); END;
CREATE TRIGGER replay_units_no_delete BEFORE DELETE ON replay_units
BEGIN SELECT RAISE(ABORT, 'replay_units is append-only'); END;

-- A sheet date's universe over its horizon, computed once the prices are loaded: how many names
-- it holds, the evaluable ones, the others' reasons (a JSON object of counts) and the benchmark,
-- the evaluable names' equally weighted gross return (NULL: not evaluable whole).
CREATE TABLE replay_cohorts (
    sheet_date   TEXT    PRIMARY KEY CHECK (sheet_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    start        TEXT    NOT NULL CHECK (start GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    end          TEXT    NOT NULL CHECK (end GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    universe     INTEGER NOT NULL CHECK (universe >= 0),
    evaluable    INTEGER NOT NULL CHECK (evaluable >= 0 AND evaluable <= universe),
    reasons      TEXT    NOT NULL CHECK (json_valid(reasons) AND json_type(reasons) = 'object'),
    benchmark    REAL    CHECK (benchmark IS NULL OR benchmark > 0),
    computed_at  TEXT    NOT NULL CHECK (computed_at GLOB
                     '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
) WITHOUT ROWID;

CREATE TRIGGER replay_cohorts_no_update BEFORE UPDATE ON replay_cohorts
BEGIN SELECT RAISE(ABORT, 'replay_cohorts is append-only'); END;
CREATE TRIGGER replay_cohorts_no_delete BEFORE DELETE ON replay_cohorts
BEGIN SELECT RAISE(ABORT, 'replay_cohorts is append-only'); END;
