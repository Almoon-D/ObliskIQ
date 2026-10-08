-- 019 — correlation_matrix gains an asset class, so a pair can be equity
-- against rates rather than equity against equity.
--
-- ── Why the table is rebuilt rather than altered ──
--
-- The matrix has computed every unordered MARKET pair against a non-overlapping
-- norm since 003, and it is equity-index-to-equity-index only. FX is in the
-- store — eleven ECB reference rates — and rates are in the store — a policy
-- rate and a 10-year yield per market — and neither entered the matrix. Equity
-- against rates flipping sign is one of the cleanest regime markers there is,
-- and both series were already being collected and paid for.
--
-- A market no longer identifies a leg: `germany` has an equity index, a policy
-- rate and a long rate, and they are three different series. The primary key
-- therefore has to carry the asset class and the series, and the table is
-- WITHOUT ROWID with the key in it — which SQLite cannot alter in place. So:
-- create, copy, drop, rename. Forward-only and in order, like every migration
-- here.
--
-- ── What a leg is, and why series_a == market_a on an equity row ──
--
-- A leg is (asset_class, series). For `fx` and `rate` the series is the macro
-- series id the market declares in universe.yaml. For `equity` it is the MARKET
-- NAME, not the native index symbol — a market is measured on exactly one index
-- contract, so the market names that series uniquely, and using it means the
-- existing rows copy across EXACTLY rather than being back-filled with a symbol
-- this migration has no way to look up. A convention invented here that the next
-- build then contradicts would be worse than the one row it saved.
--
-- market_a / market_b survive as the market each leg BELONGS to, which is what a
-- reader groups on. On an equity row they equal the series; on a rate row they
-- say whose rate it is.
--
-- ── The two count columns ──
--
-- n_obs is the calendar-date intersection of the two series after differencing —
-- the aligned observations the pair HAS. It is not the correlation's own window,
-- which window_days already states: the correlation reads the newest window_days
-- of these and the norm reads all of them, so this is the number that says
-- whether either was possible at all. norm_independent is how many
-- NON-OVERLAPPING readings the norm actually rests on.
--
-- Both were previously stated only inside the reason prose, which is where a
-- number goes to stop being checkable. grid.n_obs has carried the same fact for
-- the grid since 007, on the same rule: a count is a claim about evidence, and
-- most cross-asset pairs will sit below grid.correlation_norm_min_independent
-- for a long time. That is the honest answer, and it should be countable with
-- SQL rather than by reading sentences.
--
-- There is deliberately NO transform column. Whether a leg is differenced or
-- log-returned is a pure function of its asset class — rates are level changes
-- because a log return on a rate near zero is not a return — so a column would
-- be a second copy of a derived fact, free to drift out of step with the value
-- beside it. docs/COMPUTE.md states the mapping and a test pins it, which is the
-- same treatment the realised-vol estimators get for the same reason.

CREATE TABLE correlation_matrix_019 (
    as_of            TEXT    NOT NULL,
    -- equity | fx | rate
    asset_class_a    TEXT    NOT NULL,
    series_a         TEXT    NOT NULL,
    asset_class_b    TEXT    NOT NULL,
    series_b         TEXT    NOT NULL,
    -- The market each leg belongs to. Equal to the series on an equity leg.
    market_a         TEXT    NOT NULL,
    market_b         TEXT    NOT NULL,
    window_days      INTEGER NOT NULL,
    correlation      REAL,
    -- Aligned observations the pair has, after differencing. See the note above:
    -- the correlation reads the newest window_days of them.
    n_obs            INTEGER,
    norm_mean        REAL,
    norm_sd          REAL,
    -- Non-overlapping readings behind the norm. Below
    -- grid.correlation_norm_min_independent there is no dispersion to divide by
    -- and no shift to flag, and this is the count that says so.
    norm_independent INTEGER,
    z_score          REAL,
    flagged          INTEGER NOT NULL DEFAULT 0 CHECK (flagged IN (0, 1)),
    status           TEXT    NOT NULL CHECK (status IN ('ok', 'n/a', 'stale')),
    reason           TEXT,
    computed_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (as_of, asset_class_a, series_a, asset_class_b, series_b, window_days),
    CHECK (status = 'ok' OR reason IS NOT NULL)
) WITHOUT ROWID;

-- Every existing row is an equity-to-equity market pair, which is all the table
-- could hold. The counts are NULL rather than 0: nothing recorded them, and a
-- zero would be the claim that these rows rested on no observations.
INSERT INTO correlation_matrix_019 (
    as_of, asset_class_a, series_a, asset_class_b, series_b, market_a, market_b,
    window_days, correlation, n_obs, norm_mean, norm_sd, norm_independent, z_score,
    flagged, status, reason, computed_at)
SELECT
    as_of, 'equity', market_a, 'equity', market_b, market_a, market_b,
    window_days, correlation, NULL, norm_mean, norm_sd, NULL, z_score,
    flagged, status, reason, computed_at
FROM correlation_matrix;

DROP TABLE correlation_matrix;

ALTER TABLE correlation_matrix_019 RENAME TO correlation_matrix;

-- The flagged read is "which pairs moved", per session, and it is the one the
-- server serves. Without an index it scans a table that now holds every market's
-- equity, fx and rate legs rather than its equity alone.
CREATE INDEX IF NOT EXISTS ix_correlation_as_of_flagged
    ON correlation_matrix (as_of, flagged);
