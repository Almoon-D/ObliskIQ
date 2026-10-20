-- 007_service — the account's net asset value over time, written by the service.
--
-- Forward-only. Conventions as in 001_core and 002_orders (money as TEXT holding a Decimal's
-- exact digits). The run log the service writes is 001_core's runs and run_steps.

-- ───────────────────────────── nav_history ─────────────────────────────

-- One reading of the account's net liquidation value per day, keyed on the day it was TAKEN, never
-- the day it is about (ported from lite's nav_history; store/nav.py is its one writer). A second
-- reading on the same day corrects that day. currency is the base currency the value is in.
CREATE TABLE nav_history (
    as_of       TEXT NOT NULL PRIMARY KEY CHECK (as_of GLOB
                    '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    nav_base    TEXT NOT NULL CHECK (trim(nav_base) <> '' AND CAST(nav_base AS REAL) > 0),
    currency    TEXT NOT NULL CHECK (currency GLOB '[A-Z][A-Z][A-Z]'),
    source      TEXT NOT NULL CHECK (trim(source) <> ''),
    recorded_at TEXT NOT NULL CHECK (recorded_at GLOB
                    '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
) WITHOUT ROWID;
