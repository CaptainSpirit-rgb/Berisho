"""Phase 2 screener: find small and mid caps worth a closer look.

Pipeline (weekly): ``universe`` (who is listed) -> ``facts`` (SEC XBRL financials) ->
``prices`` (latest price for market cap) -> ``metrics`` (point-in-time numbers) ->
``screen`` (filters, percentile scores, plain-language reasons) -> digest section.
``backtest`` replays the same screen on past dates using only data published by then.
"""

from pathlib import Path

import duckdb

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create the screener tables. Safe to run repeatedly."""
    con.execute(SCHEMA_PATH.read_text())
