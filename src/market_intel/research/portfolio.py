"""Luka's positions: ``config/portfolio.yaml`` (gitignored, never committed).

Positions are small dollar amounts, so the file holds the amount invested and the buy
date; shares are derived from the close on (or right after) the buy date. Performance
is price-only (dividends not included) and compared with the benchmark over the same
dates.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import yaml


@dataclass(frozen=True)
class Position:
    symbol: str
    invested_usd: float
    buy_date: dt.date
    source: str = "bought"  # bought | gift
    thesis: str | None = None


def load_portfolio(path: Path) -> list[Position]:
    """Positions from the YAML file; an empty list when the file doesn't exist."""
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: list[Position] = []
    for e in data.get("positions") or []:
        buy = e["buy_date"]
        out.append(
            Position(
                symbol=str(e["symbol"]).upper(),
                invested_usd=float(e["invested_usd"]),
                buy_date=buy if isinstance(buy, dt.date) else dt.date.fromisoformat(str(buy)),
                source=str(e.get("source") or "bought"),
                thesis=(str(e["thesis"]).strip() or None) if e.get("thesis") else None,
            )
        )
    symbols = [p.symbol for p in out]
    if len(symbols) != len(set(symbols)):
        raise ValueError(f"Duplicate symbols in portfolio: {symbols}")
    return out


def _close_on_or_after(con, symbol: str, day: dt.date) -> tuple[dt.date, float] | None:
    return con.execute(
        "SELECT trade_date, close FROM prices_daily WHERE symbol = ? AND trade_date >= ? "
        "AND close IS NOT NULL ORDER BY trade_date LIMIT 1",
        [symbol, day],
    ).fetchone()


def _close_on_or_before(con, symbol: str, day: dt.date) -> tuple[dt.date, float] | None:
    return con.execute(
        "SELECT trade_date, close FROM prices_daily WHERE symbol = ? AND trade_date <= ? "
        "AND close IS NOT NULL ORDER BY trade_date DESC LIMIT 1",
        [symbol, day],
    ).fetchone()


def _r(v: float | None, n: int = 2) -> float | None:
    return round(v, n) if v is not None else None


def value_position(
    con: duckdb.DuckDBPyConnection, p: Position, as_of: dt.date, benchmark: str = "SPY"
) -> dict[str, Any]:
    """Value today and return since the buy date, vs the benchmark over the same dates."""
    out: dict[str, Any] = {
        "symbol": p.symbol,
        "invested_usd": p.invested_usd,
        "buy_date": str(p.buy_date),
        "source": p.source,
        "thesis": p.thesis,
    }
    buy = _close_on_or_after(con, p.symbol, p.buy_date)
    last = _close_on_or_before(con, p.symbol, as_of)
    if not buy or not last or buy[0] > last[0]:
        out["note"] = "no price history covering the buy date; run collect-prices"
        return out
    shares = p.invested_usd / buy[1]
    ret = 100 * (last[1] / buy[1] - 1)
    out.update(
        buy_close=_r(buy[1]),
        buy_close_date=str(buy[0]),
        shares=round(shares, 6),
        last_close=_r(last[1]),
        last_close_date=str(last[0]),
        value_usd=_r(shares * last[1]),
        gain_usd=_r(shares * last[1] - p.invested_usd),
        return_pct=_r(ret, 1),
        days_held=(last[0] - buy[0]).days,
    )
    b0 = _close_on_or_after(con, benchmark, buy[0])
    b1 = _close_on_or_before(con, benchmark, last[0])
    if b0 and b1 and b0[0] <= b1[0]:
        bret = 100 * (b1[1] / b0[1] - 1)
        out.update(
            benchmark=benchmark,
            benchmark_return_pct=_r(bret, 1),
            vs_benchmark_pts=_r(ret - bret, 1),
        )
    return out


def portfolio_summary(
    con: duckdb.DuckDBPyConnection,
    positions: list[Position],
    as_of: dt.date,
    benchmark: str = "SPY",
) -> dict[str, Any]:
    rows = [value_position(con, p, as_of, benchmark) for p in positions]
    valued = [r for r in rows if "value_usd" in r]
    total_value = sum(r["value_usd"] for r in valued)
    total_invested = sum(r["invested_usd"] for r in valued)
    for r in valued:
        r["share_of_portfolio_pct"] = (
            _r(100 * r["value_usd"] / total_value, 1) if total_value else None
        )
    return {
        "positions": rows,
        "total_invested_usd": _r(total_invested),
        "total_value_usd": _r(total_value),
        "total_return_pct": _r(100 * (total_value / total_invested - 1), 1)
        if total_invested
        else None,
        "note": "Price-only returns (dividends not included). Sizing decisions are Luka's.",
    }
