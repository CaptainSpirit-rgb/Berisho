"""Build synthetic SEC companyfacts documents and prices for screener tests.

Calendar fiscal years. For quarter q of year Y the company files a 10-Q 40 days after
quarter end (3-month and year-to-date values) for q1-q3, and a 10-K 60 days after year
end (annual values only), which is how real filers report. Q4 must be derived.
"""

from __future__ import annotations

import calendar
import datetime as dt

from market_intel.collectors.prices import PriceBar


def q_bounds(year: int, q: int) -> tuple[dt.date, dt.date]:
    start = dt.date(year, 3 * q - 2, 1)
    end_month = 3 * q
    end = dt.date(year, end_month, calendar.monthrange(year, end_month)[1])
    return start, end


def _fact(start, end, val, form, filed, accn, fy=None, fp=None):
    item = {
        "end": end.isoformat(),
        "val": val,
        "accn": accn,
        "fy": fy,
        "fp": fp,
        "form": form,
        "filed": filed.isoformat(),
    }
    if start is not None:
        item["start"] = start.isoformat()
    return item


def companyfacts(
    cik: int,
    years: list[int],
    revenue,  # (year, q) -> quarterly revenue
    op_margin=0.15,  # float or (year, q) -> margin
    shares: float = 100e6,
    cash: float = 50e6,
    debt: float = 20e6,
    revenue_concept: str = "Revenues",
    cfo_ratio: float = 0.12,
    capex_ratio: float = 0.03,
    file_lag_days: int = 40,
) -> dict:
    margin = op_margin if callable(op_margin) else (lambda y, q: op_margin)
    facts: dict[str, list] = {
        revenue_concept: [],
        "OperatingIncomeLoss": [],
        "NetCashProvidedByUsedInOperatingActivities": [],
        "PaymentsToAcquirePropertyPlantAndEquipment": [],
        "CashAndCashEquivalentsAtCarryingValue": [],
        "LongTermDebt": [],
        "StockholdersEquity": [],
    }
    dei_shares = []
    for y in years:
        ytd = {"rev": 0.0, "op": 0.0, "cfo": 0.0, "capex": 0.0}
        for q in (1, 2, 3, 4):
            start, end = q_bounds(y, q)
            rev = revenue(y, q)
            op = rev * margin(y, q)
            ytd["rev"] += rev
            ytd["op"] += op
            ytd["cfo"] += rev * cfo_ratio
            ytd["capex"] += rev * capex_ratio
            year_start = dt.date(y, 1, 1)
            if q < 4:
                form, filed = "10-Q", end + dt.timedelta(days=file_lag_days)
                accn = f"{cik:010d}-{y % 100:02d}-0000{q}"
                fp = f"Q{q}"
                facts[revenue_concept].append(_fact(start, end, rev, form, filed, accn, y, fp))
                facts["OperatingIncomeLoss"].append(_fact(start, end, op, form, filed, accn, y, fp))
                if q > 1:
                    facts[revenue_concept].append(
                        _fact(year_start, end, ytd["rev"], form, filed, accn, y, fp)
                    )
                    facts["OperatingIncomeLoss"].append(
                        _fact(year_start, end, ytd["op"], form, filed, accn, y, fp)
                    )
            else:
                form, filed = "10-K", end + dt.timedelta(days=file_lag_days + 20)
                accn = f"{cik:010d}-{(y + 1) % 100:02d}-00010"
                fp = "FY"
                facts[revenue_concept].append(
                    _fact(year_start, end, ytd["rev"], form, filed, accn, y, fp)
                )
                facts["OperatingIncomeLoss"].append(
                    _fact(year_start, end, ytd["op"], form, filed, accn, y, fp)
                )
            # Cash flow is year-to-date only, like most real filers.
            facts["NetCashProvidedByUsedInOperatingActivities"].append(
                _fact(year_start, end, ytd["cfo"], form, filed, accn, y, fp)
            )
            facts["PaymentsToAcquirePropertyPlantAndEquipment"].append(
                _fact(year_start, end, ytd["capex"], form, filed, accn, y, fp)
            )
            facts["CashAndCashEquivalentsAtCarryingValue"].append(
                _fact(None, end, cash, form, filed, accn, y, fp)
            )
            facts["LongTermDebt"].append(_fact(None, end, debt, form, filed, accn, y, fp))
            facts["StockholdersEquity"].append(_fact(None, end, 500e6, form, filed, accn, y, fp))
            dei_shares.append(
                _fact(None, end + dt.timedelta(days=30), shares, form, filed, accn, y, fp)
            )
    return {
        "cik": cik,
        "entityName": f"Company {cik}",
        "facts": {
            "us-gaap": {name: {"units": {"USD": items}} for name, items in facts.items()},
            "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": dei_shares}}},
        },
    }


def flat_prices(symbol: str, start: dt.date, end: dt.date, price: float, daily_growth=0.0):
    """Weekday bars from start to end inclusive, compounding ``daily_growth``."""
    bars = []
    d, px = start, price
    while d <= end:
        if d.weekday() < 5:
            bars.append(PriceBar(symbol, d, px, px, px, px, px, 1_000_000))
            px *= 1 + daily_growth
        d += dt.timedelta(days=1)
    return bars
