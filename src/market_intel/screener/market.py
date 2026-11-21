"""Market overview: major indexes and sector funds, for the top of the weekly message.

Index levels themselves aren't freely licensed, so the ETFs that track them stand in
(SPY for the S&P 500, QQQ for the Nasdaq 100, ...). Returns use adjusted closes from
``prices_daily``; the 200-day moving average is the thesis's bull/bear rule.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import duckdb

DEFAULT_INDEXES: list[tuple[str, str]] = [
    ("SPY", "S&P 500"),
    ("QQQ", "Nasdaq 100"),
    ("DIA", "Dow Jones"),
    ("IWM", "Russell 2000"),
]
DEFAULT_SECTORS: list[tuple[str, str]] = [
    ("XLK", "Technology"),
    ("XLC", "Communication"),
    ("XLY", "Consumer Discretionary"),
    ("XLP", "Consumer Staples"),
    ("XLV", "Health Care"),
    ("XLF", "Financials"),
    ("XLI", "Industrials"),
    ("XLE", "Energy"),
    ("XLB", "Materials"),
    ("XLU", "Utilities"),
    ("XLRE", "Real Estate"),
]
MA_DAYS = 200


def _pairs(raw, default):
    if not raw:
        return list(default)
    out = []
    for item in raw:
        if isinstance(item, dict):
            out.append((str(item["symbol"]).upper(), str(item.get("name", item["symbol"]))))
        else:
            out.append((str(item).upper(), str(item).upper()))
    return out


def market_settings(settings: dict[str, Any] | None) -> tuple[list, list]:
    cfg = ((settings or {}).get("screener") or {}).get("market") or {}
    return (
        _pairs(cfg.get("indexes"), DEFAULT_INDEXES),
        _pairs(cfg.get("sectors"), DEFAULT_SECTORS),
    )


def market_symbols(settings: dict[str, Any] | None) -> list[str]:
    indexes, sectors = market_settings(settings)
    return [s for s, _ in indexes + sectors]


@dataclass
class FundRow:
    symbol: str
    name: str
    as_of: dt.date | None = None
    close: float | None = None
    ret_1w: float | None = None
    ret_1m: float | None = None
    ret_3m: float | None = None
    ret_ytd: float | None = None
    above_ma: bool | None = None  # None: fewer than 200 days of history


def _fund_row(con: duckdb.DuckDBPyConnection, symbol: str, name: str, as_of: dt.date) -> FundRow:
    rows = con.execute(
        """
        SELECT trade_date, close, coalesce(adj_close, close) FROM prices_daily
        WHERE symbol = ? AND trade_date <= ? AND trade_date >= ? AND close IS NOT NULL
        ORDER BY trade_date
        """,
        [symbol, as_of, as_of - dt.timedelta(days=420)],
    ).fetchall()
    row = FundRow(symbol, name)
    if not rows or (as_of - rows[-1][0]).days > 7:
        return row
    dates = [r[0] for r in rows]
    adj = [r[2] for r in rows]
    row.as_of, row.close = dates[-1], rows[-1][1]

    def back(n: int) -> float | None:
        return adj[-1] / adj[-1 - n] - 1 if len(adj) > n and adj[-1 - n] else None

    row.ret_1w, row.ret_1m, row.ret_3m = back(5), back(21), back(63)
    prior_year = [a for d, a in zip(dates, adj, strict=True) if d.year < dates[-1].year]
    if prior_year and prior_year[-1]:
        row.ret_ytd = adj[-1] / prior_year[-1] - 1
    if len(adj) >= MA_DAYS:
        row.above_ma = adj[-1] > sum(adj[-MA_DAYS:]) / MA_DAYS
    return row


def overview(
    con: duckdb.DuckDBPyConnection, as_of: dt.date, settings: dict[str, Any] | None = None
) -> tuple[list[FundRow], list[FundRow]]:
    indexes, sectors = market_settings(settings)
    return (
        [_fund_row(con, s, n, as_of) for s, n in indexes],
        [_fund_row(con, s, n, as_of) for s, n in sectors],
    )


def _p(v: float | None) -> str:
    return "   n/a" if v is None else f"{v * 100:+5.1f}%"


def _trend(above: bool | None) -> str:
    return {True: "above 200d", False: "BELOW 200d", None: "short history"}[above]


def render_overview(indexes: list[FundRow], sectors: list[FundRow]) -> str:
    lines = ["MARKET (weekly)", f"{'':<22}{'1w':>7}{'1m':>7}{'3m':>7}{'YTD':>7}  trend"]
    for r in indexes:
        lines.append(_line(r))
    missing = [r.symbol for r in indexes + sectors if r.close is None]
    with_data = [r for r in sectors if r.close is not None and r.ret_1m is not None]
    if with_data:
        lines += ["", "Sectors, best to worst over 1 month:"]
        for r in sorted(with_data, key=lambda r: r.ret_1m or 0, reverse=True):
            lines.append(_line(r))
    if missing:
        lines.append(f"(no recent prices for {', '.join(missing)})")
    return "\n".join(lines)


def _line(r: FundRow) -> str:
    label = f"{r.name} ({r.symbol})"
    if r.close is None:
        return f"{label:<22}  no data"
    return (
        f"{label[:22]:<22}{_p(r.ret_1w):>7}{_p(r.ret_1m):>7}{_p(r.ret_3m):>7}"
        f"{_p(r.ret_ytd):>7}  {_trend(r.above_ma)}"
    )
