-- 017_scoreboard — what the scoreboard reads and decides (MASTER_PLAN §5.9, §5.19; DECISIONS
-- D62, D65; research/preregistrations/scoreboard.md): the calls a layer makes, each layer's
-- status, its monthly record beside its twin, the monitoring looks, the capital each layer is
-- given, the component weight sets and the operational drills of the promotion ladder.
--
-- Forward-only. Conventions as in 001_core and 002_orders (money as TEXT holding a Decimal's
-- exact digits, dates 'YYYY-MM-DD', stamps 'YYYY-MM-DDTHH:MM:SSZ'). Every table here is a
-- record: append-only, enforced by trigger, and a look is written once.

-- ───────────────────────────── scoreboard_calls ─────────────────────────────

-- A call a layer made on an instrument, stamped when made: a signal, a component, a pattern, a
-- forecast of the weekly panel. direction 'up' says it will do better than the equity
-- reference over the grading horizon, 'down' worse. probability is the layer's stated
-- probability that the call is right over that horizon, base_rate the market's or the random
-- walk's for the same event;
-- expected_excess and baseline are the panel's expected excess return and 12-1 momentum.
CREATE TABLE scoreboard_calls (
    call_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    layer           TEXT    NOT NULL CHECK (trim(layer) <> ''),
    conid           INTEGER NOT NULL CHECK (conid > 0),
    direction       TEXT    NOT NULL CHECK (direction IN ('up', 'down')),
    called_at       TEXT    NOT NULL CHECK (called_at GLOB
                        '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    probability     REAL    CHECK (probability IS NULL OR (probability >= 0 AND probability <= 1)),
    base_rate       REAL    CHECK (base_rate IS NULL OR (base_rate >= 0 AND base_rate <= 1)),
    expected_excess REAL,
    baseline        REAL,
    detail          TEXT    NOT NULL CHECK (trim(detail) <> '')
);

CREATE INDEX ix_scoreboard_calls_layer ON scoreboard_calls (layer, called_at);

CREATE TRIGGER scoreboard_calls_no_update BEFORE UPDATE ON scoreboard_calls
BEGIN
    SELECT RAISE(ABORT, 'scoreboard_calls is append-only');
END;

CREATE TRIGGER scoreboard_calls_no_delete BEFORE DELETE ON scoreboard_calls
BEGIN
    SELECT RAISE(ABORT, 'scoreboard_calls is append-only');
END;

-- ───────────────────────────── layer_events ─────────────────────────────

-- A layer's lifecycle on the running system: 'active', 'shadow' (zero weight), 'freeze' (no new
-- entries, after a gap breach) and 'unfreeze' (the owner, after the investigation). A layer's
-- status is its latest 'active' or 'shadow' event, else its registry status; it is frozen when
-- its latest 'freeze' is later than its latest 'unfreeze'.
CREATE TABLE layer_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT    NOT NULL CHECK (at GLOB
                 '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    layer    TEXT    NOT NULL CHECK (trim(layer) <> ''),
    event    TEXT    NOT NULL CHECK (event IN ('active', 'shadow', 'freeze', 'unfreeze')),
    actor    TEXT    NOT NULL CHECK (trim(actor) <> ''),
    detail   TEXT    NOT NULL CHECK (trim(detail) <> '')
);

CREATE INDEX ix_layer_events_layer ON layer_events (layer, event_id);

CREATE TRIGGER layer_events_no_update BEFORE UPDATE ON layer_events
BEGIN
    SELECT RAISE(ABORT, 'layer_events is append-only');
END;

CREATE TRIGGER layer_events_no_delete BEFORE DELETE ON layer_events
BEGIN
    SELECT RAISE(ABORT, 'layer_events is append-only');
END;

-- ───────────────────────────── layer_months ─────────────────────────────

-- An active layer's month: its realised log return on its capital in the mode it traded (mode),
-- its twin's (the same signals on the same dates through the cost model) and its reference's, as
-- its test measured it, and the positions it closed that month. NULL: not measured that month
-- (the look that needs it is not evaluable). Written once per month by the layer's engine.
CREATE TABLE layer_months (
    layer       TEXT NOT NULL CHECK (trim(layer) <> ''),
    month       TEXT NOT NULL CHECK (month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
    mode        TEXT NOT NULL CHECK (mode IN ('shadow', 'paper', 'live-small', 'live')),
    closed      INTEGER NOT NULL CHECK (closed >= 0),
    live        REAL,
    twin        REAL,
    reference   REAL,
    recorded_at TEXT NOT NULL CHECK (recorded_at GLOB
                    '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (layer, month)
) WITHOUT ROWID;

CREATE TRIGGER layer_months_no_update BEFORE UPDATE ON layer_months
BEGIN
    SELECT RAISE(ABORT, 'a layer''s month is written once');
END;

CREATE TRIGGER layer_months_no_delete BEFORE DELETE ON layer_months
BEGIN
    SELECT RAISE(ABORT, 'a layer''s month is written once');
END;

-- ───────────────────────────── monitor_looks ─────────────────────────────

-- One monthly look at one boundary of one layer (D65): the statistic, the critical value it was
-- held against, the alpha that look spent and whether it breached. A look that could not be
-- measured is recorded not evaluable (statistic and critical NULL) and is not repeated.
CREATE TABLE monitor_looks (
    layer     TEXT    NOT NULL CHECK (trim(layer) <> ''),
    month     TEXT    NOT NULL CHECK (month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
    boundary  TEXT    NOT NULL CHECK (boundary IN ('gap', 'twin')),
    statistic REAL,
    critical  REAL,
    spent     REAL    NOT NULL CHECK (spent > 0 AND spent < 1),
    breached  INTEGER NOT NULL CHECK (breached IN (0, 1)),
    evaluable INTEGER NOT NULL CHECK (evaluable IN (0, 1)),
    detail    TEXT    NOT NULL CHECK (trim(detail) <> ''),
    at        TEXT    NOT NULL CHECK (at GLOB
                  '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (layer, month, boundary),
    CHECK (evaluable = 1 OR (statistic IS NULL AND breached = 0)),
    CHECK ((statistic IS NULL) = (critical IS NULL))
) WITHOUT ROWID;

CREATE TRIGGER monitor_looks_no_update BEFORE UPDATE ON monitor_looks
BEGIN
    SELECT RAISE(ABORT, 'a look is written once');
END;

CREATE TRIGGER monitor_looks_no_delete BEFORE DELETE ON monitor_looks
BEGIN
    SELECT RAISE(ABORT, 'a look is written once');
END;

-- ───────────────────────────── allocation_events ─────────────────────────────

-- A layer's share of investable capital on the live account, as the allocation rule set it
-- (D65, D149, D155, D156): its latest row is its share. Every change is audited here.
CREATE TABLE allocation_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT    NOT NULL CHECK (at GLOB
                 '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    layer    TEXT    NOT NULL CHECK (trim(layer) <> ''),
    share    TEXT    NOT NULL CHECK (CAST(share AS REAL) >= 0 AND CAST(share AS REAL) <= 1),
    actor    TEXT    NOT NULL CHECK (trim(actor) <> ''),
    reason   TEXT    NOT NULL CHECK (trim(reason) <> '')
);

CREATE INDEX ix_allocation_events_layer ON allocation_events (layer, event_id);

CREATE TRIGGER allocation_events_no_update BEFORE UPDATE ON allocation_events
BEGIN
    SELECT RAISE(ABORT, 'allocation_events is append-only');
END;

CREATE TRIGGER allocation_events_no_delete BEFORE DELETE ON allocation_events
BEGIN
    SELECT RAISE(ABORT, 'allocation_events is append-only');
END;

-- ───────────────────────────── weight_sets ─────────────────────────────

-- A layer's component weights as the empirical-Bayes rule set them, versioned by the SHA-256 of
-- their canonical JSON.
CREATE TABLE weight_sets (
    layer   TEXT NOT NULL CHECK (trim(layer) <> ''),
    sha256  TEXT NOT NULL CHECK (length(sha256) = 64),
    weights TEXT NOT NULL CHECK (json_valid(weights)),
    at      TEXT NOT NULL CHECK (at GLOB
                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (layer, sha256)
) WITHOUT ROWID;

CREATE TRIGGER weight_sets_no_update BEFORE UPDATE ON weight_sets
BEGIN
    SELECT RAISE(ABORT, 'a weight set never changes');
END;

CREATE TRIGGER weight_sets_no_delete BEFORE DELETE ON weight_sets
BEGIN
    SELECT RAISE(ABORT, 'a weight set never changes');
END;

-- ───────────────────────────── drills ─────────────────────────────

-- An operational drill of the promotion ladder (MASTER_PLAN §5.19: the breaker, the kill switch
-- and a cash flow), run in a mode and recorded by the owner with what was seen.
CREATE TABLE drills (
    drill_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT    NOT NULL CHECK (at GLOB
                 '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    kind     TEXT    NOT NULL CHECK (kind IN ('breaker', 'kill', 'cash_flow')),
    mode     TEXT    NOT NULL CHECK (mode IN ('shadow', 'paper', 'live-small', 'live')),
    passed   INTEGER NOT NULL CHECK (passed IN (0, 1)),
    actor    TEXT    NOT NULL CHECK (trim(actor) <> ''),
    detail   TEXT    NOT NULL CHECK (trim(detail) <> '')
);

CREATE TRIGGER drills_no_update BEFORE UPDATE ON drills
BEGIN
    SELECT RAISE(ABORT, 'drills is append-only');
END;

CREATE TRIGGER drills_no_delete BEFORE DELETE ON drills
BEGIN
    SELECT RAISE(ABORT, 'drills is append-only');
END;
