"""SEC XBRL financials for the screener.

Source: SEC ``companyfacts``: every XBRL value a company has filed, with the filing
date of each report, so point-in-time queries are possible. Two ways in:

* **Per company** (default, S&P 500): one API call per company, see below.
* **Bulk** (only for the ``all_listed`` universe): ``companyfacts.zip`` from EDGAR, one
  download holding every company. Free, but over 1 GB, so it is streamed.
* Per-company URL: ``https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json``.

Only the concepts in ``CONCEPTS`` are kept. Each screener metric lists several XBRL
concepts in priority order because companies use different tags (and switch tags over
the years, e.g. ``SalesRevenueNet`` before 2018 and
``RevenueFromContractWithCustomerExcludingAssessedTax`` after). Rows are upserted into
``xbrl_facts`` keyed by (cik, metric, concept, period_end, duration_days, accession),
so re-ingesting is idempotent.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

log = logging.getLogger(__name__)

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
BULK_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"

# metric -> (unit, [(taxonomy, concept), ...] in priority order)
CONCEPTS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "revenue": (
        "USD",
        [
            ("us-gaap", "Revenues"),
            ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
            ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
            ("us-gaap", "SalesRevenueNet"),
            ("us-gaap", "SalesRevenueGoodsNet"),
            ("us-gaap", "SalesRevenueServicesNet"),
        ],
    ),
    "gross_profit": ("USD", [("us-gaap", "GrossProfit")]),
    "operating_income": ("USD", [("us-gaap", "OperatingIncomeLoss")]),
    "net_income": (
        "USD",
        [("us-gaap", "NetIncomeLoss"), ("us-gaap", "ProfitLoss")],
    ),
    "depreciation": (
        "USD",
        [
            ("us-gaap", "DepreciationDepletionAndAmortization"),
            ("us-gaap", "DepreciationAndAmortization"),
            ("us-gaap", "DepreciationAmortizationAndAccretionNet"),
            ("us-gaap", "Depreciation"),
        ],
    ),
    "operating_cash_flow": (
        "USD",
        [
            ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
            ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
        ],
    ),
    "capex": (
        "USD",
        [
            ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
            ("us-gaap", "PaymentsToAcquireProductiveAssets"),
        ],
    ),
    "cash": (
        "USD",
        [
            ("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
            ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
            ("us-gaap", "Cash"),
        ],
    ),
    "debt_total": (
        "USD",
        [
            ("us-gaap", "LongTermDebt"),
            ("us-gaap", "DebtLongtermAndShorttermCombinedAmount"),
        ],
    ),
    "debt_noncurrent": (
        "USD",
        [
            ("us-gaap", "LongTermDebtNoncurrent"),
            ("us-gaap", "LongTermDebtAndCapitalLeaseObligations"),
        ],
    ),
    "debt_current": (
        "USD",
        [
            ("us-gaap", "LongTermDebtCurrent"),
            ("us-gaap", "DebtCurrent"),
        ],
    ),
    "equity": (
        "USD",
        [
            ("us-gaap", "StockholdersEquity"),
            (
                "us-gaap",
                "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            ),
        ],
    ),
    "shares": (
        "shares",
        [
            ("dei", "EntityCommonStockSharesOutstanding"),
            ("us-gaap", "CommonStockSharesOutstanding"),
        ],
    ),
    "shares_diluted": (
        "shares",
        [("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding")],
    ),
}

# Only annual and quarterly reports (and their amendments) feed the screen. Values from
# S-1s, 8-Ks etc. are rarer and can carry pro-forma numbers.
FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A", "10-KT"}

CONCEPT_PRIORITY: dict[tuple[str, str], int] = {
    (metric, concept): i
    for metric, (_unit, concepts) in CONCEPTS.items()
    for i, (_tax, concept) in enumerate(concepts)
}


@dataclass(frozen=True)
class Fact:
    cik: int
    metric: str
    concept: str
    period_start: dt.date | None
    period_end: dt.date
    duration_days: int
    value: float
    form_type: str | None
    accession_no: str
    filed_date: dt.date


def _date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def parse_companyfacts(data: dict[str, Any]) -> list[Fact]:
    """Extract the screener's concepts from one companyfacts JSON document."""
    cik = int(data.get("cik") or 0)
    facts_by_tax = data.get("facts") or {}
    out: list[Fact] = []
    seen: set[tuple] = set()
    for metric, (unit, concepts) in CONCEPTS.items():
        for taxonomy, concept in concepts:
            node = (facts_by_tax.get(taxonomy) or {}).get(concept)
            if not node:
                continue
            for item in (node.get("units") or {}).get(unit) or []:
                form = item.get("form")
                if form not in FORMS:
                    continue
                end = _date(item.get("end"))
                filed = _date(item.get("filed"))
                accn = item.get("accn")
                val = item.get("val")
                if end is None or filed is None or not accn or val is None:
                    continue
                start = _date(item.get("start"))
                duration = (end - start).days + 1 if start else 0
                key = (metric, concept, end, duration, accn)
                if key in seen:  # the same value can appear twice (frames)
                    continue
                seen.add(key)
                out.append(
                    Fact(
                        cik=cik,
                        metric=metric,
                        concept=concept,
                        period_start=start,
                        period_end=end,
                        duration_days=duration,
                        value=float(val),
                        form_type=form,
                        accession_no=accn,
                        filed_date=filed,
                    )
                )
    return out


_COLUMNS = [
    "cik",
    "metric",
    "concept",
    "period_start",
    "period_end",
    "duration_days",
    "value",
    "form_type",
    "accession_no",
    "filed_date",
]


def upsert_facts(con: duckdb.DuckDBPyConnection, facts: Iterable[Fact]) -> int:
    """Bulk upsert through a DataFrame (row-by-row inserts are far too slow for the
    millions of facts in a full load)."""
    import pandas as pd

    rows = [
        (
            f.cik,
            f.metric,
            f.concept,
            f.period_start,
            f.period_end,
            f.duration_days,
            f.value,
            f.form_type,
            f.accession_no,
            f.filed_date,
        )
        for f in facts
    ]
    if not rows:
        return 0
    df = pd.DataFrame(rows, columns=_COLUMNS)
    for col in ("period_start", "period_end", "filed_date"):
        df[col] = pd.to_datetime(df[col])
    # A cursor of its own, so a half-read result elsewhere on ``con`` can't block it.
    cur = con.cursor()
    cur.register("_facts_batch", df)
    try:
        cur.execute(
            f"""
            INSERT OR REPLACE INTO xbrl_facts ({", ".join(_COLUMNS)}, ingested_at)
            SELECT cik, metric, concept, CAST(period_start AS DATE), CAST(period_end AS DATE),
                   duration_days, value, form_type, accession_no, CAST(filed_date AS DATE),
                   current_timestamp
            FROM _facts_batch
            """
        )
    finally:
        cur.close()
    return len(rows)


_CIK_FILE = re.compile(r"CIK(\d{10})\.json$")


def iter_bulk_zip(path: Path, ciks: set[int] | None = None) -> Iterator[dict[str, Any]]:
    """Yield companyfacts documents from the bulk zip, optionally only for ``ciks``."""
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            m = _CIK_FILE.search(name)
            if not m:
                continue
            if ciks is not None and int(m.group(1)) not in ciks:
                continue
            with zf.open(name) as fh:
                try:
                    yield json.load(fh)
                except json.JSONDecodeError:
                    log.warning("Skipping unreadable %s", name)


def ingest_bulk_zip(
    con: duckdb.DuckDBPyConnection,
    path: Path,
    ciks: set[int] | None = None,
    batch_rows: int = 200_000,
) -> tuple[int, int]:
    """Load the screener concepts for every company in the bulk zip.

    Returns (companies loaded, fact rows upserted).
    """
    companies = rows = 0
    buffer: list[Fact] = []
    for doc in iter_bulk_zip(path, ciks):
        facts = parse_companyfacts(doc)
        if not facts:
            continue
        buffer.extend(facts)
        companies += 1
        if len(buffer) >= batch_rows:
            rows += upsert_facts(con, buffer)
            buffer = []
            log.info("companyfacts: %d companies, %d rows so far", companies, rows)
    rows += upsert_facts(con, buffer)
    return companies, rows


def download_bulk_zip(client, dest: Path) -> Path:
    """Download companyfacts.zip with the SEC client (User-Agent + rate limit)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    client.download(BULK_URL, tmp)
    tmp.replace(dest)
    return dest


def fetch_company(con: duckdb.DuckDBPyConnection, client, cik: int) -> int:
    """Fetch and load one company's facts from the API. Returns rows upserted."""
    data = client.get_json(COMPANYFACTS_URL.format(cik=cik))
    return upsert_facts(con, parse_companyfacts(data))


def fetch_companies(
    con: duckdb.DuckDBPyConnection, client, ciks: Iterable[int]
) -> tuple[int, list[int]]:
    """Fetch companyfacts for each CIK from the API (about 500 calls for the S&P 500,
    a minute or two at SEC's rate limit). Returns (rows upserted, CIKs that failed)."""
    rows = 0
    failed: list[int] = []
    ciks = list(ciks)
    for i, cik in enumerate(ciks, start=1):
        try:
            rows += fetch_company(con, client, cik)
        except Exception as exc:  # noqa: BLE001 - one bad company shouldn't stop the run
            log.warning("companyfacts failed for CIK %s: %s", cik, exc)
            failed.append(cik)
        if i % 100 == 0:
            log.info("companyfacts: %d/%d companies", i, len(ciks))
    return rows, failed
