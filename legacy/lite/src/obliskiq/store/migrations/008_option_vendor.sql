-- 008 — a third collection route: marketdata.app.
--
-- WHY THIS EXISTS. The founding premise was that option-chain history cannot be
-- retrieved retrospectively, which made daily self-collection the highest
-- priority in the build. That is true of IBKR and false of the market:
-- marketdata.app serves historical end-of-day chains carrying implied vol and
-- full greeks. Twelve of the
-- fifteen collect_chain instruments are USD-listed and reachable that way; the
-- three European listings (ASML/AEB, SAN/BM, TTE/SBF) are not, and for those the
-- original argument still holds and chain_collector keeps serving them.
--
-- Two changes follow.
--
-- 1. source gains a third value. CLAUDE.md requires the route recorded per row
--    "so collection can be migrated wholesale if needed", and a row that cannot
--    say which vendor priced it is a row nobody can audit. The existing
--    mcp-capture path writes 'mcp' because it IS MCP traffic; this is a
--    different provider with a different decoder, a different smile and a
--    different notion of a session, and it gets its own tag. Only the two tables
--    that receive vendor rows are widened. universe, bars and curve_snapshots
--    keep the two-value CHECK, because nothing writes vendor rows to them and a
--    constraint relaxed "for symmetry" is a constraint that stops constraining.
--
-- 2. chain_snapshots gains option_symbol. The vendor identifies a contract by
--    its OCC symbol (SPY261231C00765000), not by an IBKR conid, so contract_id
--    stays NULL on vendor rows — it is already nullable, and the primary key
--    never needed it. Storing the OCC symbol keeps the row re-fetchable from the
--    vendor and cross-referenceable against IBKR later. NULL on every IBKR row,
--    which is honest: those were never identified that way.
--
-- SQLite cannot alter a CHECK in place, so both tables are rebuilt as 002, 004
-- and 005 did. Both are WITHOUT ROWID with a foreign key to universe and are
-- live upsert targets, so every column, default, index and the exact primary-key
-- order is reproduced below and existing rows carry across unchanged.
--
-- Nothing is back-filled. option_symbol arrives NULL on rows collected before it
-- existed, because the OCC symbol for a row identified only by an IBKR conid is
-- not recoverable here, and deriving one would be a guess wearing a checked
-- identifier.
--
-- SPDX-License-Identifier: AGPL-3.0-or-later

PRAGMA foreign_keys = ON;

-- ── chain_snapshots ─────────────────────────────────────────────────────────

ALTER TABLE chain_snapshots RENAME TO chain_snapshots_001;

CREATE TABLE chain_snapshots (
    snapshot_date  TEXT    NOT NULL,       -- YYYY-MM-DD, the session collected
    underlying_id  INTEGER NOT NULL REFERENCES universe (contract_id) ON DELETE CASCADE,
    expiry         TEXT    NOT NULL,       -- YYYY-MM-DD
    strike         REAL    NOT NULL,
    right          TEXT    NOT NULL CHECK (right IN ('C', 'P')),
    contract_id    INTEGER,                -- the option's own conid, when resolved
    bid            REAL,
    ask            REAL,
    last           REAL,
    iv             REAL,                   -- decimal, not percent
    delta          REAL,
    open_interest  INTEGER,
    volume         INTEGER,
    source         TEXT    NOT NULL DEFAULT 'mcp'
                   CHECK (source IN ('mcp', 'cp_api', 'marketdata_app')),
    collected_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    sigma_used     REAL,
    sigma_source   TEXT,
    market_data_status TEXT,
    -- OCC symbol, e.g. SPY261231C00765000. How marketdata_app names a contract;
    -- NULL on rows the IBKR routes collected, which never carried one.
    --
    -- Appended LAST on purpose. A rebuild is free to put a new column anywhere,
    -- and putting it beside contract_id would read better — but every other
    -- migration here adds columns with ALTER TABLE ADD COLUMN, which appends,
    -- and tests compare a pre-migration row against the new one by POSITION.
    -- Column order carries no meaning; matching what an ALTER would have done
    -- keeps that comparison honest instead of making a rebuild look like a
    -- reordering.
    option_symbol  TEXT,
    PRIMARY KEY (snapshot_date, underlying_id, expiry, strike, right)
) WITHOUT ROWID;

INSERT INTO chain_snapshots (snapshot_date, underlying_id, expiry, strike, right,
                             contract_id, bid, ask, last, iv, delta, open_interest,
                             volume, source, collected_at, sigma_used, sigma_source,
                             market_data_status)
SELECT snapshot_date, underlying_id, expiry, strike, right,
       contract_id, bid, ask, last, iv, delta, open_interest,
       volume, source, collected_at, sigma_used, sigma_source,
       market_data_status
FROM chain_snapshots_001;

DROP TABLE chain_snapshots_001;

CREATE INDEX IF NOT EXISTS ix_chain_underlying_date ON chain_snapshots (underlying_id, snapshot_date);
CREATE INDEX IF NOT EXISTS ix_chain_expiry          ON chain_snapshots (expiry);

-- ── underlying_snapshots ────────────────────────────────────────────────────
--
-- iv_percentile_route keeps its own two-value CHECK deliberately. The vendor
-- serves no percentile; the whole point of collecting its chains is that the
-- percentile becomes SELF-computed from an accumulated implied-vol series. A
-- route value of 'marketdata_app' would claim a vendor percentile that does not
-- exist.

ALTER TABLE underlying_snapshots RENAME TO underlying_snapshots_003;

CREATE TABLE underlying_snapshots (
    snapshot_date      TEXT    NOT NULL,       -- YYYY-MM-DD
    contract_id        INTEGER NOT NULL REFERENCES universe (contract_id) ON DELETE CASCADE,
    last               REAL,
    bid                REAL,
    ask                REAL,
    volume             INTEGER,
    implied_vol        REAL,                   -- decimal, not percent
    iv_percentile_52w  REAL,                   -- vendor; decimal 0-1. NULL = not served
    historical_vol_30d REAL,                   -- vendor; decimal. Cross-check only
    source             TEXT    NOT NULL DEFAULT 'cp_api'
                       CHECK (source IN ('mcp', 'cp_api', 'marketdata_app')),
    collected_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    iv_percentile_13w  REAL,
    iv_percentile_26w  REAL,
    iv_percentile_route TEXT
        CHECK (iv_percentile_route IS NULL OR iv_percentile_route IN ('mcp', 'cp_api')),
    market_data_status TEXT,
    -- How implied_vol was arrived at, mirroring chain_snapshots.sigma_source.
    -- The IBKR routes read a vendor-served figure for the underlying; this one
    -- has none and DERIVES it from the nearest expiry's ATM strike, so the two
    -- must be distinguishable in the series the IV percentile is built from.
    -- NULL where the route served the number directly.
    implied_vol_source TEXT,
    PRIMARY KEY (snapshot_date, contract_id)
) WITHOUT ROWID;

INSERT INTO underlying_snapshots (snapshot_date, contract_id, last, bid, ask, volume,
                                  implied_vol, iv_percentile_52w, historical_vol_30d,
                                  source, collected_at, iv_percentile_13w,
                                  iv_percentile_26w, iv_percentile_route,
                                  market_data_status)
SELECT snapshot_date, contract_id, last, bid, ask, volume,
       implied_vol, iv_percentile_52w, historical_vol_30d,
       source, collected_at, iv_percentile_13w,
       iv_percentile_26w, iv_percentile_route,
       market_data_status
FROM underlying_snapshots_003;

DROP TABLE underlying_snapshots_003;

CREATE INDEX IF NOT EXISTS ix_underlying_snap_contract
    ON underlying_snapshots (contract_id, snapshot_date);

-- ── vendor_fetch_log ────────────────────────────────────────────────────────
--
-- Per (symbol, trade_date) pull record, so a re-run never re-spends a credit on
-- a day already held. Modelled on macro_fetch_log, which exists for the same
-- reason against the same failure: a backfill that restarts from zero after an
-- interruption burns the daily allowance and then fails on every remaining day.
--
-- credits records what the response header reported it cost, not an estimate.
-- A blocked or errored call reports zero and is recorded as such, so a retry
-- after a transient block is not mistaken for a day already collected: only
-- status 'ok' suppresses a re-fetch.

CREATE TABLE IF NOT EXISTS vendor_fetch_log (
    symbol      TEXT NOT NULL,
    trade_date  TEXT NOT NULL,             -- YYYY-MM-DD, the ET session asked for
    vendor      TEXT NOT NULL DEFAULT 'marketdata_app',
    fetched_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    status      TEXT NOT NULL CHECK (status IN ('ok', 'no_data', 'error')),
    contracts   INTEGER NOT NULL DEFAULT 0,
    credits     INTEGER,                   -- as reported by the response header
    detail      TEXT,
    PRIMARY KEY (symbol, trade_date, vendor)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS ix_vendor_fetch_date ON vendor_fetch_log (trade_date);
