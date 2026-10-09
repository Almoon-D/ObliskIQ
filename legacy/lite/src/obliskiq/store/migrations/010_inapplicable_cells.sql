-- 010 — a cell nobody could ever collect is not a collection failure.
--
-- ── grid.status gains 'inapplicable' ──
--
-- `n/a` carried two different claims and the coverage metric could not tell
-- them apart:
--
--   1. COLLECTIBLE, NOT COLLECTED YET — an instrument measures this cell and
--      the input has not arrived. A collector run fixes it. This is what
--      grid.min_coverage_pct is meant to police.
--   2. STRUCTURALLY INAPPLICABLE — no collector could ever fill it. No
--      instrument is mapped to the cell at all; or the measuring instrument has
--      no reachable option chain, so indicators 7, 8 and 9 do not apply to it;
--      or universe.yaml declares no macro series for the market, which is a gap
--      the file states on purpose.
--
-- Counting the second against collection health made the floor unreachable.
-- `cells_written` is every market x sector x leaf — 13 x 12 x ~37 = 5,772 — and
-- only fifteen cells had a measurement instrument, so the ceiling was about
-- 9.6% against a 20% floor and every build reported DEGRADED. The signal was
-- dead in the direction that matters: a real collection outage looked exactly
-- like a Tuesday.
--
-- The fix splits the reason rather than moving the denominator. Coverage is now
-- reported as two numbers that answer two different questions:
--
--   populated / collectible   collection health. Gated by grid.min_coverage_pct.
--   collectible / total       the ambition gap. Reported, never gated.
--
-- The gap stays visible — that is the whole point of declaring the axes in
-- universe.yaml rather than deriving them from whatever happens to exist — it
-- just stops being counted as something a collector failed to do.
--
-- SQLite cannot ALTER a CHECK constraint, so `grid` is rebuilt. 002, 004 and
-- 005 rebuild tables the same way, and tests/test_migration_chain.py walks the
-- chain with rows underneath for exactly this reason.
--
-- `CHECK (status = 'ok' OR reason IS NOT NULL)` is kept deliberately: an
-- inapplicable cell still has to say why it is inapplicable. "Not applicable"
-- with no reason is the silent hole this schema has refused from the start.
--
-- ── universe.history_exchange ──
--
-- Probed live on 2026-08-29: get_price_history on a TSEJ-listed share with an
-- explicit exchange=TSEJ returns "No market data permissions", while the SAME
-- contract with the exchange omitted returns full daily history at
-- delayed: 1200. Reproduced on 1617 and 1621. This inverts the native-exchange
-- rule, which holds for indices and futures and does not hold here.
--
-- `exchange` stays the true listing venue — it is what identifies the contract.
-- `history_exchange` is what a history request should send, and NULL means send
-- none. Existing rows are backfilled from `exchange`, so the column changes no
-- behaviour until a row declares otherwise.
--
-- ── universe.options_unavailable_reason ──
--
-- Which of the two claims above a chainless cell is making cannot be inferred
-- from the absence of collect_chain — absence is exactly the ambiguity being
-- removed. So it is declared, in the same shape as tradable /
-- untradeable_reason: a grid-measuring row either collects a chain or says in
-- words why no chain is reachable for it. A silent absence stays a collection
-- gap, which is the conservative direction.
--
-- SPDX-License-Identifier: AGPL-3.0-or-later

ALTER TABLE universe ADD COLUMN history_exchange TEXT;
ALTER TABLE universe ADD COLUMN options_unavailable_reason TEXT;

UPDATE universe SET history_exchange = exchange WHERE history_exchange IS NULL;

CREATE TABLE grid_new (
    as_of      TEXT NOT NULL,              -- YYYY-MM-DD
    market     TEXT NOT NULL,
    sector     TEXT NOT NULL,
    indicator  TEXT NOT NULL,
    value      REAL,
    -- ok         a value this cell can stand behind
    -- n/a        collectible, not collected yet
    -- stale      a real value whose newest input is past its freshness window
    -- inapplicable  no collector could ever fill this; excluded from the
    --            coverage denominator and reported as the ambition gap instead
    status     TEXT NOT NULL CHECK (status IN ('ok', 'n/a', 'stale', 'inapplicable')),
    -- A missing cell states its reason. Never a silent proxy, never an estimate
    -- presented as data.
    reason     TEXT,
    window     TEXT,                       -- the window the value was measured over
    computed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    indicator_family TEXT,
    contract_id INTEGER,
    units      TEXT,
    reason_code TEXT,
    n_obs      INTEGER,
    market_data_status TEXT,
    PRIMARY KEY (as_of, market, sector, indicator),
    CHECK (status = 'ok' OR reason IS NOT NULL)
) WITHOUT ROWID;

INSERT INTO grid_new (
    as_of, market, sector, indicator, value, status, reason, window, computed_at,
    indicator_family, contract_id, units, reason_code, n_obs, market_data_status)
SELECT
    as_of, market, sector, indicator, value, status, reason, window, computed_at,
    indicator_family, contract_id, units, reason_code, n_obs, market_data_status
FROM grid;

DROP TABLE grid;
ALTER TABLE grid_new RENAME TO grid;

CREATE INDEX IF NOT EXISTS ix_grid_indicator ON grid (as_of, indicator, status);
CREATE INDEX IF NOT EXISTS ix_grid_family ON grid (as_of, indicator_family);
