"""Point-in-time fundamentals: the numbers a company had *published* by a given date.

``company_metrics(con, as_of)`` reads ``xbrl_facts`` using only rows with
``filed_date <= as_of`` (the latest report of each period wins, so restatements count
from the day they were filed, not before). That is what makes the backtest honest.

XBRL quirks handled here:

* 10-Qs report the quarter *and* year-to-date; 10-Ks report only the full year. Q4 and
  any missing quarter are derived by subtraction (full year - 9 months, etc.).
* Cash-flow lines are usually year-to-date only, so the same derivation applies.
* Companies switch tags over time; per metric the tag used for the latest period is
  preferred everywhere and other tags only fill gaps (avoids fake growth from mixing a
  broad "Revenues" with a narrower tag).
* Fiscal years of 52/53 weeks mean period ends drift by a few days; dates are matched
  with a tolerance.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import duckdb

from market_intel.screener.facts import CONCEPT_PRIORITY

FLOW_METRICS = (
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "depreciation",
    "operating_cash_flow",
    "capex",
    "shares_diluted",
)
INSTANT_METRICS = (
    "cash",
    "debt_total",
    "debt_noncurrent",
    "debt_current",
    "equity",
    "shares",
)

QUARTER = (80, 100)  # duration in days
ANNUAL = (350, 380)
DATE_TOL = 12  # days of slack when matching period ends across quarters/years
HISTORY_DAYS = 3 * 365 + 120  # facts older than this before as_of aren't needed


@dataclass
class Series:
    """One flow metric for one company: values keyed by (start, end)."""

    by_period: dict[tuple[dt.date, dt.date], float] = field(default_factory=dict)
    _quarters: dict[dt.date, float] | None = field(default=None, repr=False)
    _annuals: dict[dt.date, float] | None = field(default=None, repr=False)

    def quarters(self) -> dict[dt.date, float]:
        if self._quarters is None:
            self._quarters = self._derive_quarters()
        return self._quarters

    def _derive_quarters(self) -> dict[dt.date, float]:
        q: dict[dt.date, float] = {}
        for (start, end), v in self.by_period.items():
            if QUARTER[0] <= (end - start).days + 1 <= QUARTER[1]:
                q[end] = v
        # Derive missing quarters: a longer period minus the same-start period that
        # ends one quarter earlier (FY - 9M = Q4, 6M - 3M = Q2, ...). Two passes let a
        # derived quarter feed the next one.
        for _ in range(2):
            for (start, end), v in self.by_period.items():
                if (end - start).days + 1 <= QUARTER[1] or _near_key(q, end, 3):
                    continue
                for (s2, e2), v2 in self.by_period.items():
                    if s2 == start and QUARTER[0] <= (end - e2).days <= QUARTER[1] + 2:
                        q[end] = v - v2
                        break
        return q

    def annuals(self) -> dict[dt.date, float]:
        if self._annuals is None:
            self._annuals = {
                end: v
                for (start, end), v in self.by_period.items()
                if ANNUAL[0] <= (end - start).days + 1 <= ANNUAL[1]
            }
        return self._annuals


def _near_key(d: dict[dt.date, Any], target: dt.date, tol: int = DATE_TOL) -> dt.date | None:
    best = None
    for k in d:
        gap = abs((k - target).days)
        if gap <= tol and (best is None or gap < abs((best - target).days)):
            best = k
    return best


def _near(d: dict[dt.date, float], target: dt.date, tol: int = DATE_TOL) -> float | None:
    k = _near_key(d, target, tol)
    return d[k] if k is not None else None


def ttm(series: Series, end: dt.date) -> float | None:
    """Trailing twelve months ending near ``end``: the annual figure if one ends there,
    otherwise the sum of four consecutive quarters."""
    annual = _near(series.annuals(), end, 5)
    if annual is not None:
        return annual
    q = series.quarters()
    vals = [_near(q, end - dt.timedelta(days=91 * i)) for i in range(4)]
    if any(v is None for v in vals):
        return None
    return float(sum(vals))  # type: ignore[arg-type]


def quarter(series: Series, end: dt.date) -> float | None:
    return _near(series.quarters(), end)


def _ratio_growth(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old <= 0:
        return None
    return new / old - 1


def _div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def load_facts(con: duckdb.DuckDBPyConnection, as_of: dt.date, ciks: list[int] | None = None):
    """Latest-known value of every (company, metric, tag, period) as of ``as_of``."""
    params: list[Any] = [as_of, as_of - dt.timedelta(days=HISTORY_DAYS)]
    cik_filter = ""
    if ciks is not None:
        if not ciks:
            return []
        cik_filter = f"AND cik IN ({','.join(str(int(c)) for c in ciks)})"
    return con.execute(
        f"""
        SELECT cik, metric, concept, period_start, period_end, duration_days, value
        FROM (
            SELECT *, row_number() OVER (
                PARTITION BY cik, metric, concept, period_end, duration_days
                ORDER BY filed_date DESC, accession_no DESC) AS rn
            FROM xbrl_facts
            WHERE filed_date <= ? AND period_end >= ? {cik_filter}
        )
        WHERE rn = 1
        """,
        params,
    ).fetchall()


def _choose_concepts(rows_by_period: dict[tuple, dict[str, float]]) -> dict[tuple, float]:
    """Per period pick one tag: the one used for the latest period if present,
    otherwise the highest-priority tag available."""
    if not rows_by_period:
        return {}
    latest_period = max(rows_by_period, key=lambda p: (p[1], -(p[1] - p[0]).days if p[0] else 0))
    preferred = min(rows_by_period[latest_period], key=lambda c: c[1])
    out = {}
    for period, by_concept in rows_by_period.items():
        if preferred in by_concept:
            out[period] = by_concept[preferred]
        else:
            out[period] = by_concept[min(by_concept, key=lambda c: c[1])]
    return out


def company_metrics(
    con: duckdb.DuckDBPyConnection,
    as_of: dt.date,
    ciks: list[int] | None = None,
    max_staleness_days: int = 200,
) -> dict[int, dict[str, Any]]:
    """Fundamental metrics per CIK as known on ``as_of``. Price-based numbers (market
    cap, valuation) are added later by the screen."""
    # cik -> metric -> period -> {(concept, priority): value}
    raw: dict[int, dict[str, dict[tuple, dict[tuple[str, int], float]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(dict))
    )
    for cik, metric, concept, start, end, duration, value in load_facts(con, as_of, ciks):
        if duration == 0:
            period = (None, end)
        else:
            period = (start, end)
        prio = CONCEPT_PRIORITY.get((metric, concept), 99)
        raw[cik][metric][period][(concept, prio)] = value

    out: dict[int, dict[str, Any]] = {}
    for cik, metrics in raw.items():
        m = compute_one(metrics, as_of, max_staleness_days)
        if m is not None:
            out[cik] = m
    return out


def compute_one(metrics: dict, as_of: dt.date, max_staleness_days: int) -> dict[str, Any] | None:
    flows: dict[str, Series] = {}
    for name in FLOW_METRICS:
        chosen = _choose_concepts(
            {p: v for p, v in metrics.get(name, {}).items() if p[0] is not None}
        )
        flows[name] = Series(chosen)  # type: ignore[arg-type]
    instants: dict[str, dict[dt.date, float]] = {}
    for name in INSTANT_METRICS:
        chosen = _choose_concepts({p: v for p, v in metrics.get(name, {}).items() if p[0] is None})
        instants[name] = {p[1]: v for p, v in chosen.items()}

    rev = flows["revenue"]
    ends = list(rev.quarters()) + list(rev.annuals())
    if not ends:
        return None
    end = max(ends)
    age = (as_of - end).days
    if age > max_staleness_days:
        return None

    year_ago = end - dt.timedelta(days=364)
    prev_q = end - dt.timedelta(days=91)

    rev_ttm = ttm(rev, end)
    rev_ttm_prev = ttm(rev, year_ago)
    q_now, q_year_ago = quarter(rev, end), quarter(rev, year_ago)
    q_prev, q_prev_year_ago = quarter(rev, prev_q), quarter(rev, prev_q - dt.timedelta(days=364))
    q_growth = _ratio_growth(q_now, q_year_ago)
    q_growth_prev = _ratio_growth(q_prev, q_prev_year_ago)

    op_ttm = ttm(flows["operating_income"], end)
    op_ttm_prev = ttm(flows["operating_income"], year_ago)
    gp_ttm = ttm(flows["gross_profit"], end)
    gp_ttm_prev = ttm(flows["gross_profit"], year_ago)
    ni_ttm = ttm(flows["net_income"], end)
    cfo_ttm = ttm(flows["operating_cash_flow"], end)
    capex_ttm = ttm(flows["capex"], end)
    dep_ttm = ttm(flows["depreciation"], end)

    op_margin = _div(op_ttm, rev_ttm)
    op_margin_prev = _div(op_ttm_prev, rev_ttm_prev)
    gross_margin = _div(gp_ttm, rev_ttm)
    gross_margin_prev = _div(gp_ttm_prev, rev_ttm_prev)
    fcf = None if cfo_ttm is None else cfo_ttm - (capex_ttm or 0.0)
    ebitda = None if op_ttm is None else op_ttm + (dep_ttm or 0.0)

    def latest_instant(name: str) -> float | None:
        # Balance-sheet values from roughly the latest report (not years old).
        vals = {d: v for d, v in instants[name].items() if d >= end - dt.timedelta(days=100)}
        return vals[max(vals)] if vals else None

    cash = latest_instant("cash")
    debt_total = latest_instant("debt_total")
    debt_nc = latest_instant("debt_noncurrent")
    debt_cur = latest_instant("debt_current")
    if debt_total is not None:
        debt, debt_known = debt_total, True
    elif debt_nc is not None or debt_cur is not None:
        debt, debt_known = (debt_nc or 0.0) + (debt_cur or 0.0), True
    else:
        debt, debt_known = 0.0, False
    net_debt = debt - (cash or 0.0)

    shares = latest_instant("shares")
    shares_estimated = False
    if shares is None or shares <= 0:
        diluted = flows["shares_diluted"].quarters() or flows["shares_diluted"].annuals()
        shares = diluted[max(diluted)] if diluted else None
        shares_estimated = shares is not None

    if ebitda is not None and ebitda > 0:
        leverage = net_debt / ebitda
    elif net_debt <= 0:
        leverage = 0.0  # net cash but no operating profit: no debt problem
    else:
        leverage = None  # debt but no positive EBITDA: worst case, handled by the screen

    return {
        "period_end": end,
        "data_age_days": age,
        "revenue_ttm": rev_ttm,
        "revenue_growth": _ratio_growth(rev_ttm, rev_ttm_prev),
        "quarter_growth": q_growth,
        "quarter_growth_prev": q_growth_prev,
        "growth_acceleration": (
            q_growth - q_growth_prev if q_growth is not None and q_growth_prev is not None else None
        ),
        "operating_income_ttm": op_ttm,
        "operating_margin": op_margin,
        "operating_margin_change": (
            op_margin - op_margin_prev
            if op_margin is not None and op_margin_prev is not None
            else None
        ),
        "gross_margin": gross_margin,
        "gross_margin_change": (
            gross_margin - gross_margin_prev
            if gross_margin is not None and gross_margin_prev is not None
            else None
        ),
        "net_income_ttm": ni_ttm,
        "fcf_ttm": fcf,
        "fcf_margin": _div(fcf, rev_ttm),
        "ebitda_ttm": ebitda,
        "cash": cash,
        "debt": debt,
        "debt_known": debt_known,
        "net_debt": net_debt,
        "net_debt_to_ebitda": leverage,
        "equity": latest_instant("equity"),
        "shares": shares,
        "shares_estimated": shares_estimated,
    }
