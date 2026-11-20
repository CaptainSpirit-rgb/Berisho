"""Daily digest built from the raw tables.

Phase 1 dry run: the digest reads ``prices_daily``, ``filings`` and
``insider_transactions`` directly and prints plain text. Simple flags (price gap,
volume spike, fresh 8-K, insider buying) are computed inline with thresholds from
``config/settings.yaml``; the proper detectors that write to ``signals`` come next and
will replace them. Times are stored in UTC and shown in Tbilisi time.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

import duckdb

from market_intel.config import Security

# Plain-language names for common 8-K items (Form 8-K general instructions).
EIGHT_K_ITEMS = {
    "1.01": "material agreement",
    "1.02": "agreement terminated",
    "1.03": "bankruptcy",
    "2.01": "acquisition/disposition completed",
    "2.02": "results of operations",
    "2.03": "new financial obligation",
    "2.05": "exit/restructuring costs",
    "2.06": "material impairment",
    "3.01": "listing/delisting notice",
    "3.02": "unregistered equity sale",
    "4.01": "auditor change",
    "4.02": "prior financials unreliable",
    "5.01": "change in control",
    "5.02": "officer/director change",
    "5.03": "bylaws/charter amended",
    "5.07": "shareholder vote results",
    "7.01": "Reg FD disclosure",
    "8.01": "other events",
    "9.01": "exhibits",
}


@dataclass
class TickerRow:
    symbol: str
    trade_date: dt.date | None = None
    close: float | None = None
    change_1d_pct: float | None = None
    change_5d_pct: float | None = None
    gap_pct: float | None = None
    volume_ratio: float | None = None


@dataclass
class Digest:
    generated_at: dt.datetime  # aware, UTC
    display_tz: ZoneInfo
    regime: dict[str, Any] | None
    rows: list[TickerRow]
    flags: list[tuple[str, str]]  # (symbol, text)
    filings: list[dict[str, Any]]
    insiders: list[dict[str, Any]]
    windows: dict[str, int] = field(default_factory=dict)
    market: TickerRow | None = None  # the regime benchmark (SPY), for "vs the market"
    names: dict[str, str] = field(default_factory=dict)
    insider_trades: list[dict[str, Any]] = field(default_factory=list)  # largest first
    news: list[dict[str, Any]] = field(default_factory=list)  # newest first per symbol

    def headlines(self, symbol: str) -> list[dict[str, Any]]:
        return [n for n in self.news if n["symbol"] == symbol]

    sentiment: list[tuple[str, str]] = field(default_factory=list)  # (symbol, line); [] = no news

    @property
    def as_of(self) -> dt.date | None:
        dates = [r.trade_date for r in self.rows if r.trade_date]
        return max(dates) if dates else None


def describe_items(items: str | None) -> str:
    if not items:
        return ""
    parts = []
    for item in (i.strip() for i in items.split(",")):
        if not item or item == "9.01":  # exhibits only, not interesting on its own
            continue
        label = EIGHT_K_ITEMS.get(item)
        parts.append(f"{item} {label}" if label else item)
    return "; ".join(parts)


def _ticker_rows(
    con: duckdb.DuckDBPyConnection, symbols: list[str], lookback: int
) -> list[TickerRow]:
    sql = f"""
        WITH p AS (
            SELECT symbol, trade_date, open, close, volume,
                   lag(close, 1) OVER w AS prev_close,
                   lag(close, 5) OVER w AS close_5d,
                   avg(volume) OVER (PARTITION BY symbol ORDER BY trade_date
                       ROWS BETWEEN {lookback} PRECEDING AND 1 PRECEDING) AS avg_vol,
                   count(volume) OVER (PARTITION BY symbol ORDER BY trade_date
                       ROWS BETWEEN {lookback} PRECEDING AND 1 PRECEDING) AS n_vol,
                   row_number() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rn
            FROM prices_daily
            WHERE symbol IN (SELECT unnest(?))
            WINDOW w AS (PARTITION BY symbol ORDER BY trade_date)
        )
        SELECT symbol, trade_date, close,
               100 * (close / prev_close - 1),
               100 * (close / close_5d - 1),
               100 * (open / prev_close - 1),
               CASE WHEN n_vol = {lookback} AND avg_vol > 0 THEN volume / avg_vol END
        FROM p WHERE rn = 1
    """
    found = {r[0]: TickerRow(*r) for r in con.execute(sql, [symbols]).fetchall()}
    return [found.get(s, TickerRow(s)) for s in symbols]


def _regime(con: duckdb.DuckDBPyConnection, benchmark: str, ma_days: int) -> dict[str, Any] | None:
    rows = con.execute(
        """
        SELECT trade_date, coalesce(adj_close, close) FROM prices_daily
        WHERE symbol = ? ORDER BY trade_date DESC LIMIT ?
        """,
        [benchmark, ma_days],
    ).fetchall()
    if len(rows) < ma_days:
        return {"benchmark": benchmark, "history": len(rows), "needed": ma_days}
    close = rows[0][1]
    ma = sum(r[1] for r in rows) / ma_days
    return {
        "benchmark": benchmark,
        "as_of": rows[0][0],
        "close": close,
        "ma": ma,
        "ma_days": ma_days,
        "regime": "bull" if close > ma else "bear",
    }


def build_digest(
    con: duckdb.DuckDBPyConnection,
    securities: list[Security],
    settings: dict[str, Any],
    now: dt.datetime | None = None,
) -> Digest:
    now = now or dt.datetime.now(dt.UTC)
    now_naive = now.astimezone(dt.UTC).replace(tzinfo=None)  # DB stores naive UTC
    tz = ZoneInfo(settings.get("timezone", {}).get("display", "Asia/Tbilisi"))
    sig = settings.get("signals", {})
    dig = settings.get("digest", {})
    bench = settings.get("benchmark", {})
    symbols = [s.symbol for s in securities]

    gap_thr = float(sig.get("price_gap_pct", 5.0))
    vol_thr = float(sig.get("volume_spike_multiple", 3.0))
    vol_lookback = int(sig.get("volume_lookback_days", 20))
    cluster = sig.get("insider_cluster", {})
    min_buyers = int(cluster.get("min_buyers", 3))
    cluster_days = int(cluster.get("window_days", 10))
    filings_days = int(dig.get("filings_days", 7))
    new_8k_days = int(dig.get("new_8k_days", 3))
    insider_days = int(dig.get("insider_days", 30))

    bench_symbol = bench.get("symbol", "SPY")
    all_rows = _ticker_rows(con, symbols + [bench_symbol], vol_lookback)
    rows, market = all_rows[:-1], all_rows[-1]
    if bench_symbol in symbols:
        market = rows[symbols.index(bench_symbol)]
    regime = _regime(con, bench_symbol, int(bench.get("regime_ma_days", 200)))

    flags: list[tuple[str, str]] = []
    for r in rows:
        if r.gap_pct is not None and abs(r.gap_pct) >= gap_thr:
            flags.append((r.symbol, f"opened {r.gap_pct:+.1f}% vs prior close (gap)"))
        if r.volume_ratio is not None and r.volume_ratio >= vol_thr:
            flags.append((r.symbol, f"volume {r.volume_ratio:.1f}x its {vol_lookback}-day average"))

    filings = [
        dict(
            zip(
                (
                    "accession_no",
                    "symbol",
                    "form_type",
                    "accepted_at",
                    "filed_date",
                    "items",
                    "url",
                ),
                f,
                strict=True,
            )
        )
        for f in con.execute(
            """
            SELECT accession_no, symbol, form_type, accepted_at, filed_date, items,
                   primary_doc_url
            FROM filings
            WHERE symbol IN (SELECT unnest(?))
              AND form_type NOT IN ('4', '4/A')
              AND subject_cik = cik  -- skip filings the company made about other issuers
              AND coalesce(accepted_at, filed_date::TIMESTAMP) >= ?
            ORDER BY coalesce(accepted_at, filed_date::TIMESTAMP) DESC
            """,
            [symbols, now_naive - dt.timedelta(days=filings_days)],
        ).fetchall()
    ]
    for f in filings:
        when = f["accepted_at"] or dt.datetime.combine(f["filed_date"], dt.time())
        if f["form_type"].startswith("8-K") and when >= now_naive - dt.timedelta(days=new_8k_days):
            desc = describe_items(f["items"])
            flags.append((f["symbol"], f"new {f['form_type']}" + (f" ({desc})" if desc else "")))
        elif "13D" in f["form_type"]:
            flags.append((f["symbol"], f"new {f['form_type']} (activist-size stake)"))

    insiders = [
        dict(
            zip(
                ("symbol", "buyers", "buy_value", "sellers", "sell_value", "cluster_buyers"),
                r,
                strict=True,
            )
        )
        for r in con.execute(
            """
            SELECT symbol,
                   count(DISTINCT insider_name) FILTER (WHERE transaction_code = 'P'),
                   sum(shares * price) FILTER (WHERE transaction_code = 'P'),
                   count(DISTINCT insider_name) FILTER (WHERE transaction_code = 'S'),
                   sum(shares * price) FILTER (WHERE transaction_code = 'S'),
                   count(DISTINCT insider_name) FILTER (
                       WHERE transaction_code = 'P' AND transaction_date >= ?)
            FROM insider_transactions
            WHERE symbol IN (SELECT unnest(?)) AND transaction_date >= ?
              AND transaction_code IN ('P', 'S')
            GROUP BY symbol ORDER BY symbol
            """,
            [
                (now_naive - dt.timedelta(days=cluster_days)).date(),
                symbols,
                (now_naive - dt.timedelta(days=insider_days)).date(),
            ],
        ).fetchall()
    ]
    for ins in insiders:
        if ins["cluster_buyers"] >= min_buyers:
            flags.append(
                (
                    ins["symbol"],
                    f"insider cluster buy: {ins['cluster_buyers']} insiders bought "
                    f"in the last {cluster_days} days",
                )
            )

    trade_keys = ("symbol", "insider", "title", "code", "date", "shares", "price", "value")
    insider_trades = [
        dict(zip(trade_keys, r, strict=True))
        for r in con.execute(
            """
            SELECT symbol, insider_name, insider_title, transaction_code, transaction_date,
                   shares, price, shares * price AS value
            FROM insider_transactions
            WHERE symbol IN (SELECT unnest(?)) AND transaction_date >= ?
              AND transaction_code IN ('P', 'S')
            QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY shares * price DESC NULLS LAST)
                    <= 5
            ORDER BY symbol, value DESC NULLS LAST
            """,
            [symbols, (now_naive - dt.timedelta(days=insider_days)).date()],
        ).fetchall()
    ]

    from market_intel.collectors.news import recent_headlines

    news = recent_headlines(
        con,
        symbols,
        now_naive - dt.timedelta(days=int(dig.get("news_days", 3))),
        int(dig.get("news_per_symbol", 3)),
    )
    sentiment_lines = _sentiment(con, symbols, settings, now, regime, flags)

    order = {s: i for i, s in enumerate(symbols)}
    flags.sort(key=lambda f: order.get(f[0], len(order)))
    return Digest(
        generated_at=now.astimezone(dt.UTC),
        display_tz=tz,
        regime=regime,
        rows=rows,
        flags=flags,
        filings=filings,
        insiders=insiders,
        windows={
            "filings": filings_days,
            "insiders": insider_days,
            "news": int(dig.get("news_days", 3)),
        },
        market=market if market.close is not None else None,
        names={sec.symbol: sec.name for sec in securities},
        insider_trades=insider_trades,
        news=news,
        sentiment=sentiment_lines,
    )


def _sentiment(
    con: duckdb.DuckDBPyConnection,
    symbols: list[str],
    settings: dict[str, Any],
    now: dt.datetime,
    regime: dict[str, Any] | None,
    flags: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """News-sentiment lines per ticker, and flags for any shift or volume spike. Read-only
    (signals are written by `score-sentiment`); empty while no news has been collected."""
    from market_intel.signals import sentiment

    cfg = sentiment.sentiment_settings(settings)
    if not cfg["enabled"] or not con.execute("SELECT count(*) FROM news").fetchone()[0]:
        return []
    results = sentiment.detect(
        con, symbols, settings, now=now, regime=(regime or {}).get("regime"), write=False
    )
    for r in results:
        flags += [(r.symbol, sentiment.flag_text(s)) for s in r.signals]
    return [(r.symbol, sentiment.describe(r, cfg)) for r in results]


def _pct(v: float | None) -> str:
    return f"{v:+.1f}%" if v is not None else "-"


def _money(v: float | None) -> str:
    if not v:
        return "$0"
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v / div:.1f}{suffix}"
    return f"${v:.0f}"


def _local(ts: dt.datetime, tz: ZoneInfo) -> str:
    return ts.replace(tzinfo=dt.UTC).astimezone(tz).strftime("%d %b %H:%M")
