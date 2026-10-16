-- 003_pretrade — the pre-trade rails: the owner's decisions on intents held for approval, and the
-- protective stops cut to make room for an exit until the exit ends (I5).
--
-- Forward-only. Conventions as in 001_core and 002_orders.

-- ───────────────────────────── approvals ─────────────────────────────

-- The owner's approvals and rejections of intents the approval queue holds. Append-only: an
-- intent's latest row is the decision in force.
CREATE TABLE approvals (
    approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id   TEXT    NOT NULL REFERENCES intents (intent_id),
    decision    TEXT    NOT NULL CHECK (decision IN ('approved', 'rejected')),
    actor       TEXT    NOT NULL CHECK (trim(actor) <> ''),
    reason      TEXT    NOT NULL CHECK (trim(reason) <> ''),
    at          TEXT    NOT NULL CHECK (at GLOB
                    '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
);

CREATE INDEX ix_approvals_intent ON approvals (intent_id, approval_id);

CREATE TRIGGER approvals_no_update BEFORE UPDATE ON approvals
BEGIN
    SELECT RAISE(ABORT, 'approvals is append-only');
END;

CREATE TRIGGER approvals_no_delete BEFORE DELETE ON approvals
BEGIN
    SELECT RAISE(ABORT, 'approvals is append-only');
END;

-- ───────────────────────────── stop resizes (I5) ─────────────────────────────

-- A protective stop cut (resized_quantity > 0) or cancelled (0) so that an exit fits beside it,
-- with the stop's terms before the cut. The stop is restored when the exit is refused, cancelled
-- or expires; only the state, its stamp and its detail ever change, and no row is deleted.
CREATE TABLE stop_resizes (
    resize_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    exit_intent_id   TEXT    NOT NULL REFERENCES intents (intent_id),
    stop_intent_id   TEXT    NOT NULL REFERENCES intents (intent_id),
    stop_order_id    TEXT    NOT NULL CHECK (trim(stop_order_id) <> ''),
    original_terms   TEXT    NOT NULL CHECK (json_valid(original_terms)),
    resized_quantity TEXT    NOT NULL,
    state            TEXT    NOT NULL CHECK (state IN ('resized', 'closed', 'restored',
                         'restore_failed')),
    created_at       TEXT    NOT NULL CHECK (created_at GLOB
                         '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    updated_at       TEXT    NOT NULL CHECK (updated_at GLOB
                         '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    detail           TEXT    NOT NULL CHECK (trim(detail) <> ''),
    CHECK (exit_intent_id <> stop_intent_id)
);

CREATE INDEX ix_stop_resizes_state ON stop_resizes (state);

CREATE TRIGGER stop_resizes_identity_fixed BEFORE UPDATE OF resize_id, exit_intent_id,
    stop_intent_id, stop_order_id, original_terms, resized_quantity, created_at ON stop_resizes
BEGIN
    SELECT RAISE(ABORT, 'a stop resize''s identity never changes');
END;

-- A settled resize (closed, restored) is never reopened.
CREATE TRIGGER stop_resizes_settled_once BEFORE UPDATE OF state ON stop_resizes
WHEN OLD.state IN ('closed', 'restored')
BEGIN
    SELECT RAISE(ABORT, 'a settled stop resize never changes');
END;

CREATE TRIGGER stop_resizes_no_delete BEFORE DELETE ON stop_resizes
BEGIN
    SELECT RAISE(ABORT, 'a stop resize is never deleted');
END;

-- ───────────────────────────── clearances ─────────────────────────────

-- The daily caps add up the clearances issued in the last 24 hours.
CREATE INDEX ix_clearances_issued ON clearances (issued_at);
