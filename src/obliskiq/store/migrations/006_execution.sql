-- 006_execution — the execution mechanics: the protective spec an intent carries, and the
-- reconciliation record whose latest run freezes new risk while it is not green.
--
-- Forward-only. Conventions as in 001_core and 002_orders.

-- ───────────────────────────── the protective spec ─────────────────────────────

-- The stop an entry's fills get, or a protective stop's own rule (stop price, the limit's
-- offset below it and how it was set), as JSON. NULL: none (a core position carries no stop).
ALTER TABLE intents ADD COLUMN protective_spec TEXT
    CHECK (protective_spec IS NULL OR json_valid(protective_spec));

-- Like the rest of an intent's identity, its spec never changes once it is stored.
CREATE TRIGGER intents_spec_fixed BEFORE UPDATE OF protective_spec ON intents
BEGIN
    SELECT RAISE(ABORT, 'an intent''s protective spec never changes');
END;

-- ───────────────────────────── reconciliations ─────────────────────────────

-- One run of reconciliation: the broker's orders, trades, positions and cash against the store.
-- status: green (everything agreed), drift (a finding), incomplete (the broker's data was
-- partial or missing: never green). The latest run by insertion is in force; append-only. The
-- positions and cash reads a run took are the next runs' anchor when it is green and settled.
CREATE TABLE reconciliations (
    run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at             TEXT    NOT NULL CHECK (at GLOB
                       '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    status         TEXT    NOT NULL CHECK (status IN ('green', 'drift', 'incomplete')),
    findings       TEXT    NOT NULL CHECK (json_valid(findings)),
    positions_read INTEGER REFERENCES account_reads (read_id),
    cash_read      INTEGER REFERENCES account_reads (read_id),
    anchor         INTEGER NOT NULL CHECK (anchor IN (0, 1)),
    CHECK (anchor = 0 OR (status = 'green' AND positions_read IS NOT NULL
        AND cash_read IS NOT NULL))
);

CREATE INDEX ix_reconciliations_anchor ON reconciliations (anchor, run_id);

CREATE TRIGGER reconciliations_no_update BEFORE UPDATE ON reconciliations
BEGIN
    SELECT RAISE(ABORT, 'reconciliations is append-only');
END;

CREATE TRIGGER reconciliations_no_delete BEFORE DELETE ON reconciliations
BEGIN
    SELECT RAISE(ABORT, 'reconciliations is append-only');
END;
