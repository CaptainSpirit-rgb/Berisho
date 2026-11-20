-- DuckDB schema for market-intel.
-- Conventions: timestamps are UTC; every raw table has a natural primary key so
-- collectors can upsert (INSERT OR REPLACE) and stay idempotent; signals are
-- derived from raw tables. Statements are idempotent (IF NOT EXISTS).

-- ---------------------------------------------------------------- reference
CREATE TABLE IF NOT EXISTS securities (
    symbol       VARCHAR PRIMARY KEY,
    name         VARCHAR,
    cik          BIGINT,
    sector_etf   VARCHAR,
    on_watchlist BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at   TIMESTAMP NOT NULL DEFAULT current_timestamp
);

-- ---------------------------------------------------------------- raw data
CREATE TABLE IF NOT EXISTS prices_daily (
    symbol      VARCHAR NOT NULL,
    trade_date  DATE NOT NULL,
    open        DOUBLE,
    high        DOUBLE,
    low         DOUBLE,
    close       DOUBLE,
    adj_close   DOUBLE,
    volume      BIGINT,
    source      VARCHAR NOT NULL,
    ingested_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
    PRIMARY KEY (symbol, trade_date)
);

-- SEC filings index (8-K, 10-Q, 10-K, Form 4, 13D/13G, ...).
-- accepted_at is the point-in-time timestamp to use in backtests.
CREATE TABLE IF NOT EXISTS filings (
    accession_no VARCHAR PRIMARY KEY,
    cik          BIGINT NOT NULL,
    symbol       VARCHAR,
    form_type    VARCHAR NOT NULL,
    filed_date   DATE,
    accepted_at  TIMESTAMP,
    period_of_report DATE,
    items        VARCHAR,          -- 8-K item numbers, comma-separated
    primary_doc_url VARCHAR,
    details_parsed_at TIMESTAMP,       -- set once the document (e.g. Form 4 XML) is parsed
    subject_cik  BIGINT,           -- company the filing is about; = cik when it's about us
    ingested_at  TIMESTAMP NOT NULL DEFAULT current_timestamp
);
-- Databases created before subject_cik existed.
ALTER TABLE filings ADD COLUMN IF NOT EXISTS subject_cik BIGINT;

-- Form 4 transactions (one row per reported transaction line).
CREATE TABLE IF NOT EXISTS insider_transactions (
    accession_no     VARCHAR NOT NULL,
    line_no          INTEGER NOT NULL,
    cik              BIGINT NOT NULL,
    symbol           VARCHAR,
    insider_name     VARCHAR,
    insider_title    VARCHAR,
    transaction_date DATE,
    transaction_code VARCHAR,      -- P = open-market purchase, S = sale, ...
    acquired_disposed VARCHAR,     -- A = acquired, D = disposed
    shares           DOUBLE,
    price            DOUBLE,
    shares_after     DOUBLE,
    accepted_at      TIMESTAMP,
    ingested_at      TIMESTAMP NOT NULL DEFAULT current_timestamp,
    PRIMARY KEY (accession_no, line_no)
);

-- EDGAR XBRL facts (companyfacts / frames).
CREATE TABLE IF NOT EXISTS fundamentals (
    cik            BIGINT NOT NULL,
    taxonomy       VARCHAR NOT NULL,   -- us-gaap, dei, ...
    concept        VARCHAR NOT NULL,   -- e.g. Revenues
    unit           VARCHAR NOT NULL,   -- USD, shares, ...
    period_start   DATE,
    period_end     DATE NOT NULL,
    fiscal_year    INTEGER,
    fiscal_period  VARCHAR,            -- FY, Q1, ...
    form_type      VARCHAR,
    value          DOUBLE,
    accession_no   VARCHAR NOT NULL,
    filed_date     DATE NOT NULL,      -- point-in-time key, not period_end
    ingested_at    TIMESTAMP NOT NULL DEFAULT current_timestamp,
    PRIMARY KEY (cik, taxonomy, concept, unit, period_end, accession_no)
);

CREATE TABLE IF NOT EXISTS news (
    news_id      VARCHAR PRIMARY KEY,  -- stable hash of source + url (or headline)
    symbol       VARCHAR,
    published_at TIMESTAMP NOT NULL,
    source       VARCHAR NOT NULL,
    headline     VARCHAR NOT NULL,
    summary      VARCHAR,
    url          VARCHAR,
    sentiment_score DOUBLE,            -- filled later (FinBERT)
    sentiment_label VARCHAR,
    ingested_at  TIMESTAMP NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS macro_series (
    series_id   VARCHAR NOT NULL,      -- FRED id, e.g. DGS10
    obs_date    DATE NOT NULL,
    value       DOUBLE,
    ingested_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
    PRIMARY KEY (series_id, obs_date)
);

-- ---------------------------------------------------------------- derived
CREATE TABLE IF NOT EXISTS market_regime (
    as_of_date DATE PRIMARY KEY,
    benchmark  VARCHAR NOT NULL,
    close      DOUBLE,
    ma_200     DOUBLE,
    regime     VARCHAR NOT NULL CHECK (regime IN ('bull', 'bear'))
);

CREATE TABLE IF NOT EXISTS signals (
    signal_id   VARCHAR PRIMARY KEY,   -- hash of type + symbol + as_of + source key
    symbol      VARCHAR NOT NULL,
    signal_type VARCHAR NOT NULL,      -- price_gap, volume_spike, insider_cluster_buy, new_8k, ...
    as_of       TIMESTAMP NOT NULL,
    strength    DOUBLE,                -- normalised 0..1
    details     JSON,                  -- inputs that produced the signal
    source_ref  VARCHAR,               -- e.g. accession_no or news_id
    created_at  TIMESTAMP NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS scores (
    run_date     DATE NOT NULL,
    symbol       VARCHAR NOT NULL,
    score        DOUBLE NOT NULL,
    regime       VARCHAR,
    contributions JSON,                -- signal_id -> weighted contribution (explainability)
    created_at   TIMESTAMP NOT NULL DEFAULT current_timestamp,
    PRIMARY KEY (run_date, symbol)
);

-- Cached Claude briefs plus token usage and cost per call.
CREATE TABLE IF NOT EXISTS briefs (
    brief_id      VARCHAR PRIMARY KEY, -- hash of symbol + inputs (cache key)
    run_date      DATE NOT NULL,
    symbol        VARCHAR NOT NULL,
    model         VARCHAR,
    content       VARCHAR NOT NULL,
    sources       JSON,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_usd      DOUBLE,
    created_at    TIMESTAMP NOT NULL DEFAULT current_timestamp
);

-- ---------------------------------------------------------------- journal
CREATE SEQUENCE IF NOT EXISTS journal_id_seq START 1;

CREATE TABLE IF NOT EXISTS journal (
    idea_id     INTEGER PRIMARY KEY DEFAULT nextval('journal_id_seq'),
    idea_date   DATE NOT NULL,
    symbol      VARCHAR NOT NULL,
    signal_ids  JSON,
    score       DOUBLE,
    action      VARCHAR NOT NULL CHECK (action IN ('ignored', 'watch', 'bought', 'sold')),
    notes       VARCHAR,
    created_at  TIMESTAMP NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS journal_outcomes (
    idea_id        INTEGER NOT NULL,
    horizon_days   INTEGER NOT NULL CHECK (horizon_days IN (5, 20, 60)),
    return_pct     DOUBLE,
    spy_return_pct DOUBLE,
    sector_return_pct DOUBLE,
    computed_at    TIMESTAMP NOT NULL DEFAULT current_timestamp,
    PRIMARY KEY (idea_id, horizon_days)
);

-- ---------------------------------------------------------------- filing text
-- Plain text of a filing's main document and press-release exhibits (EX-99.x), fetched
-- for recent 8-Ks so the brief can say what a filing actually says.
CREATE TABLE IF NOT EXISTS filing_documents (
    accession_no VARCHAR NOT NULL,
    doc_name     VARCHAR NOT NULL,
    doc_type     VARCHAR,              -- 8-K, EX-99.1, ...
    description  VARCHAR,
    url          VARCHAR,
    text         VARCHAR,
    fetched_at   TIMESTAMP NOT NULL DEFAULT current_timestamp,
    PRIMARY KEY (accession_no, doc_name)
);
