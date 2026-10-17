-- 005_research_vendors — the research vendors MSCI, the ECB Data Portal and the Bundesbank, and
-- series derived from stored ones.
--
-- Forward-only. SQLite cannot change a CHECK in place, so research_series is rebuilt with the
-- wider vendor list and its rows copied; the runner holds foreign keys off for the rebuild and
-- checks them afterwards, so research_history keeps every row and its reference.

CREATE TABLE research_series_new (
    series_id      TEXT PRIMARY KEY CHECK (trim(series_id) <> ''),
    dataset_id     TEXT NOT NULL CHECK (trim(dataset_id) <> ''),
    vendor         TEXT NOT NULL CHECK (vendor IN ('yahoo', 'ken_french', 'aqr', 'fred',
                       'alfred', 'ecb', 'msci', 'ecb_data', 'bundesbank', 'derived')),
    field          TEXT NOT NULL CHECK (trim(field) <> ''),
    units          TEXT NOT NULL CHECK (trim(units) <> ''),
    currency       TEXT CHECK (currency IS NULL OR currency GLOB '[A-Z][A-Z][A-Z]'),
    frequency      TEXT NOT NULL CHECK (frequency IN ('daily', 'monthly', 'annual')),
    point_in_time  TEXT NOT NULL CHECK (point_in_time IN ('vintages', 'as_observed',
                       'release_lag')),
    source_url     TEXT NOT NULL CHECK (trim(source_url) <> ''),
    downloaded_at  TEXT NOT NULL CHECK (downloaded_at GLOB
                       '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64
                       AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
    bias_notes     TEXT NOT NULL CHECK (json_valid(bias_notes)
                       AND json_type(bias_notes) = 'array'
                       AND json_array_length(bias_notes) > 0)
) WITHOUT ROWID;

INSERT INTO research_series_new SELECT * FROM research_series;

-- Dropping the table drops its index; the index is made again on the rebuilt table.
DROP TABLE research_series;
ALTER TABLE research_series_new RENAME TO research_series;

CREATE INDEX ix_research_series_dataset ON research_series (dataset_id);
