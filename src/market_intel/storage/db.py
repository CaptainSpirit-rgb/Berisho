"""DuckDB connection and schema management."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import duckdb

from market_intel.config import Security

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(path: str | Path = ":memory:") -> duckdb.DuckDBPyConnection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path))


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create all tables. Safe to run repeatedly."""
    con.execute(SCHEMA_PATH.read_text())


def list_tables(con: duckdb.DuckDBPyConnection) -> list[str]:
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' ORDER BY table_name"
    ).fetchall()
    return [r[0] for r in rows]


def sync_watchlist(con: duckdb.DuckDBPyConnection, securities: Iterable[Security]) -> int:
    """Upsert watchlist securities and flag anything no longer listed as off-watchlist."""
    securities = list(securities)
    con.execute("UPDATE securities SET on_watchlist = FALSE")
    for s in securities:
        con.execute(
            """
            INSERT OR REPLACE INTO securities
                (symbol, name, cik, sector_etf, on_watchlist, updated_at)
            VALUES (?, ?, ?, ?, TRUE, current_timestamp)
            """,
            [s.symbol, s.name, s.cik, s.sector_etf],
        )
    return len(securities)
