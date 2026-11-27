-- 022_analyst_segments — the analyst's segments and passes (MASTER_PLAN §5.10; DECISIONS D188):
-- each call names the configured segment that made it and may be a scan, a dossier or a weekly
-- review; a review is stored once per call beside the thesis it is on.
--
-- Forward-only. SQLite cannot change a CHECK in place, so analyst_calls is rebuilt (as 016 rebuilt
-- theses); a call made before segments keeps its rows and names no segment. Conventions as in
-- 015_analyst.
-- SPDX-License-Identifier: AGPL-3.0-or-later

-- ───────────────────────────── calls ─────────────────────────────

CREATE TABLE analyst_calls_new (
    call_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT    NOT NULL CHECK (kind IN ('scan', 'dossier', 'drafts', 'opinion', 'review')),
    thesis_id      INTEGER REFERENCES theses (thesis_id),
    as_of          TEXT    NOT NULL CHECK (as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    sheet_hash     TEXT    NOT NULL CHECK (length(sheet_hash) = 64),
    produced_at    TEXT    NOT NULL CHECK (produced_at GLOB
                       '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    provider       TEXT    NOT NULL CHECK (trim(provider) <> ''),
    model_id       TEXT    NOT NULL CHECK (trim(model_id) <> ''),
    prompt_version INTEGER NOT NULL CHECK (prompt_version >= 1),
    prompt_hash    TEXT    NOT NULL CHECK (length(prompt_hash) = 64),
    status         TEXT    NOT NULL CHECK (status IN ('ok', 'invalid', 'failed')),
    attempts       INTEGER NOT NULL CHECK (attempts >= 0),
    output         TEXT,
    error          TEXT,
    input_tokens   INTEGER NOT NULL CHECK (input_tokens >= 0),
    output_tokens  INTEGER NOT NULL CHECK (output_tokens >= 0),
    cost_usd       REAL    NOT NULL CHECK (cost_usd >= 0),
    segment        TEXT    CHECK (segment IS NULL OR trim(segment) <> ''),
    CHECK ((kind IN ('opinion', 'review')) = (thesis_id IS NOT NULL)),
    CHECK ((status = 'ok') = (output IS NOT NULL)),
    CHECK (status = 'ok' OR (error IS NOT NULL AND trim(error) <> ''))
);

INSERT INTO analyst_calls_new (call_id, kind, thesis_id, as_of, sheet_hash, produced_at, provider, model_id, prompt_version, prompt_hash, status, attempts, output, error, input_tokens, output_tokens, cost_usd, segment)
SELECT call_id, kind, thesis_id, as_of, sheet_hash, produced_at, provider, model_id, prompt_version, prompt_hash, status, attempts, output, error, input_tokens, output_tokens, cost_usd, NULL FROM analyst_calls;

-- Dropping the table drops its index and its triggers; both are made again on the rebuilt table.
DROP TABLE analyst_calls;
ALTER TABLE analyst_calls_new RENAME TO analyst_calls;

CREATE INDEX ix_analyst_calls_produced ON analyst_calls (produced_at);
CREATE INDEX ix_analyst_calls_segment ON analyst_calls (segment, as_of);

CREATE TRIGGER analyst_calls_no_update BEFORE UPDATE ON analyst_calls
BEGIN
    SELECT RAISE(ABORT, 'analyst_calls is append-only');
END;

CREATE TRIGGER analyst_calls_no_delete BEFORE DELETE ON analyst_calls
BEGIN
    SELECT RAISE(ABORT, 'analyst_calls is append-only');
END;

-- ───────────────────────────── reviews ─────────────────────────────

-- A segment's weekly review of one open thesis or open call: a critique and the probability it
-- states that the target comes first. One per ok review call; it changes no thesis. Graded
-- (Brier) against the idea ledger's outcome of the thesis's call when it resolves after it.
CREATE TABLE thesis_reviews (
    call_id     INTEGER PRIMARY KEY REFERENCES analyst_calls (call_id),
    thesis_id   INTEGER NOT NULL REFERENCES theses (thesis_id),
    probability REAL    NOT NULL CHECK (probability >= 0 AND probability <= 1),
    critique    TEXT    NOT NULL CHECK (trim(critique) <> ''),
    facts       TEXT    NOT NULL
);

CREATE INDEX ix_thesis_reviews_thesis ON thesis_reviews (thesis_id);

CREATE TRIGGER thesis_reviews_no_update BEFORE UPDATE ON thesis_reviews
BEGIN
    SELECT RAISE(ABORT, 'a review is written once');
END;

CREATE TRIGGER thesis_reviews_no_delete BEFORE DELETE ON thesis_reviews
BEGIN
    SELECT RAISE(ABORT, 'a review is never deleted');
END;
