"""Market regime from the thesis rule: the benchmark (SPY) above its 200-day moving average
is a bull market, below it a bear market (Chau, Deesomsak & Koutmos, 2016)."""

from __future__ import annotations

import datetime as dt

import duckdb


def current_regime(
    con: duckdb.DuckDBPyConnection,
    benchmark: str = "SPY",
    ma_days: int = 200,
    as_of: dt.date | None = None,
) -> str | None:
    """'bull', 'bear', or None when there isn't ``ma_days`` of price history yet."""
    rows = con.execute(
        """
        SELECT coalesce(adj_close, close) FROM prices_daily
        WHERE symbol = ? AND trade_date <= coalesce(?, DATE '9999-12-31')
        ORDER BY trade_date DESC LIMIT ?
        """,
        [benchmark, as_of, ma_days],
    ).fetchall()
    if len(rows) < ma_days or any(r[0] is None for r in rows):
        return None
    close = rows[0][0]
    ma = sum(r[0] for r in rows) / ma_days
    return "bull" if close > ma else "bear"
