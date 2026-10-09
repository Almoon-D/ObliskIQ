-- 005 — the option sleeve becomes a shared pool, and the IV percentile gets a route in.
--
-- Two changes, both of which the schema has to carry before any code can.
--
-- 1. option_ledger gains 'premium_committed'.
--
--    budget_pct is specified as a hard cap on current NAV, and it was not one.
--    The ledger recorded what a position COST once it went wrong and never what
--    it TIED UP while it was alive, so available budget was computed as though
--    every open position were free. With max_concurrent_positions at five, five
--    entries could each be sized against the whole sleeve.
--
--    The commitment is a first-class event rather than a column on
--    position_ledger because position_ledger stores a per-share entry_price and
--    no quantity: it cannot say what a position cost. It carries penalty_added
--    = 0, so the penalty replay and its audit trail are untouched — the balance
--    still sums exactly the rows it summed before.
--
--    There is deliberately NO release event. A commitment stops counting when
--    its position stops being open, which is one join rather than a second row
--    that can be forgotten, written twice, or written without its partner.
--
-- 2. underlying_snapshots gains the 13- and 26-week IV percentiles.
--
--    The MCP connector serves implied_volatility_percentile as
--    {high_13w, high_26w, high_52w} — three fractions, verified live in Session
--    5 — and the store had a column for one of them. Keeping only the 52-week
--    figure throws away two thirds of a reading that cannot be re-fetched for a
--    past date, which is the same mistake that left indicator 8 with no history
--    to build on. Both are NULL on every existing row and on every collector
--    write until a Client Portal field id is verified.
--
-- SQLite cannot alter a CHECK constraint in place, so option_ledger is rebuilt
-- as 002 and 004 did. Existing rows carry across unchanged, event_id included.

PRAGMA foreign_keys = ON;

ALTER TABLE option_ledger RENAME TO option_ledger_001;

CREATE TABLE option_ledger (
    event_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_at               TEXT    NOT NULL,
    -- premium_committed: premium tied up by an OPEN position. Subtracted from
    -- available budget; carries penalty_added = 0 and never touches the balance.
    event_type             TEXT    NOT NULL CHECK (event_type IN (
                               'premium_committed',
                               'loss_detected', 'loss_closed',
                               'gain_realised', 'qualifying_equity_gain')),
    contract_id            INTEGER REFERENCES universe (contract_id),
    position_id            INTEGER REFERENCES position_ledger (position_id),
    premium_paid           REAL,
    proceeds               REAL,
    realised_pnl           REAL,
    -- Booked at zombie DETECTION, not at close. Booking at close would reward
    -- deferring the close in order to postpone the penalty.
    penalty_added          REAL    NOT NULL DEFAULT 0.0,
    penalty_balance_after  REAL    NOT NULL,
    resolved_at            TEXT,
    note                   TEXT,
    created_at             TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

INSERT INTO option_ledger (event_id, event_at, event_type, contract_id, position_id,
                           premium_paid, proceeds, realised_pnl, penalty_added,
                           penalty_balance_after, resolved_at, note, created_at)
SELECT event_id, event_at, event_type, contract_id, position_id,
       premium_paid, proceeds, realised_pnl, penalty_added,
       penalty_balance_after, resolved_at, note, created_at
FROM option_ledger_001;

DROP TABLE option_ledger_001;

CREATE INDEX IF NOT EXISTS ix_option_ledger_at   ON option_ledger (event_at);
CREATE INDEX IF NOT EXISTS ix_option_ledger_type ON option_ledger (event_type, event_at);

-- A commitment is read by joining to the open positions, so index the join.
CREATE INDEX IF NOT EXISTS ix_option_ledger_position
    ON option_ledger (position_id, event_type);

-- Vendor, decimal 0-1, same units as iv_percentile_52w. NULL = not served.
ALTER TABLE underlying_snapshots ADD COLUMN iv_percentile_13w REAL;
ALTER TABLE underlying_snapshots ADD COLUMN iv_percentile_26w REAL;

-- Which ROUTE served the percentiles: 'mcp' | 'cp_api', the same vocabulary as
-- the row's own `source` column and NOT the vendor|self choice in
-- data.iv_percentile_source, which is a different question.
--
-- It needs its own column because one row can legitimately hold fields from two
-- routes. The chain collector writes the quote over CP in the morning; a session
-- with the MCP connector adds the percentile through record_iv_percentile
-- afterwards. Row-level `source` can only name one of them, and CLAUDE.md wants
-- the source per row precisely so collection can be migrated wholesale — a
-- percentile filed under the route that did not serve it would migrate wrongly
-- and nothing would look broken.
ALTER TABLE underlying_snapshots ADD COLUMN iv_percentile_route TEXT
    CHECK (iv_percentile_route IS NULL OR iv_percentile_route IN ('mcp', 'cp_api'));
