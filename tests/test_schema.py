import datetime as dt

import duckdb
import pytest

from market_intel.config import Security
from market_intel.storage import db

EXPECTED_TABLES = {
    "securities",
    "prices_daily",
    "filings",
    "insider_transactions",
    "fundamentals",
    "news",
    "macro_series",
    "market_regime",
    "signals",
    "scores",
    "briefs",
    "journal",
    "journal_outcomes",
}


def test_schema_creates_expected_tables(con):
    assert EXPECTED_TABLES <= set(db.list_tables(con))


def test_init_schema_is_idempotent(con):
    db.init_schema(con)
    db.init_schema(con)
    assert EXPECTED_TABLES <= set(db.list_tables(con))


def test_init_schema_adds_subject_cik_to_old_filings_table():
    con = db.connect(":memory:")
    con.execute("CREATE TABLE filings (accession_no VARCHAR PRIMARY KEY, cik BIGINT NOT NULL)")
    db.init_schema(con)
    cols = {r[0] for r in con.execute("DESCRIBE filings").fetchall()}
    assert "subject_cik" in cols
    con.close()


def test_prices_upsert_by_natural_key(con):
    sql = """
        INSERT OR REPLACE INTO prices_daily (symbol, trade_date, close, volume, source)
        VALUES (?, ?, ?, ?, 'test')
    """
    day = dt.date(2026, 11, 19)
    con.execute(sql, ["AAPL", day, 100.0, 1_000])
    con.execute(sql, ["AAPL", day, 101.5, 2_000])  # re-run of the same day
    rows = con.execute("SELECT close, volume FROM prices_daily").fetchall()
    assert rows == [(101.5, 2_000)]


def test_journal_rejects_unknown_action(con):
    with pytest.raises(duckdb.ConstraintException):
        con.execute(
            "INSERT INTO journal (idea_date, symbol, action) VALUES (current_date, 'AAPL', 'yolo')"
        )


def test_journal_assigns_ids(con):
    sql = "INSERT INTO journal (idea_date, symbol, action) VALUES (current_date, ?, ?)"
    con.execute(sql, ["AAPL", "watch"])
    con.execute(sql, ["GS", "ignored"])
    assert con.execute("SELECT idea_id FROM journal ORDER BY idea_id").fetchall() == [(1,), (2,)]


def test_sync_watchlist_flags_removed_symbols(con):
    db.sync_watchlist(con, [Security("AAPL", "Apple"), Security("XOM", "Exxon")])
    db.sync_watchlist(con, [Security("AAPL", "Apple")])
    rows = con.execute("SELECT symbol, on_watchlist FROM securities ORDER BY symbol").fetchall()
    assert rows == [("AAPL", True), ("XOM", False)]


def test_file_database_persists(tmp_path):
    path = tmp_path / "nested" / "test.duckdb"
    con = db.connect(path)
    db.init_schema(con)
    con.close()
    con = db.connect(path)
    assert EXPECTED_TABLES <= set(db.list_tables(con))
    con.close()
