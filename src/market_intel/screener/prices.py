"""Prices for the whole screener universe, fetched in bulk.

The daily collector fetches one ticker at a time, which is fine for a watchlist but
too slow for thousands of names, so this uses ``yfinance.download`` in chunks and
writes into the same ``prices_daily`` table (idempotent upsert). Yahoo is unofficial
and free; expect a few tickers to fail on any run.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Iterable, Sequence

import duckdb

from market_intel.collectors.prices import PriceBar, _num

log = logging.getLogger(__name__)

# (tickers, start inclusive, end exclusive) -> bars
BulkDownloader = Callable[[Sequence[str], dt.date, dt.date], list[PriceBar]]


def yfinance_bulk(tickers: Sequence[str], start: dt.date, end: dt.date) -> list[PriceBar]:
    import yfinance as yf

    df = yf.download(
        list(tickers),
        start=start.isoformat(),
        end=end.isoformat(),
        interval="1d",
        auto_adjust=False,
        actions=False,
        group_by="ticker",
        threads=True,
        progress=False,
    )
    bars: list[PriceBar] = []
    if df is None or df.empty:
        return bars
    multi = getattr(df.columns, "nlevels", 1) > 1
    for ticker in tickers:
        if multi:
            if ticker not in df.columns.get_level_values(0):
                continue
            sub = df[ticker]
        else:
            sub = df
        for ts, row in sub.iterrows():
            close = _num(row.get("Close"))
            if close is None:
                continue
            volume = _num(row.get("Volume"))
            bars.append(
                PriceBar(
                    symbol=ticker,
                    trade_date=ts.date(),
                    open=_num(row.get("Open")),
                    high=_num(row.get("High")),
                    low=_num(row.get("Low")),
                    close=close,
                    adj_close=_num(row.get("Adj Close")),
                    volume=int(volume) if volume is not None else None,
                )
            )
    return bars


def upsert_bars_bulk(con: duckdb.DuckDBPyConnection, bars: list[PriceBar], source: str) -> int:
    """Same result as collectors.prices.upsert_bars, through a DataFrame so millions of
    rows (10 years x thousands of tickers) load in seconds rather than hours."""
    import pandas as pd

    rows = [
        (b.symbol, b.trade_date, b.open, b.high, b.low, b.close, b.adj_close, b.volume)
        for b in bars
        if b.close is not None
    ]
    if not rows:
        return 0
    df = pd.DataFrame(
        rows,
        columns=["symbol", "trade_date", "open", "high", "low", "close", "adj_close", "volume"],
    )
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["volume"] = df["volume"].astype("Int64")
    # A cursor of its own, so a half-read result elsewhere on ``con`` can't block it.
    cur = con.cursor()
    cur.register("_bars_batch", df)
    try:
        cur.execute(
            """
            INSERT OR REPLACE INTO prices_daily
                (symbol, trade_date, open, high, low, close, adj_close, volume, source,
                 ingested_at)
            SELECT symbol, CAST(trade_date AS DATE), open, high, low, close, adj_close,
                   CAST(volume AS BIGINT), ?, current_timestamp
            FROM _bars_batch
            """,
            [source],
        )
    finally:
        cur.close()
    return len(rows)


def collect(
    con: duckdb.DuckDBPyConnection,
    tickers: Iterable[str],
    start: dt.date,
    end: dt.date,
    downloader: BulkDownloader = yfinance_bulk,
    chunk_size: int = 200,
) -> tuple[int, list[str]]:
    """Fetch bars for ``tickers`` between start (incl.) and end (excl.).

    Returns (rows upserted, tickers that returned no data).
    """
    tickers = list(dict.fromkeys(t.upper() for t in tickers))
    rows = 0
    missing: list[str] = []
    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i : i + chunk_size]
        try:
            bars = downloader(chunk, start, end)
        except Exception:
            log.exception("Bulk price download failed for chunk starting %s", chunk[0])
            missing.extend(chunk)
            continue
        got = {b.symbol for b in bars}
        missing.extend(t for t in chunk if t not in got)
        rows += upsert_bars_bulk(con, bars, "yfinance")
        log.info("prices: %d/%d tickers done", min(i + chunk_size, len(tickers)), len(tickers))
    return rows, missing
