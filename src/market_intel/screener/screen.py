"""The weekly screen: filter, score and explain (S&P 500 members by default).

Every number is point-in-time for ``as_of`` (fundamentals filed by then, the last
close on or before it), so the same function drives the live screen and the backtest.

Scoring is deliberately simple and transparent. Each metric becomes a percentile
(0 = worst, 1 = best) among the companies that passed the filters; valuation metrics
are ranked within the company's GICS sector (or 2-digit SIC group if no sector is
known) when it has enough peers.
Percentiles are averaged into four pillars, and the pillars into one score with weights
from ``config/settings.yaml``. A high score means "looks good on paper right now", not
"will go up".
"""

from __future__ import annotations

import datetime as dt
import json
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import duckdb

from market_intel.screener.metrics import company_metrics

DEFAULTS: dict[str, Any] = {
    "min_market_cap": None,  # no size filter by default (the S&P 500 is already large caps)
    "max_market_cap": None,
    "min_revenue_ttm": 25e6,
    "require_positive_operating_income": True,
    "max_staleness_days": 200,
    "max_price_age_days": 7,
    "exclude_sectors": ["Financials", "Real Estate"],  # their accounts don't fit these metrics
    "exclude_sic_ranges": [[6000, 6799]],  # same idea when only SIC is known
    "min_industry_peers": 8,
    "missing_percentile": 0.4,
    "top_n": 10,
    "weights": {"growth": 1.0, "quality": 1.0, "balance": 0.75, "value": 1.0},
}

# pillar -> [(metric, higher_is_better, rank_within_industry)]
PILLARS: dict[str, list[tuple[str, bool, bool]]] = {
    "growth": [("revenue_growth", True, False), ("growth_acceleration", True, False)],
    "quality": [
        ("operating_margin", True, True),
        ("operating_margin_change", True, False),
        ("fcf_margin", True, True),
    ],
    "balance": [("debt_burden", False, False)],
    "value": [
        ("earnings_yield", True, True),
        ("fcf_yield", True, True),
        ("ev_to_sales", False, True),
    ],
}


def screen_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg.update((settings or {}).get("screener") or {})
    cfg["weights"] = {**DEFAULTS["weights"], **(cfg.get("weights") or {})}
    return cfg


@dataclass
class Candidate:
    cik: int
    ticker: str
    name: str
    sic: int | None
    sic_description: str | None
    metrics: dict[str, Any]
    sector: str | None = None
    percentiles: dict[str, float] = field(default_factory=dict)
    pillars: dict[str, float] = field(default_factory=dict)
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def industry(self) -> str | int | None:
        """Peer group for relative valuation: GICS sector, else 2-digit SIC."""
        if self.sector:
            return self.sector
        return self.sic // 100 if self.sic else None

    @property
    def industry_label(self) -> str | None:
        return self.sector or self.sic_description


@dataclass
class ScreenResult:
    as_of: dt.date
    candidates: list[Candidate]  # every company that passed the filters, best first
    excluded: dict[str, int]  # reason -> count
    universe_size: int
    industry_medians: dict[tuple[Any, str], float] = field(default_factory=dict)


def latest_prices(
    con: duckdb.DuckDBPyConnection, as_of: dt.date, max_age_days: int
) -> dict[str, tuple[float, dt.date]]:
    rows = con.execute(
        """
        SELECT symbol, close, trade_date FROM (
            SELECT symbol, close, trade_date, row_number() OVER (
                PARTITION BY symbol ORDER BY trade_date DESC) AS rn
            FROM prices_daily
            WHERE trade_date <= ? AND trade_date >= ? AND close IS NOT NULL)
        WHERE rn = 1
        """,
        [as_of, as_of - dt.timedelta(days=max_age_days)],
    ).fetchall()
    return {sym: (close, day) for sym, close, day in rows}


def percentile_ranks(values: dict[Any, float | None], higher_is_better: bool) -> dict[Any, float]:
    """Average-rank percentiles in [0, 1]; keys with None are left out. Values are
    rounded to 4 decimals first so floating-point noise doesn't break ties."""
    present = sorted(
        ((round(v, 4), k) for k, v in values.items() if v is not None),
        key=lambda x: x[0],
        reverse=not higher_is_better,
    )
    n = len(present)
    if n == 0:
        return {}
    if n == 1:
        return {present[0][1]: 0.5}
    out: dict[Any, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and present[j + 1][0] == present[i][0]:
            j += 1
        pct = ((i + j) / 2) / (n - 1)
        for k in range(i, j + 1):
            out[present[k][1]] = pct
        i = j + 1
    return out


def _excluded_sic(sic: int | None, ranges) -> bool:
    return bool(sic) and any(lo <= sic <= hi for lo, hi in ranges)


def run_screen(
    con: duckdb.DuckDBPyConnection,
    as_of: dt.date,
    settings: dict[str, Any] | None = None,
) -> ScreenResult:
    cfg = screen_settings(settings)
    universe = con.execute(
        "SELECT cik, ticker, name, sic, sic_description, sector FROM screen_universe"
    ).fetchall()
    fundamentals = company_metrics(
        con, as_of, [u[0] for u in universe], int(cfg["max_staleness_days"])
    )
    prices = latest_prices(con, as_of, int(cfg["max_price_age_days"]))

    excluded: dict[str, int] = defaultdict(int)
    passed: list[Candidate] = []
    excluded_sectors = {x.lower() for x in cfg.get("exclude_sectors") or []}
    for cik, ticker, name, sic, sic_desc, sector in universe:
        if (sector and sector.lower() in excluded_sectors) or _excluded_sic(
            sic, cfg["exclude_sic_ranges"]
        ):
            excluded["financials/REITs (different accounts)"] += 1
            continue
        m = fundamentals.get(cik)
        if m is None or m.get("revenue_ttm") is None:
            excluded["no recent financials"] += 1
            continue
        price = prices.get(ticker)
        if price is None or not m.get("shares"):
            excluded["no price or share count"] += 1
            continue
        m = dict(m)
        m["price"], m["price_date"] = price
        m["market_cap"] = m["price"] * m["shares"]
        lo, hi = cfg.get("min_market_cap"), cfg.get("max_market_cap")
        if (lo is not None and m["market_cap"] < lo) or (hi is not None and m["market_cap"] > hi):
            excluded["outside the market-cap range"] += 1
            continue
        if m["revenue_ttm"] < cfg["min_revenue_ttm"]:
            excluded["sales too small"] += 1
            continue
        if (
            cfg["require_positive_operating_income"]
            and not (m.get("operating_income_ttm") or 0) > 0
        ):
            excluded["not profitable (operating loss)"] += 1
            continue
        lev = m.get("net_debt_to_ebitda")
        # Net cash counts as "no debt burden" (0); ranking -0.1x vs -0.3x isn't meaningful.
        m["debt_burden"] = max(lev, 0.0) if lev is not None else None
        ev = m["market_cap"] + m["net_debt"]
        m["enterprise_value"] = ev
        m["earnings_yield"] = m["operating_income_ttm"] / ev if ev > 0 else None
        m["fcf_yield"] = m["fcf_ttm"] / m["market_cap"] if m.get("fcf_ttm") is not None else None
        m["ev_to_sales"] = ev / m["revenue_ttm"] if ev > 0 and m["revenue_ttm"] > 0 else None
        passed.append(Candidate(cik, ticker, name or "", sic, sic_desc, m, sector))

    _score(passed, cfg)
    medians = _industry_medians(passed, int(cfg["min_industry_peers"]))
    for c in passed:
        c.reasons, c.warnings = explain(c, medians)
    passed.sort(key=lambda c: c.score, reverse=True)
    return ScreenResult(as_of, passed, dict(excluded), len(universe), medians)


def _score(cands: list[Candidate], cfg: dict[str, Any]) -> None:
    min_peers = int(cfg["min_industry_peers"])
    missing = float(cfg["missing_percentile"])
    by_industry: dict[Any, list[Candidate]] = defaultdict(list)
    for c in cands:
        by_industry[c.industry].append(c)

    for metrics in PILLARS.values():
        for metric, higher, within_industry in metrics:
            ranks: dict[int, float] = {}
            if within_industry:
                small: list[Candidate] = []
                for group in by_industry.values():
                    with_value = [c for c in group if c.metrics.get(metric) is not None]
                    if len(with_value) >= min_peers:
                        ranks.update(
                            percentile_ranks({c.cik: c.metrics[metric] for c in group}, higher)
                        )
                    else:
                        small.extend(group)
                if small:
                    # Rank thin industries against the whole screen instead.
                    whole = percentile_ranks({c.cik: c.metrics.get(metric) for c in cands}, higher)
                    ranks.update({c.cik: whole[c.cik] for c in small if c.cik in whole})
            else:
                ranks = percentile_ranks({c.cik: c.metrics.get(metric) for c in cands}, higher)
            for c in cands:
                if c.cik in ranks:
                    c.percentiles[metric] = ranks[c.cik]
                elif metric == "debt_burden" and c.metrics.get("net_debt", 0) > 0:
                    c.percentiles[metric] = 0.0  # debt and no EBITDA to pay it from
                else:
                    c.percentiles[metric] = missing

    weights = cfg["weights"]
    total_w = sum(float(weights.get(p, 0)) for p in PILLARS) or 1.0
    for c in cands:
        c.pillars = {
            p: statistics.fmean(c.percentiles[m] for m, _, _ in metrics)
            for p, metrics in PILLARS.items()
        }
        c.score = sum(c.pillars[p] * float(weights.get(p, 0)) for p in PILLARS) / total_w


def _industry_medians(cands: list[Candidate], min_peers: int) -> dict[tuple[Any, str], float]:
    out: dict[tuple[Any, str], float] = {}
    by_industry: dict[Any, list[Candidate]] = defaultdict(list)
    for c in cands:
        by_industry[c.industry].append(c)
    for ind, group in by_industry.items():
        for metric in ("ev_to_sales", "operating_margin", "earnings_yield"):
            vals = [c.metrics[metric] for c in group if c.metrics.get(metric) is not None]
            if len(vals) >= min_peers:
                out[(ind, metric)] = statistics.median(vals)
    return out


# ------------------------------------------------------------------ explanations
def fmt_pct(v: float | None, signed: bool = True) -> str:
    if v is None:
        return "n/a"
    return f"{v * 100:+.0f}%" if signed else f"{v * 100:.0f}%"


def fmt_money(v: float | None) -> str:
    if v is None:
        return "n/a"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e9:
        return f"{sign}${a / 1e9:.1f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:.0f}M"
    return f"{sign}${a:,.0f}"


def explain(c: Candidate, medians: dict[tuple[Any, str], float]) -> tuple[list[str], list[str]]:
    m, p = c.metrics, c.percentiles
    reasons: list[str] = []
    warnings: list[str] = []

    g, acc = m.get("revenue_growth"), m.get("growth_acceleration")
    if g is not None and p.get("revenue_growth", 0) >= 0.7:
        reasons.append(
            f"Sales {fmt_pct(g)} over the last 12 months ({fmt_money(m['revenue_ttm'])})"
        )
    if acc is not None and acc > 0.02:
        reasons.append(
            f"Growth speeding up: latest quarter {fmt_pct(m['quarter_growth'])} vs "
            f"{fmt_pct(m['quarter_growth_prev'])} the quarter before"
        )
    elif acc is not None and acc < -0.05:
        warnings.append(
            f"Growth slowing: latest quarter {fmt_pct(m['quarter_growth'])} vs "
            f"{fmt_pct(m['quarter_growth_prev'])} the quarter before"
        )

    om, omc = m.get("operating_margin"), m.get("operating_margin_change")
    if om is not None and (p.get("operating_margin", 0) >= 0.7 or (omc or 0) > 0.01):
        change = (
            f", {omc * 100:+.1f} pts vs a year ago" if omc is not None and abs(omc) >= 0.005 else ""
        )
        reasons.append(f"Operating margin {fmt_pct(om, signed=False)}{change}")
    if omc is not None and omc < -0.02:
        warnings.append(f"Operating margin down {abs(omc) * 100:.1f} pts vs a year ago")

    lev, nd = m.get("net_debt_to_ebitda"), m.get("net_debt", 0.0)
    if nd < 0 and m.get("debt_known"):
        reasons.append(f"Net cash: {fmt_money(-nd)} more cash than debt")
    elif lev is not None and 0 <= lev < 1.5 and m.get("debt_known"):
        reasons.append(f"Low debt: net debt {lev:.1f}x yearly operating profit (EBITDA)")
    if lev is None or lev > 3:
        warnings.append(
            "Heavy debt: net debt "
            + (f"{lev:.1f}x EBITDA" if lev is not None else "with no EBITDA to cover it")
        )
    if not m.get("debt_known"):
        warnings.append("No debt figure found in the filings; check the balance sheet")

    evs = m.get("ev_to_sales")
    med = medians.get((c.industry, "ev_to_sales"))
    if evs is not None and med and p.get("ev_to_sales", 0) >= 0.7:
        reasons.append(f"Cheap vs its sector: EV/sales {evs:.1f}x vs sector median {med:.1f}x")
    ey = m.get("earnings_yield")
    if ey is not None and p.get("earnings_yield", 0) >= 0.7:
        reasons.append(f"Earnings yield {fmt_pct(ey, signed=False)} (operating profit / EV)")
    fy = m.get("fcf_yield")
    if fy is not None and p.get("fcf_yield", 0) >= 0.7 and fy > 0:
        reasons.append(f"Free cash flow yield {fmt_pct(fy, signed=False)}")
    if m.get("fcf_ttm") is not None and m["fcf_ttm"] < 0:
        warnings.append(f"Burning cash: free cash flow {fmt_money(m['fcf_ttm'])} over 12 months")

    if g is not None and g > 1.0:
        warnings.append("Sales more than doubled; check whether an acquisition explains it")
    if m.get("data_age_days", 0) > 120:
        warnings.append(
            f"Latest report covers the period to {m['period_end']} ({m['data_age_days']} days ago)"
        )
    if m.get("shares_estimated"):
        warnings.append("Share count estimated from diluted average; market cap is approximate")
    if not reasons:
        reasons.append("Ranks well overall without one standout number")
    return reasons, warnings


# ------------------------------------------------------------------ storage and text
def save_results(con: duckdb.DuckDBPyConnection, result: ScreenResult, top_n: int) -> int:
    con.execute("DELETE FROM screen_results WHERE run_date = ?", [result.as_of])
    rows = []
    for rank, c in enumerate(result.candidates[:top_n], start=1):
        metrics = {
            k: (v.isoformat() if isinstance(v, dt.date) else v) for k, v in c.metrics.items()
        }
        rows.append(
            (
                result.as_of,
                c.cik,
                c.ticker,
                c.name,
                rank,
                c.score,
                json.dumps(c.pillars),
                json.dumps(metrics),
                json.dumps(c.reasons),
                json.dumps(c.warnings),
            )
        )
    if rows:
        con.executemany(
            """
            INSERT INTO screen_results
                (run_date, cik, ticker, name, rank, score, pillars, metrics, reasons, warnings)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    return len(rows)


def previous_tickers(con: duckdb.DuckDBPyConnection, before: dt.date) -> set[str]:
    row = con.execute(
        "SELECT max(run_date) FROM screen_results WHERE run_date < ?", [before]
    ).fetchone()
    if not row or row[0] is None:
        return set()
    return {
        r[0]
        for r in con.execute(
            "SELECT ticker FROM screen_results WHERE run_date = ?", [row[0]]
        ).fetchall()
    }


def render_section(
    result: ScreenResult,
    top_n: int,
    previous: set[str] | None = None,
    watchlist: set[str] | None = None,
) -> str:
    previous = previous or set()
    watchlist = watchlist or set()
    picks = result.candidates[:top_n]
    lines = [
        f"NEW CANDIDATES (weekly screen, data as of {result.as_of})",
        f"{len(result.candidates)} of {result.universe_size} companies passed the "
        "filters (profitable, recent filings, no banks/insurers/REITs). Top "
        f"{len(picks)} by growth, quality, balance sheet and price:",
        "",
    ]
    if not picks:
        lines.append("No company passed the filters. Check that prices and financials loaded.")
    for i, c in enumerate(picks, start=1):
        m = c.metrics
        tags = []
        if previous and c.ticker not in previous:
            tags.append("new this week")
        if c.ticker in watchlist:
            tags.append("on your watchlist")
        tag = f" [{', '.join(tags)}]" if tags else ""
        industry = f", {c.industry_label}" if c.industry_label else ""
        lines.append(f"{i}. {c.ticker} {c.name} ({fmt_money(m['market_cap'])}{industry}){tag}")
        lines.append(
            "   Score {:.2f} | growth {:.2f} quality {:.2f} balance {:.2f} value {:.2f}".format(
                c.score, *(c.pillars[p] for p in ("growth", "quality", "balance", "value"))
            )
        )
        for r in c.reasons:
            lines.append(f"   + {r}")
        for w in c.warnings:
            lines.append(f"   ! {w}")
        lines.append(
            f"   Filings: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={c.cik}"
            "&type=10-&dateb=&owner=include&count=40"
        )
        lines.append("")
    lines.append(
        "A screen finds companies that look good on paper today. It does not predict prices. "
        "Read the latest 10-Q/10-K before acting. Not financial advice."
    )
    return "\n".join(lines)
