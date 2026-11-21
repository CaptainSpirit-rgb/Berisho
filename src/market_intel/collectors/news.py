"""News headlines for the watchlist (free, no API key).

Headlines come from Yahoo Finance through ``yfinance`` (the library already used for
prices). One row per (headline, ticker) goes into ``news``: ``published_at`` is the
article's publish time in UTC (stored naive, like every table), ``source`` the publisher.
``news_id`` is sha256(source|url|symbol), so the same article listed for two tickers
gets two rows. Inserts never overwrite an existing row, so sentiment scores filled in
later survive re-fetches.

yfinance has returned two shapes over time: the current ``{"id", "content": {...}}`` and
the older flat ``{"title", "publisher", "link", "providerPublishTime"}``. Both are read.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import duckdb

log = logging.getLogger(__name__)

# symbol -> raw yfinance news items
NewsFetcher = Callable[[str], list[dict[str, Any]]]


@dataclass(frozen=True)
class Headline:
    news_id: str
    symbol: str
    published_at: dt.datetime  # naive UTC
    source: str
    headline: str
    summary: str | None
    url: str | None


def yfinance_news(symbol: str, count: int = 20) -> list[dict[str, Any]]:
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    if hasattr(ticker, "get_news"):
        return ticker.get_news(count=count) or []
    return ticker.news or []


def _parse_time(value: Any) -> dt.datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, dt.UTC).replace(tzinfo=None)
    try:
        ts = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone(dt.UTC).replace(tzinfo=None)
    return ts


def news_id(source: str, url: str | None, headline: str, symbol: str) -> str:
    key = f"{source}|{url or headline}|{symbol}"
    return hashlib.sha256(key.encode()).hexdigest()


def parse_item(symbol: str, item: dict[str, Any]) -> Headline | None:
    content = item.get("content")
    if isinstance(content, dict):  # current yfinance shape
        title = content.get("title")
        summary = content.get("summary") or content.get("description")
        published = _parse_time(content.get("pubDate") or content.get("displayTime"))
        source = (content.get("provider") or {}).get("displayName") or "Yahoo Finance"
        url = (content.get("canonicalUrl") or {}).get("url") or (
            content.get("clickThroughUrl") or {}
        ).get("url")
    else:  # older flat shape
        title = item.get("title")
        summary = item.get("summary")
        published = _parse_time(item.get("providerPublishTime"))
        source = item.get("publisher") or "Yahoo Finance"
        url = item.get("link")
    if not title or published is None:
        return None
    title = " ".join(str(title).split())
    return Headline(
        news_id=news_id(source, url, title, symbol),
        symbol=symbol,
        published_at=published,
        source=source,
        headline=title,
        summary=" ".join(str(summary).split()) if summary else None,
        url=url,
    )


def insert_headlines(con: duckdb.DuckDBPyConnection, headlines: Iterable[Headline]) -> int:
    rows = [
        (h.news_id, h.symbol, h.published_at, h.source, h.headline, h.summary, h.url)
        for h in headlines
    ]
    if not rows:
        return 0
    before = con.execute("SELECT count(*) FROM news").fetchone()[0]
    con.executemany(
        "INSERT INTO news (news_id, symbol, published_at, source, headline, summary, url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (news_id) DO NOTHING",
        rows,
    )
    return con.execute("SELECT count(*) FROM news").fetchone()[0] - before


def collect_news(
    con: duckdb.DuckDBPyConnection,
    symbols: Iterable[str],
    fetch: NewsFetcher = yfinance_news,
) -> dict[str, int]:
    """New headlines stored per symbol; -1 marks a symbol whose fetch failed."""
    results: dict[str, int] = {}
    for symbol in symbols:
        try:
            items = fetch(symbol)
        except Exception as exc:  # unofficial API: keep going, report per symbol
            log.warning("News for %s failed: %s", symbol, exc)
            results[symbol] = -1
            continue
        parsed = [h for h in (parse_item(symbol, i) for i in items) if h]
        results[symbol] = insert_headlines(con, parsed)
    return results


def recent_headlines(
    con: duckdb.DuckDBPyConnection, symbols: list[str], since: dt.datetime, per_symbol: int
) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        WITH unique_headlines AS (  -- the same story is often listed twice (two URLs)
            SELECT symbol, published_at, source, headline, url FROM news
            WHERE symbol IN (SELECT unnest(?)) AND published_at >= ?
            QUALIFY row_number() OVER (
                PARTITION BY symbol, lower(trim(headline)) ORDER BY published_at DESC
            ) = 1
        )
        SELECT * FROM unique_headlines
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY published_at DESC) <= ?
        ORDER BY symbol, published_at DESC
        """,
        [symbols, since, per_symbol],
    ).fetchall()
    keys = ("symbol", "published_at", "source", "headline", "url")
    return [dict(zip(keys, r, strict=True)) for r in rows]
