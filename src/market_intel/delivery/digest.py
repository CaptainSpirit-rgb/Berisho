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

    rows = _ticker_rows(con, symbols, vol_lookback)
    regime = _regime(con, bench.get("symbol", "SPY"), int(bench.get("regime_ma_days", 200)))

    flags: list[tuple[str, str]] = []
    for r in rows:
        if r.gap_pct is not None and abs(r.gap_pct) >= gap_thr:
            flags.append((r.symbol, f"opened {r.gap_pct:+.1f}% vs prior close (gap)"))
        if r.volume_ratio is not None and r.volume_ratio >= vol_thr:
            flags.append((r.symbol, f"volume {r.volume_ratio:.1f}x its {vol_lookback}-day average"))

    filings = [
        dict(
            zip(
                ("symbol", "form_type", "accepted_at", "filed_date", "items", "url"), f, strict=True
            )
        )
        for f in con.execute(
            """
            SELECT symbol, form_type, accepted_at, filed_date, items, primary_doc_url
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
        windows={"filings": filings_days, "insiders": insider_days},
    )


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


def render_text(d: Digest) -> str:
    tz = d.display_tz
    local_now = d.generated_at.astimezone(tz)
    out = [
        f"MARKET INTEL DIGEST  {local_now:%a %d %b %Y, %H:%M} {tz.key}",
        "=" * 64,
    ]

    r = d.regime
    if r and "regime" in r:
        out.append(
            f"Regime: {r['regime'].upper()}  ({r['benchmark']} {r['close']:.2f} vs "
            f"{r['ma_days']}-day MA {r['ma']:.2f}, as of {r['as_of']})"
        )
    elif r:
        out.append(f"Regime: unknown ({r['benchmark']} has {r['history']} of {r['needed']} days)")

    out += ["", f"FLAGS ({len(d.flags)})"]
    out += [f"  * {sym:<6} {text}" for sym, text in d.flags] or ["  Nothing flagged today."]

    out += [
        "",
        "WATCHLIST",
        f"  {'SYMBOL':<7}{'CLOSE':>10}{'1D':>8}{'5D':>8}{'GAP':>8}{'VOL/AVG':>9}  LAST BAR",
    ]
    for t in d.rows:
        if t.close is None:
            out.append(f"  {t.symbol:<7}{'no price data yet':>43}")
            continue
        vol = f"{t.volume_ratio:.1f}x" if t.volume_ratio is not None else "-"
        out.append(
            f"  {t.symbol:<7}{t.close:>10.2f}{_pct(t.change_1d_pct):>8}{_pct(t.change_5d_pct):>8}"
            f"{_pct(t.gap_pct):>8}{vol:>9}  {t.trade_date}"
        )

    out += ["", f"FILINGS (last {d.windows['filings']} days, excluding Form 4)"]
    if not d.filings:
        out.append("  None.")
    for f in d.filings:
        when = _local(f["accepted_at"], tz) if f["accepted_at"] else str(f["filed_date"])
        desc = describe_items(f["items"]) if f["form_type"].startswith("8-K") else ""
        out.append(f"  {when:<13} {f['symbol']:<6} {f['form_type']:<8} {desc}".rstrip())
        if f["url"]:
            out.append(f"  {'':<13} {f['url']}")

    out += ["", f"INSIDER OPEN-MARKET TRADES (last {d.windows['insiders']} days)"]
    if not d.insiders:
        out.append("  None.")
    for i in d.insiders:
        out.append(
            f"  {i['symbol']:<6} buys: {i['buyers']} insider(s), {_money(i['buy_value'])}   "
            f"sells: {i['sellers']} insider(s), {_money(i['sell_value'])}"
        )

    out += ["", "Decision support only, not financial advice."]
    return "\n".join(out)
