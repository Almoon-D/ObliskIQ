-- 002_orders — the order path: intents and their history, the clearances that unlock a broker
-- write, what the broker reported (orders, fills, positions, cash), execution modes, the kill
-- switch and the holdings registry.
--
-- Forward-only, like every migration. Conventions as in 001_core, and:
--   * Money and quantities are TEXT holding a Decimal's exact digits, never REAL: a float
--     cannot hold every price, and an order's terms are compared exactly.
--   * Histories are append-only: intent_events, mode_audit, killswitch and
--     holding_assignments refuse UPDATE and DELETE by trigger. The current value of a mode,
--     the kill switch or a holding is its latest row.
--   * An intent's id is its uuid7 text; coid is what IBKR sees (the id, or its short form when
--     IBKR's cOID is shorter), unique, so the mapping back is a lookup.

-- ───────────────────────────── intents ─────────────────────────────

CREATE TABLE intents (
    intent_id         TEXT    PRIMARY KEY CHECK (length(intent_id) = 36),
    coid              TEXT    NOT NULL UNIQUE CHECK (trim(coid) <> ''),
    sleeve            TEXT    NOT NULL CHECK (trim(sleeve) <> ''),
    thesis_id         TEXT,
    role              TEXT    NOT NULL CHECK (role IN ('entry', 'protective', 'exit', 'conversion')),
    conid             INTEGER NOT NULL CHECK (conid > 0),
    side              TEXT    NOT NULL CHECK (side IN ('BUY', 'SELL')),
    terms             TEXT    NOT NULL CHECK (json_valid(terms)),
    reason            TEXT    NOT NULL CHECK (trim(reason) <> ''),
    mode              TEXT    NOT NULL CHECK (mode IN ('shadow', 'paper', 'live-small', 'live')),
    approval_required INTEGER NOT NULL CHECK (approval_required IN (0, 1)),
    created_at        TEXT    NOT NULL CHECK (created_at GLOB
                          '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    expires_at        TEXT    NOT NULL CHECK (expires_at GLOB
                          '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    parent_id         TEXT    REFERENCES intents (intent_id),
    protective_id     TEXT    REFERENCES intents (intent_id),
    state             TEXT    NOT NULL CHECK (state IN ('proposed', 'approved', 'staged',
                          'submitted', 'working', 'partially_filled', 'filled', 'cancelled',
                          'rejected', 'expired')),
    updated_at        TEXT    NOT NULL CHECK (updated_at GLOB
                          '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    CHECK (expires_at > created_at),
    CHECK (parent_id IS NULL OR parent_id <> intent_id),
    CHECK (protective_id IS NULL OR protective_id <> intent_id)
);

CREATE INDEX ix_intents_conid_state ON intents (conid, state);
CREATE INDEX ix_intents_state ON intents (state);

-- Every transition, and every fact within a state (from_state = to_state: a modify, a fill, a
-- reply). The first event of an intent has no from_state.
CREATE TABLE intent_events (
    intent_id  TEXT    NOT NULL REFERENCES intents (intent_id),
    seq        INTEGER NOT NULL CHECK (seq >= 1),
    at         TEXT    NOT NULL CHECK (at GLOB
                   '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    from_state TEXT,
    to_state   TEXT    NOT NULL,
    detail     TEXT    NOT NULL CHECK (trim(detail) <> ''),
    PRIMARY KEY (intent_id, seq),
    CHECK ((seq = 1) = (from_state IS NULL))
) WITHOUT ROWID;

CREATE TRIGGER intent_events_no_update BEFORE UPDATE ON intent_events
BEGIN
    SELECT RAISE(ABORT, 'intent_events is append-only');
END;

CREATE TRIGGER intent_events_no_delete BEFORE DELETE ON intent_events
BEGIN
    SELECT RAISE(ABORT, 'intent_events is append-only');
END;

-- ───────────────────────────── clearances ─────────────────────────────

-- Issued by the pre-trade gate, spent once by the order client (used_at), bound to one intent,
-- one action and the exact terms by their SHA-256. A modify names the broker order it changes.
-- conid, side and quantity let the gate count sells that are cleared but not yet listed.
CREATE TABLE clearances (
    clearance_id  TEXT    PRIMARY KEY CHECK (length(clearance_id) = 36),
    intent_id     TEXT    NOT NULL REFERENCES intents (intent_id),
    action        TEXT    NOT NULL CHECK (action IN ('place', 'modify')),
    order_id      TEXT,
    conid         INTEGER NOT NULL CHECK (conid > 0),
    side          TEXT    NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity      TEXT,
    terms_digest  TEXT    NOT NULL CHECK (length(terms_digest) = 64),
    effect        TEXT    NOT NULL CHECK (effect IN ('reduces_risk', 'adds_risk')),
    clearance     TEXT    NOT NULL CHECK (json_valid(clearance)),
    issued_at     TEXT    NOT NULL CHECK (issued_at GLOB
                      '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    expires_at    TEXT    NOT NULL CHECK (expires_at GLOB
                      '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    used_at       TEXT    CHECK (used_at IS NULL OR used_at GLOB
                      '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    CHECK ((action = 'modify') = (order_id IS NOT NULL)),
    CHECK (expires_at > issued_at),
    CHECK (effect = 'adds_risk' OR side = 'SELL')
);

CREATE INDEX ix_clearances_intent ON clearances (intent_id);
CREATE INDEX ix_clearances_open ON clearances (conid, side) WHERE used_at IS NULL;

-- ───────────────────────────── what the broker reported ─────────────────────────────

-- The latest known state of each broker order, keyed by IBKR's order id. intent_id is NULL for
-- an order the bot did not place.
CREATE TABLE broker_orders (
    order_id           TEXT    PRIMARY KEY CHECK (trim(order_id) <> ''),
    coid               TEXT,
    intent_id          TEXT    REFERENCES intents (intent_id),
    conid              INTEGER CHECK (conid IS NULL OR conid > 0),
    side               TEXT    CHECK (side IS NULL OR side IN ('BUY', 'SELL')),
    sec_type           TEXT,
    order_type         TEXT,
    status             TEXT    NOT NULL CHECK (trim(status) <> ''),
    total_quantity     TEXT,
    filled_quantity    TEXT,
    remaining_quantity TEXT,
    average_price      TEXT,
    tif                TEXT,
    observed_at        TEXT    NOT NULL CHECK (observed_at GLOB
                           '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
);

CREATE INDEX ix_broker_orders_intent ON broker_orders (intent_id);
CREATE INDEX ix_broker_orders_coid ON broker_orders (coid);

-- Executions, keyed by IBKR's execution id (a re-read inserts nothing new). simulated marks a
-- shadow-mode fill.
CREATE TABLE fills (
    execution_id TEXT    PRIMARY KEY CHECK (trim(execution_id) <> ''),
    intent_id    TEXT    REFERENCES intents (intent_id),
    coid         TEXT,
    conid        INTEGER NOT NULL CHECK (conid > 0),
    side         TEXT    NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity     TEXT    NOT NULL,
    price        TEXT    NOT NULL,
    commission   TEXT,
    executed_at  TEXT    NOT NULL CHECK (executed_at GLOB
                     '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    simulated    INTEGER NOT NULL CHECK (simulated IN (0, 1))
);

CREATE INDEX ix_fills_conid_time ON fills (conid, executed_at);
CREATE INDEX ix_fills_intent ON fills (intent_id);

-- One complete read of the account's positions or cash. A read with no rows is an empty
-- portfolio, which is not the same as no read at all.
CREATE TABLE account_reads (
    read_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL CHECK (kind IN ('positions', 'cash')),
    observed_at TEXT    NOT NULL CHECK (observed_at GLOB
                    '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
);

CREATE INDEX ix_account_reads_kind_time ON account_reads (kind, observed_at);

CREATE TABLE positions_snap (
    read_id      INTEGER NOT NULL REFERENCES account_reads (read_id),
    conid        INTEGER NOT NULL CHECK (conid > 0),
    quantity     TEXT    NOT NULL,
    average_cost TEXT,
    currency     TEXT    CHECK (currency IS NULL OR currency GLOB '[A-Z][A-Z][A-Z]'),
    PRIMARY KEY (read_id, conid)
) WITHOUT ROWID;

CREATE TABLE cash_snap (
    read_id      INTEGER NOT NULL REFERENCES account_reads (read_id),
    currency     TEXT    NOT NULL CHECK (currency GLOB '[A-Z][A-Z][A-Z]'),
    cash         TEXT    NOT NULL,
    settled_cash TEXT,
    PRIMARY KEY (read_id, currency)
) WITHOUT ROWID;

-- ───────────────────────────── modes, kill switch, holdings ─────────────────────────────

-- Every mode change: an upgrade by an owner command, or an automatic downgrade.
CREATE TABLE mode_audit (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT    NOT NULL CHECK (at GLOB
                  '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    from_mode TEXT    NOT NULL CHECK (from_mode IN ('shadow', 'paper', 'live-small', 'live')),
    to_mode   TEXT    NOT NULL CHECK (to_mode IN ('shadow', 'paper', 'live-small', 'live')),
    direction TEXT    NOT NULL CHECK (direction IN ('upgrade', 'downgrade')),
    actor     TEXT    NOT NULL CHECK (trim(actor) <> ''),
    reason    TEXT    NOT NULL CHECK (trim(reason) <> ''),
    CHECK (from_mode <> to_mode)
);

CREATE TRIGGER mode_audit_no_update BEFORE UPDATE ON mode_audit
BEGIN
    SELECT RAISE(ABORT, 'mode_audit is append-only');
END;

CREATE TRIGGER mode_audit_no_delete BEFORE DELETE ON mode_audit
BEGIN
    SELECT RAISE(ABORT, 'mode_audit is append-only');
END;

-- The kill switch's database flag. The switch is also a file in the data directory; it is on
-- when either says so.
CREATE TABLE killswitch (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT    NOT NULL CHECK (at GLOB
                  '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    state     TEXT    NOT NULL CHECK (state IN ('on', 'off')),
    actor     TEXT    NOT NULL CHECK (trim(actor) <> ''),
    reason    TEXT    NOT NULL CHECK (trim(reason) <> '')
);

CREATE TRIGGER killswitch_no_update BEFORE UPDATE ON killswitch
BEGIN
    SELECT RAISE(ABORT, 'killswitch is append-only');
END;

CREATE TRIGGER killswitch_no_delete BEFORE DELETE ON killswitch
BEGIN
    SELECT RAISE(ABORT, 'killswitch is append-only');
END;

-- The holdings registry: which sleeve a holding belongs to, or 'unmanaged' (counted in NAV and
-- risk, never traded until the owner assigns it). A conid's latest row is its assignment.
CREATE TABLE holding_assignments (
    assignment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    conid         INTEGER NOT NULL CHECK (conid > 0),
    owner         TEXT    NOT NULL CHECK (trim(owner) <> ''),
    assigned_by   TEXT    NOT NULL CHECK (trim(assigned_by) <> ''),
    assigned_at   TEXT    NOT NULL CHECK (assigned_at GLOB
                      '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    reason        TEXT    NOT NULL CHECK (trim(reason) <> '')
);

CREATE INDEX ix_holding_assignments_conid ON holding_assignments (conid, assignment_id);

CREATE TRIGGER holding_assignments_no_update BEFORE UPDATE ON holding_assignments
BEGIN
    SELECT RAISE(ABORT, 'holding_assignments is append-only');
END;

CREATE TRIGGER holding_assignments_no_delete BEFORE DELETE ON holding_assignments
BEGIN
    SELECT RAISE(ABORT, 'holding_assignments is append-only');
END;
