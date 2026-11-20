"""Replay the screen on past dates using only data published by each date.

For each rebalance date D (quarterly by default, a few weeks after quarter ends when
most 10-Qs are in), the screen runs with fundamentals filed on or before D and the
last close on or before D. The top N picks are then held for each horizon, and their
average return is compared with the equal-weighted average of *every company that
passed the filters on D* (and with IWM, the Russell 2000 ETF, when its prices are
loaded). Each pillar is also tested on its own, to see which part of the score did
the work.

Known biases (stated in every report):

* **Survivorship.** The universe comes from today's SEC ticker list and today's Yahoo
  prices, so companies that went bankrupt, were delisted or were acquired are missing.
  Comparing with the same survivor-only universe cancels part of this, not all of it.
* **Split-adjusted prices.** Yahoo's historical closes are adjusted for later splits
  while share counts are as reported, so market cap can be off for companies that
  split after D. Returns use the adjusted close, which is correct.
* Small sample: a 10-year quarterly test is ~40 periods with overlapping holding
  windows; treat differences of a few percent as noise.
"""

from __future__ import annotations

import datetime as dt
import statistics
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import duckdb

from market_intel.screener.screen import PILLARS, run_screen, screen_settings

BT_DEFAULTS: dict[str, Any] = {
    "start": "2015-01-01",
    "rebalance_months": [2, 5, 8, 11],
    "rebalance_day": 20,
    "horizons_days": [91, 182, 365],
    "benchmark": "IWM",
}


def bt_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    cfg = dict(BT_DEFAULTS)
    cfg.update(((settings or {}).get("screener") or {}).get("backtest") or {})
    return cfg


def rebalance_dates(start: dt.date, end: dt.date, months: list[int], day: int) -> list[dt.date]:
    out = []
    for year in range(start.year, end.year + 1):
        for month in sorted(months):
            d = dt.date(year, month, day)
            if start <= d <= end:
                out.append(d)
    return out


class PriceBook:
    """Adjusted closes per symbol, for forward returns."""

    def __init__(self, con: duckdb.DuckDBPyConnection, symbols: set[str] | None = None):
        rows = con.execute(
            "SELECT symbol, trade_date, coalesce(adj_close, close) FROM prices_daily "
            "WHERE coalesce(adj_close, close) IS NOT NULL ORDER BY symbol, trade_date"
        ).fetchall()
        self.dates: dict[str, list[dt.date]] = defaultdict(list)
        self.values: dict[str, list[float]] = defaultdict(list)
        for sym, day, px in rows:
            if symbols is not None and sym not in symbols:
                continue
            self.dates[sym].append(day)
            self.values[sym].append(px)

    def on_or_before(self, sym: str, day: dt.date) -> tuple[dt.date, float] | None:
        ds = self.dates.get(sym)
        if not ds:
            return None
        i = bisect_right(ds, day) - 1
        return (ds[i], self.values[sym][i]) if i >= 0 else None

    def on_or_after(self, sym: str, day: dt.date) -> tuple[dt.date, float] | None:
        ds = self.dates.get(sym)
        if not ds:
            return None
        i = bisect_left(ds, day)
        return (ds[i], self.values[sym][i]) if i < len(ds) else None

    def last_date(self) -> dt.date | None:
        return max((ds[-1] for ds in self.dates.values() if ds), default=None)

    def forward_return(self, sym: str, start: dt.date, days: int) -> float | None:
        """Return from the last close on/before ``start`` to the first close on/after
        ``start + days``. If the stock stopped trading before then, its last close is
        used (a rough stand-in for delisting/takeover value)."""
        entry = self.on_or_before(sym, start)
        if entry is None or (start - entry[0]).days > 7:
            return None
        target = start + dt.timedelta(days=days)
        exit_ = self.on_or_after(sym, target)
        if exit_ is None:
            last = self.on_or_before(sym, target)
            if last is None or last[0] <= entry[0]:
                return None
            exit_ = last
        return exit_[1] / entry[1] - 1


@dataclass
class PeriodResult:
    as_of: dt.date
    n_eligible: int
    picks: dict[str, list[str]]  # strategy -> tickers
    returns: dict[int, dict[str, float | None]] = field(default_factory=dict)
    # horizon -> {strategy name or "universe"/"benchmark": avg return}


def _avg(xs: list[float | None]) -> float | None:
    vals = [x for x in xs if x is not None]
    return statistics.fmean(vals) if vals else None


def run_backtest(
    con: duckdb.DuckDBPyConnection,
    settings: dict[str, Any] | None,
    start: dt.date | None = None,
    end: dt.date | None = None,
    top_n: int | None = None,
) -> tuple[list[PeriodResult], dict[str, Any]]:
    cfg = screen_settings(settings)
    bt = bt_settings(settings)
    top_n = int(top_n or cfg["top_n"])
    book = PriceBook(con)
    last_px = book.last_date()
    if last_px is None:
        raise ValueError("No prices loaded; run `market-intel screen-prices --start ...` first")
    start = start or dt.date.fromisoformat(str(bt["start"]))
    end = end or last_px
    horizons = [int(h) for h in bt["horizons_days"]]
    bench = bt.get("benchmark")

    periods: list[PeriodResult] = []
    for d in rebalance_dates(start, end, list(bt["rebalance_months"]), int(bt["rebalance_day"])):
        res = run_screen(con, d, settings)
        if not res.candidates:
            continue
        strategies = {"score": [c.ticker for c in res.candidates[:top_n]]}
        for pillar in PILLARS:
            ranked = sorted(res.candidates, key=lambda c, p=pillar: c.pillars[p], reverse=True)
            strategies[pillar] = [c.ticker for c in ranked[:top_n]]
        pr = PeriodResult(d, len(res.candidates), strategies)
        for h in horizons:
            if d + dt.timedelta(days=h) > last_px:
                continue  # not enough time has passed to measure this horizon
            row: dict[str, float | None] = {
                name: _avg([book.forward_return(t, d, h) for t in tickers])
                for name, tickers in strategies.items()
            }
            row["universe"] = _avg([book.forward_return(c.ticker, d, h) for c in res.candidates])
            if bench:
                row["benchmark"] = book.forward_return(bench, d, h)
            pr.returns[h] = row
        periods.append(pr)
    return periods, summarize(periods, horizons)


def summarize(periods: list[PeriodResult], horizons: list[int]) -> dict[str, Any]:
    """Per horizon and strategy: average return, average excess over the universe,
    share of periods that beat the universe, number of periods."""
    out: dict[str, Any] = {}
    for h in horizons:
        rows = [p.returns[h] for p in periods if h in p.returns]
        if not rows:
            continue
        names = [k for k in rows[0] if k not in ("universe", "benchmark")]
        stats: dict[str, Any] = {}
        for name in names + ["universe", "benchmark"]:
            vals = [r.get(name) for r in rows]
            pairs = [
                (r[name], r["universe"])
                for r in rows
                if r.get(name) is not None and r.get("universe") is not None
            ]
            excess = [a - b for a, b in pairs]
            stats[name] = {
                "avg_return": _avg(vals),
                "avg_excess_vs_universe": _avg(excess) if name != "universe" else 0.0,
                "beat_rate": (sum(e > 0 for e in excess) / len(excess)) if excess else None,
                "periods": len([v for v in vals if v is not None]),
            }
        out[h] = stats
    return out


def _p(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:+.1f}%"


def render_report(
    periods: list[PeriodResult], summary: dict[str, Any], top_n: int, settings: dict | None
) -> str:
    cfg = screen_settings(settings)
    lines = [
        "# Screener backtest (point-in-time)",
        "",
        f"Generated {dt.date.today()}. {len(periods)} rebalance dates"
        + (f", {periods[0].as_of} to {periods[-1].as_of}." if periods else "."),
        f"Top {top_n} picks per date, equal weight. Filters: market cap "
        f"${cfg['min_market_cap'] / 1e6:,.0f}M to ${cfg['max_market_cap'] / 1e9:,.0f}B, "
        "profitable, recent filings, no financials/REITs.",
        "",
        "**Read this first.** The universe is today's listed companies, so firms that went "
        "bust or were taken over since are missing (survivorship bias): absolute returns are "
        "flattering. The fairer test is the *excess over the universe* column, because the "
        "universe has the same bias. ~40 overlapping periods is a small sample; a few "
        "percent either way is noise. Past results don't predict future ones.",
        "",
    ]
    for h, stats in summary.items():
        lines += [
            f"## Holding {h} days",
            "",
            "| Strategy | Avg return | Avg excess vs universe | Beat universe | Periods |",
            "|---|---|---|---|---|",
        ]
        for name, s in stats.items():
            if s["periods"] == 0:
                continue
            label = {
                "score": "Full score (what the digest uses)",
                "universe": "Universe (all that passed filters)",
                "benchmark": "IWM (Russell 2000 ETF)",
            }.get(name, f"{name.capitalize()} pillar only")
            beat = "n/a" if s["beat_rate"] is None else f"{s['beat_rate'] * 100:.0f}%"
            excess = "" if name == "universe" else _p(s["avg_excess_vs_universe"])
            lines.append(
                f"| {label} | {_p(s['avg_return'])} | {excess} | {beat} | {s['periods']} |"
            )
        lines.append("")
    lines += ["## Per date", "", "| Date | Passed filters | Top picks |", "|---|---|---|"]
    for p in periods:
        lines.append(f"| {p.as_of} | {p.n_eligible} | {', '.join(p.picks['score'])} |")
    return "\n".join(lines) + "\n"
