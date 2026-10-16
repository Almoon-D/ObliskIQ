-- 004_research_tax — research history and its trial registry, and the tax lots.
--
-- Forward-only. Conventions as in 001_core. Research tables live in the research database
-- (paths.research_database); the live decision path never reads them (an import contract keeps
-- obliskiq.store.research away from decision, execution, engines and ibkr).

-- ───────────────────────────── research series ─────────────────────────────

-- A series and where it came from: vendor, URL, download stamp, the SHA-256 of the payload, how
-- its availability dates were derived and its known biases (a JSON array of notes). A reload
-- replaces a series whole, metadata and rows together.
CREATE TABLE research_series (
    series_id      TEXT PRIMARY KEY CHECK (trim(series_id) <> ''),
    dataset_id     TEXT NOT NULL CHECK (trim(dataset_id) <> ''),
    vendor         TEXT NOT NULL CHECK (vendor IN ('yahoo', 'ken_french', 'aqr', 'fred',
                       'alfred', 'ecb')),
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

CREATE INDEX ix_research_series_dataset ON research_series (dataset_id);

-- One value per period and vintage. observed_on is the period the value describes;
-- available_on is when it became public, and every read is bounded by it.
CREATE TABLE research_history (
    series_id    TEXT NOT NULL REFERENCES research_series (series_id) ON DELETE CASCADE,
    observed_on  TEXT NOT NULL CHECK (observed_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    available_on TEXT NOT NULL CHECK (available_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    value        REAL NOT NULL,
    PRIMARY KEY (series_id, observed_on, available_on),
    CHECK (available_on >= observed_on)
) WITHOUT ROWID;

CREATE INDEX ix_research_history_available ON research_history (series_id, available_on);

-- ───────────────────────────── trial registry ─────────────────────────────

-- Every run of a study, exploratory or the confirmation. Append-only: a trial that happened
-- counts in the deflated Sharpe whatever it found.
CREATE TABLE research_trials (
    trial_id      TEXT PRIMARY KEY CHECK (trim(trial_id) <> ''),
    study_id      TEXT NOT NULL CHECK (trim(study_id) <> ''),
    stage         TEXT NOT NULL CHECK (stage IN ('exploration', 'confirmation')),
    recorded_at   TEXT NOT NULL CHECK (recorded_at GLOB
                      '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    spec_sha256   TEXT NOT NULL CHECK (length(spec_sha256) = 64
                      AND spec_sha256 NOT GLOB '*[^0-9a-f]*'),
    inputs_sha256 TEXT NOT NULL CHECK (length(inputs_sha256) = 64
                      AND inputs_sha256 NOT GLOB '*[^0-9a-f]*'),
    parameters    TEXT NOT NULL CHECK (json_valid(parameters)),
    metric        TEXT NOT NULL CHECK (trim(metric) <> ''),
    estimate      REAL,
    sharpe        REAL,
    observations  INTEGER CHECK (observations IS NULL OR observations >= 0),
    outcome       TEXT
) WITHOUT ROWID;

CREATE INDEX ix_research_trials_study ON research_trials (study_id, recorded_at);

CREATE TRIGGER research_trials_no_update BEFORE UPDATE ON research_trials
BEGIN
    SELECT RAISE(ABORT, 'research_trials is append-only');
END;

CREATE TRIGGER research_trials_no_delete BEFORE DELETE ON research_trials
BEGIN
    SELECT RAISE(ABORT, 'research_trials is append-only');
END;

-- ───────────────────────────── tax lots ─────────────────────────────

-- The open lots of the lot engine, saved whole (Session 7 wires them to fills). Amounts are
-- decimals written as text, in the base currency.
CREATE TABLE tax_lots (
    lot_id            TEXT PRIMARY KEY CHECK (trim(lot_id) <> ''),
    security          TEXT NOT NULL CHECK (trim(security) <> ''),
    acquired_on       TEXT NOT NULL CHECK (acquired_on GLOB
                          '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    quantity          TEXT NOT NULL,
    cost              TEXT NOT NULL,
    deferred_loss     TEXT NOT NULL,
    replacement_units TEXT NOT NULL,
    saved_at          TEXT NOT NULL CHECK (saved_at GLOB
                          '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
) WITHOUT ROWID;

CREATE INDEX ix_tax_lots_security ON tax_lots (security, acquired_on);
