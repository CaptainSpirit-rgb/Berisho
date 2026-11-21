-- Screener tables (Phase 2). Kept apart from storage/schema.sql so the screener can
-- evolve without touching the daily-digest schema. Idempotent like the main schema.

-- Candidate universe: the companies the screen ranks (S&P 500 members by default).
CREATE TABLE IF NOT EXISTS screen_universe (
    cik             BIGINT PRIMARY KEY,
    ticker          VARCHAR NOT NULL,
    name            VARCHAR,
    exchange        VARCHAR,
    sic             INTEGER,           -- from the submissions API, NULL until fetched
    sic_description VARCHAR,
    updated_at      TIMESTAMP NOT NULL DEFAULT current_timestamp
);
ALTER TABLE screen_universe ADD COLUMN IF NOT EXISTS sector VARCHAR;        -- GICS sector
ALTER TABLE screen_universe ADD COLUMN IF NOT EXISTS sub_industry VARCHAR;  -- GICS sub-industry

-- XBRL facts from SEC companyfacts, only the concepts the screener uses.
-- One row per reported value. The same period is usually reported again in later
-- filings (as a comparative or a restatement); each report is its own row, so a
-- point-in-time query takes the latest row with filed_date <= the as-of date.
-- duration_days is 0 for balance-sheet (instant) values; it is part of the key because
-- a 10-Q reports both the 3-month and the year-to-date value for the same period end.
CREATE TABLE IF NOT EXISTS xbrl_facts (
    cik           BIGINT NOT NULL,
    metric        VARCHAR NOT NULL,    -- screener name, e.g. revenue (see facts.CONCEPTS)
    concept       VARCHAR NOT NULL,    -- XBRL concept it came from, e.g. Revenues
    period_start  DATE,
    period_end    DATE NOT NULL,
    duration_days INTEGER NOT NULL,
    value         DOUBLE NOT NULL,
    form_type     VARCHAR,
    accession_no  VARCHAR NOT NULL,
    filed_date    DATE NOT NULL,       -- point-in-time key, not period_end
    ingested_at   TIMESTAMP NOT NULL DEFAULT current_timestamp,
    PRIMARY KEY (cik, metric, concept, period_end, duration_days, accession_no)
);

-- Output of each weekly screen: the ranked candidates and why they ranked.
CREATE TABLE IF NOT EXISTS screen_results (
    run_date   DATE NOT NULL,
    cik        BIGINT NOT NULL,
    ticker     VARCHAR NOT NULL,
    name       VARCHAR,
    rank       INTEGER NOT NULL,
    score      DOUBLE NOT NULL,
    pillars    JSON,                   -- growth / quality / balance / value scores 0..1
    metrics    JSON,                   -- raw numbers behind the score
    reasons    JSON,                   -- plain-language reasons it passed
    warnings   JSON,                   -- things to check before trusting it
    created_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
    PRIMARY KEY (run_date, cik)
);
