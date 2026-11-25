-- Each listed company's state or country of incorporation, as its SEC submissions state it
-- (``stateOfIncorporation``): an EDGAR code, NULL where the SEC states none. Read once, with the
-- company's SIC; append-only, bounded by known_at.
-- SPDX-License-Identifier: AGPL-3.0-or-later

CREATE TABLE stock_incorporations (
    cik      INTEGER PRIMARY KEY CHECK (cik > 0),
    code     TEXT    CHECK (code IS NULL OR code GLOB '[A-Z0-9][A-Z0-9]'),
    known_at TEXT    NOT NULL CHECK (known_at GLOB
                 '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
);

CREATE TRIGGER stock_incorporations_no_update BEFORE UPDATE ON stock_incorporations
BEGIN SELECT RAISE(ABORT, 'stock_incorporations is append-only'); END;
CREATE TRIGGER stock_incorporations_no_delete BEFORE DELETE ON stock_incorporations
BEGIN SELECT RAISE(ABORT, 'stock_incorporations is append-only'); END;
