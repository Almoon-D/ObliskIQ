-- 015_analyst — the LLM analyst (MASTER_PLAN §5.5, §5.10; DECISIONS D70, D128, D156): every call
-- it made, the drafts it proposed and what became of each, and its second opinion on each of the
-- owner's ideas.
--
-- Forward-only. Dates are 'YYYY-MM-DD' and stamps 'YYYY-MM-DDTHH:MM:SSZ', checked as in 001_core.
-- All three tables are append-only: the forward record is the series as it was produced. A
-- segment of it is one model and one prompt hash (model_id, prompt_hash of the call).

-- ───────────────────────────── calls ─────────────────────────────

-- One call of the analyst: for drafts of its own, or for a second opinion on one owner thesis.
-- as_of is the fact sheet's date and sheet_hash its identity; output is the validated answer.
CREATE TABLE analyst_calls (
    call_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT    NOT NULL CHECK (kind IN ('drafts', 'opinion')),
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
    CHECK ((kind = 'opinion') = (thesis_id IS NOT NULL)),
    CHECK ((status = 'ok') = (output IS NOT NULL)),
    CHECK (status = 'ok' OR (error IS NOT NULL AND trim(error) <> ''))
);

CREATE INDEX ix_analyst_calls_produced ON analyst_calls (produced_at);

CREATE TRIGGER analyst_calls_no_update BEFORE UPDATE ON analyst_calls
BEGIN
    SELECT RAISE(ABORT, 'analyst_calls is append-only');
END;

CREATE TRIGGER analyst_calls_no_delete BEFORE DELETE ON analyst_calls
BEGIN
    SELECT RAISE(ABORT, 'analyst_calls is append-only');
END;

-- ───────────────────────────── drafts ─────────────────────────────

-- Each draft a drafts call returned, in its order: as the model wrote it, the fact lines it cited
-- as they read, and what became of it: dropped by the analyst's own checks, refused by the
-- intake, or accepted as a thesis (a candidate for the book's gate).
CREATE TABLE analyst_drafts (
    call_id   INTEGER NOT NULL REFERENCES analyst_calls (call_id),
    seq       INTEGER NOT NULL CHECK (seq >= 1),
    draft     TEXT    NOT NULL CHECK (trim(draft) <> ''),
    facts     TEXT    NOT NULL,
    outcome   TEXT    NOT NULL CHECK (outcome IN ('dropped', 'refused', 'accepted')),
    reason    TEXT,
    thesis_id INTEGER REFERENCES theses (thesis_id),
    PRIMARY KEY (call_id, seq),
    CHECK ((outcome = 'accepted') = (thesis_id IS NOT NULL)),
    CHECK (outcome = 'accepted' OR (reason IS NOT NULL AND trim(reason) <> ''))
) WITHOUT ROWID;

CREATE TRIGGER analyst_drafts_no_update BEFORE UPDATE ON analyst_drafts
BEGIN
    SELECT RAISE(ABORT, 'analyst_drafts is append-only');
END;

CREATE TRIGGER analyst_drafts_no_delete BEFORE DELETE ON analyst_drafts
BEGIN
    SELECT RAISE(ABORT, 'analyst_drafts is append-only');
END;

-- ───────────────────────────── second opinions ─────────────────────────────

-- The analyst's second opinion on one owner thesis: a critique and its stated probability that
-- the target is reached, from one ok call. One per thesis; never a veto. Graded (Brier) against
-- the thesis's grade when it closes.
CREATE TABLE thesis_opinions (
    thesis_id   INTEGER PRIMARY KEY REFERENCES theses (thesis_id),
    call_id     INTEGER NOT NULL UNIQUE REFERENCES analyst_calls (call_id),
    probability REAL    NOT NULL CHECK (probability >= 0 AND probability <= 1),
    critique    TEXT    NOT NULL CHECK (trim(critique) <> ''),
    facts       TEXT    NOT NULL
);

CREATE TRIGGER thesis_opinions_no_update BEFORE UPDATE ON thesis_opinions
BEGIN
    SELECT RAISE(ABORT, 'a second opinion is written once');
END;

CREATE TRIGGER thesis_opinions_no_delete BEFORE DELETE ON thesis_opinions
BEGIN
    SELECT RAISE(ABORT, 'a second opinion is never deleted');
END;
