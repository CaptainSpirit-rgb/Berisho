"""Daily price collector.

Fetches daily OHLCV bars and upserts them into ``prices_daily`` keyed by
(symbol, trade_date), so re-running is safe. Each run is incremental: it starts a few
days before the latest stored bar (to pick up late corrections) or, for a new symbol,
``lookback_days`` back so the 200-day regime MA has enough history.

The fetcher is pluggable. The default uses ``yfinance``, which is unofficial and may
break; swapping in Polygon/Tiingo later means writing another ``PriceFetcher``.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import duckdb

log = logging.getLogger(__name__)

DEFAULT_LOOKBACK_DAYS = 400  # calendar days; ~275 trading days covers a 200-day MA
REFETCH_OVERLAP_DAYS = 5


@dataclass(frozen=True)
class PriceBar:
    symbol: str
    trade_date: dt.date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    adj_close: float | None
    volume: int | None


# (symbol, start inclusive, end exclusive) -> bars
PriceFetcher = Callable[[str, dt.date, dt.date], list[PriceBar]]


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def yfinance_fetcher(symbol: str, start: dt.date, end: dt.date) -> list[PriceBar]:
    """Fetch unadjusted OHLC plus adjusted close from Yahoo Finance via yfinance."""
    import yfinance as yf  # imported lazily so tests and other commands don't need it

    df = yf.Ticker(symbol).history(
        start=start.isoformat(),
        end=end.isoformat(),
        interval="1d",
        auto_adjust=False,
        actions=False,
    )
    bars = []
    for ts, row in df.iterrows():
        volume = _num(row.get("Volume"))
        bars.append(
            PriceBar(
                symbol=symbol,
                trade_date=ts.date(),  # index is in the exchange's time zone
                open=_num(row.get("Open")),
                high=_num(row.get("High")),
                low=_num(row.get("Low")),
                close=_num(row.get("Close")),
                adj_close=_num(row.get("Adj Close")),
                volume=int(volume) if volume is not None else None,
            )
        )
    return bars


def latest_trade_date(con: duckdb.DuckDBPyConnection, symbol: str) -> dt.date | None:
    row = con.execute(
        "SELECT max(trade_date) FROM prices_daily WHERE symbol = ?", [symbol]
    ).fetchone()
    return row[0] if row else None


def upsert_bars(con: duckdb.DuckDBPyConnection, bars: Iterable[PriceBar], source: str) -> int:
    rows = [
        (b.symbol, b.trade_date, b.open, b.high, b.low, b.close, b.adj_close, b.volume, source)
        for b in bars
        if b.close is not None
    ]
    if rows:
        con.executemany(
            """
            INSERT OR REPLACE INTO prices_daily
                (symbol, trade_date, open, high, low, close, adj_close, volume, source,
                 ingested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)
            """,
            rows,
        )
    return len(rows)


def collect_prices(
    con: duckdb.DuckDBPyConnection,
    symbols: Iterable[str],
    fetcher: PriceFetcher = yfinance_fetcher,
    source: str = "yfinance",
    today: dt.date | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict[str, int]:
    """Collect daily bars for each symbol. Returns rows upserted per symbol.

    A failure for one symbol is logged and recorded as -1 so the others still run.
    """
    today = today or dt.datetime.now(dt.UTC).date()
    end = today + dt.timedelta(days=1)  # fetchers treat end as exclusive
    results: dict[str, int] = {}
    for symbol in dict.fromkeys(s.upper() for s in symbols):  # de-dup, keep order
        last = latest_trade_date(con, symbol)
        start = (
            last - dt.timedelta(days=REFETCH_OVERLAP_DAYS)
            if last
            else today - dt.timedelta(days=lookback_days)
        )
        try:
            bars = fetcher(symbol, start, end)
        except Exception:
            log.exception("Price fetch failed for %s", symbol)
            results[symbol] = -1
            continue
        results[symbol] = upsert_bars(con, bars, source)
        log.info("%s: %d bars (%s to %s)", symbol, results[symbol], start, today)
    return results
