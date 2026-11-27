-- 024_sec_filings — the SEC's filings collected forward for the fact sheet's events and the
-- analyst's dossier (MASTER_PLAN §5.5; DECISIONS D189, D200): each filing of a kept form as the
-- daily index lists it, with an 8-K's items from its header; each pricing prospectus as read; and
-- each section of a report read for a dossier.
--
-- Conventions as in 001_core (dates 'YYYY-MM-DD', stamps 'YYYY-MM-DDTHH:MM:SSZ'). Every table is a
-- record: append-only, enforced by trigger.
-- SPDX-License-Identifier: AGPL-3.0-or-later

CREATE TABLE sec_filings (
    accession   TEXT    PRIMARY KEY CHECK (trim(accession) <> ''),
    cik         INTEGER NOT NULL CHECK (cik > 0),
    company     TEXT    NOT NULL CHECK (trim(company) <> ''),
    form        TEXT    NOT NULL CHECK (trim(form) <> ''),
    filed       TEXT    NOT NULL CHECK (filed GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    path        TEXT    NOT NULL CHECK (trim(path) <> ''),
    -- A JSON list of item numbers ('2.02'); NULL when the header was not read.
    items       TEXT,
    accepted_at TEXT    CHECK (accepted_at IS NULL OR accepted_at GLOB
                    '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    known_at    TEXT    NOT NULL CHECK (known_at GLOB
                    '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
) WITHOUT ROWID;

CREATE INDEX ix_sec_filings_filed ON sec_filings (filed);
CREATE INDEX ix_sec_filings_cik ON sec_filings (cik, filed);

CREATE TRIGGER sec_filings_no_update BEFORE UPDATE ON sec_filings
BEGIN SELECT RAISE(ABORT, 'sec_filings is append-only'); END;
CREATE TRIGGER sec_filings_no_delete BEFORE DELETE ON sec_filings
BEGIN SELECT RAISE(ABORT, 'sec_filings is append-only'); END;

-- A pricing prospectus as read, once: an initial public offering's terms, or why there are none.
CREATE TABLE ipo_terms (
    accession      TEXT    PRIMARY KEY REFERENCES sec_filings (accession),
    cik            INTEGER NOT NULL CHECK (cik > 0),
    status         TEXT    NOT NULL CHECK (status IN ('ipo', 'not_ipo', 'failed')),
    price          TEXT,
    shares         INTEGER CHECK (shares IS NULL OR shares > 0),
    lockup_days    INTEGER CHECK (lockup_days IS NULL OR lockup_days > 0),
    prospectus_day TEXT    CHECK (prospectus_day IS NULL OR prospectus_day GLOB
                       '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    lockup_ends    TEXT    CHECK (lockup_ends IS NULL OR lockup_ends GLOB
                       '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    detail         TEXT,
    known_at       TEXT    NOT NULL CHECK (known_at GLOB
                       '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    CHECK ((lockup_days IS NULL) = (lockup_ends IS NULL))
) WITHOUT ROWID;

CREATE TRIGGER ipo_terms_no_update BEFORE UPDATE ON ipo_terms
BEGIN SELECT RAISE(ABORT, 'ipo_terms is append-only'); END;
CREATE TRIGGER ipo_terms_no_delete BEFORE DELETE ON ipo_terms
BEGIN SELECT RAISE(ABORT, 'ipo_terms is append-only'); END;

-- One section of a report read for a dossier, with its accession; read once.
CREATE TABLE filing_texts (
    accession TEXT    NOT NULL CHECK (trim(accession) <> ''),
    section   TEXT    NOT NULL CHECK (trim(section) <> ''),
    cik       INTEGER NOT NULL CHECK (cik > 0),
    form      TEXT    NOT NULL CHECK (trim(form) <> ''),
    filed     TEXT    NOT NULL CHECK (filed GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    status    TEXT    NOT NULL CHECK (status IN ('ok', 'not_found', 'failed')),
    text      TEXT,
    detail    TEXT,
    known_at  TEXT    NOT NULL CHECK (known_at GLOB
                  '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    PRIMARY KEY (accession, section),
    CHECK ((status = 'ok') = (text IS NOT NULL AND text <> ''))
) WITHOUT ROWID;

CREATE INDEX ix_filing_texts_cik ON filing_texts (cik, filed);

CREATE TRIGGER filing_texts_no_update BEFORE UPDATE ON filing_texts
BEGIN SELECT RAISE(ABORT, 'filing_texts is append-only'); END;
CREATE TRIGGER filing_texts_no_delete BEFORE DELETE ON filing_texts
BEGIN SELECT RAISE(ABORT, 'filing_texts is append-only'); END;
