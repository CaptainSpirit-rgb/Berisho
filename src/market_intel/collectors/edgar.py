"""SEC EDGAR filings collector.

Two steps per watchlist company:

1. **Filings index** from the submissions API
   (``https://data.sec.gov/submissions/CIK##########.json``). Rows for the configured
   form types are upserted into ``filings`` keyed by accession number.
2. **Form 4 details**: for recent Form 4 filings not parsed yet, the raw ownership XML
   is fetched and its non-derivative transactions are upserted into
   ``insider_transactions`` keyed by (accession_no, line_no).

Both steps are idempotent. SEC asks for a descriptive User-Agent with a contact email
(``SEC_USER_AGENT`` in ``.env``) and at most 10 requests per second; ``SecClient``
enforces both.

Point-in-time note: ``acceptanceDateTime`` in the submissions JSON is true UTC (the
``Z`` is honest). Checked against a live index page: NVDA's 8-K 0001045810-26-000088
shows "Accepted 2026-11-17 16:20:51" (New York time) and the JSON says
``2026-11-17T21:20:51.000Z``. It is stored as naive UTC unchanged.

Filer vs subject: a company's submissions feed also lists filings it made *about other
companies*: Goldman's Schedule 13G stakes in other issuers, Exxon's 13D on Imperial Oil,
or Form 4s where the company is a 10% owner of another issuer. Each filing therefore
gets ``subject_cik``: the company itself for 8-K/10-Q/10-K, the issuer from the Form 4
XML, and the SUBJECT-COMPANY CIK from the SEC header for 13D/13G. Only rows with
``subject_cik = cik`` are about the watchlist company.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

import duckdb

from market_intel.config import Security

log = logging.getLogger(__name__)

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{doc}"
HEADERS_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{acc}-index-headers.html"

DEFAULT_FORMS = (
    "8-K",
    "8-K/A",
    "10-Q",
    "10-K",
    "10-K/A",
    "4",
    "4/A",
    "SC 13D",
    "SC 13D/A",
    "SC 13G",
    "SC 13G/A",
    "SCHEDULE 13D",
    "SCHEDULE 13D/A",
    "SCHEDULE 13G",
    "SCHEDULE 13G/A",
)
FORM4_TYPES = {"4", "4/A"}


def is_stake_form(form: str) -> bool:
    """Schedule 13D/13G (old 'SC 13D' and new 'SCHEDULE 13D' names, amendments too)."""
    return "13D" in form or "13G" in form


def needs_subject_check(form: str) -> bool:
    """Forms where the listed company may be the filer rather than the subject."""
    return form in FORM4_TYPES or is_stake_form(form)


class MissingUserAgentError(RuntimeError):
    pass


class HttpClient(Protocol):
    def get_json(self, url: str) -> dict[str, Any]: ...
    def get_text(self, url: str) -> str: ...


class SecClient:
    """Minimal EDGAR HTTP client: required User-Agent, rate limit, retry on 429/5xx."""

    def __init__(
        self,
        user_agent: str | None = None,
        max_requests_per_second: float = 10,
        max_retries: int = 3,
        timeout: float = 30,
    ) -> None:
        import requests

        user_agent = user_agent or os.environ.get("SEC_USER_AGENT", "").strip()
        if not user_agent or "@" not in user_agent:
            raise MissingUserAgentError(
                "SEC_USER_AGENT must be set to something like 'market-intel you@example.com' "
                "(SEC requires a contact email). Add it to .env."
            )
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self._min_interval = 1.0 / max_requests_per_second
        self._last = 0.0
        self._lock = threading.Lock()
        self._max_retries = max_retries
        self._timeout = timeout

    def _throttle(self) -> None:
        with self._lock:
            wait = self._last + self._min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def _get(self, url: str):
        for attempt in range(self._max_retries + 1):
            self._throttle()
            resp = self._session.get(url, timeout=self._timeout)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self._max_retries:
                backoff = 2**attempt
                log.warning("SEC %s for %s, retrying in %ss", resp.status_code, url, backoff)
                time.sleep(backoff)
                continue
            resp.raise_for_status()
            return resp
        raise RuntimeError("unreachable")

    def get_json(self, url: str) -> dict[str, Any]:
        return self._get(url).json()

    def get_text(self, url: str) -> str:
        return self._get(url).text


# ------------------------------------------------------------------ filings index


@dataclass(frozen=True)
class Filing:
    accession_no: str
    cik: int
    symbol: str | None
    form_type: str
    filed_date: dt.date | None
    accepted_at: dt.datetime | None  # naive UTC
    period_of_report: dt.date | None
    items: str | None
    primary_doc: str | None
    primary_doc_url: str | None
    subject_cik: int | None = None  # None until resolved (Form 4, 13D/13G)


def _date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def parse_acceptance(value: str | None) -> dt.datetime | None:
    """EDGAR acceptanceDateTime ('2026-11-17T21:20:51.000Z', true UTC) -> naive UTC."""
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.rstrip("Z")[:19])
    except ValueError:
        return None


def archive_url(cik: int, accession_no: str, doc: str) -> str:
    return ARCHIVES_URL.format(cik=cik, acc_nodash=accession_no.replace("-", ""), doc=doc)


def parse_submissions(
    payload: dict[str, Any],
    cik: int,
    symbol: str | None,
    forms: Iterable[str] = DEFAULT_FORMS,
    since: dt.date | None = None,
) -> list[Filing]:
    """Turn the column-oriented ``filings.recent`` block into Filing rows."""
    recent = (payload.get("filings") or {}).get("recent") or {}
    accessions = recent.get("accessionNumber") or []
    wanted = set(forms)

    def col(name: str, i: int):
        values = recent.get(name) or []
        return values[i] if i < len(values) else None

    out = []
    for i, acc in enumerate(accessions):
        form = col("form", i)
        if form not in wanted:
            continue
        filed = _date(col("filingDate", i))
        if since and filed and filed < since:
            continue
        doc = col("primaryDocument", i) or None
        out.append(
            Filing(
                accession_no=acc,
                cik=cik,
                symbol=symbol,
                form_type=form,
                filed_date=filed,
                accepted_at=parse_acceptance(col("acceptanceDateTime", i)),
                period_of_report=_date(col("reportDate", i)),
                items=col("items", i) or None,
                primary_doc=doc,
                primary_doc_url=archive_url(cik, acc, doc) if doc else None,
                subject_cik=None if needs_subject_check(form) else cik,
            )
        )
    return out


def upsert_filings(con: duckdb.DuckDBPyConnection, filings: Iterable[Filing]) -> int:
    rows = [
        (
            f.accession_no,
            f.cik,
            f.symbol,
            f.form_type,
            f.filed_date,
            f.accepted_at,
            f.period_of_report,
            f.items,
            f.primary_doc_url,
            f.subject_cik,
        )
        for f in filings
    ]
    if rows:
        # ON CONFLICT keeps details_parsed_at and a resolved subject_cik, so Form 4s and
        # 13D/13G headers aren't re-fetched every run.
        con.executemany(
            """
            INSERT INTO filings (accession_no, cik, symbol, form_type, filed_date,
                                 accepted_at, period_of_report, items, primary_doc_url,
                                 subject_cik)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (accession_no) DO UPDATE SET
                cik = excluded.cik, symbol = excluded.symbol,
                form_type = excluded.form_type, filed_date = excluded.filed_date,
                accepted_at = excluded.accepted_at,
                period_of_report = excluded.period_of_report, items = excluded.items,
                primary_doc_url = excluded.primary_doc_url,
                subject_cik = coalesce(excluded.subject_cik, filings.subject_cik),
                ingested_at = now()
            """,
            rows,
        )
    return len(rows)


# ------------------------------------------------------------------ Form 4


@dataclass(frozen=True)
class InsiderTransaction:
    accession_no: str
    line_no: int
    cik: int
    symbol: str | None
    insider_name: str | None
    insider_title: str | None
    transaction_date: dt.date | None
    transaction_code: str | None
    acquired_disposed: str | None
    shares: float | None
    price: float | None
    shares_after: float | None
    accepted_at: dt.datetime | None


def form4_xml_url(cik: int, accession_no: str, primary_doc: str) -> str:
    """The submissions API points at the XSL-rendered HTML ('xslF345X05/x.xml');
    the raw XML lives at the same name without the xsl directory."""
    return archive_url(cik, accession_no, primary_doc.rsplit("/", 1)[-1])


def _text(node: ET.Element | None, path: str) -> str | None:
    if node is None:
        return None
    found = node.find(path)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def form4_issuer_cik(xml_text: str) -> int | None:
    root = ET.fromstring(xml_text.strip().encode())
    value = _text(root, "issuer/issuerCik")
    return int(value) if value and value.isdigit() else None


def _insider_title(owner_rel: ET.Element | None) -> str | None:
    if owner_rel is None:
        return None
    parts = []
    if _text(owner_rel, "officerTitle"):
        parts.append(_text(owner_rel, "officerTitle"))
    elif _text(owner_rel, "isOfficer") in ("1", "true"):
        parts.append("Officer")
    if _text(owner_rel, "isDirector") in ("1", "true"):
        parts.append("Director")
    if _text(owner_rel, "isTenPercentOwner") in ("1", "true"):
        parts.append("10% owner")
    return ", ".join(parts) or None


def parse_form4(
    xml_text: str,
    accession_no: str,
    cik: int,
    symbol: str | None,
    accepted_at: dt.datetime | None,
) -> list[InsiderTransaction]:
    """Parse non-derivative transactions from a Form 4 ownership XML document.

    Derivative transactions (option grants/exercises) are skipped for now; open-market
    buys and sells (codes P and S) are in the non-derivative table.
    """
    root = ET.fromstring(xml_text.strip().encode())
    owner = root.find("reportingOwner")
    name = _text(owner, "reportingOwnerId/rptOwnerName")
    title = _insider_title(owner.find("reportingOwnerRelationship") if owner is not None else None)

    out = []
    for i, txn in enumerate(root.findall("nonDerivativeTable/nonDerivativeTransaction"), 1):
        out.append(
            InsiderTransaction(
                accession_no=accession_no,
                line_no=i,
                cik=cik,
                symbol=symbol,
                insider_name=name,
                insider_title=title,
                transaction_date=_date(_text(txn, "transactionDate/value")),
                transaction_code=_text(txn, "transactionCoding/transactionCode"),
                acquired_disposed=_text(
                    txn, "transactionAmounts/transactionAcquiredDisposedCode/value"
                ),
                shares=_float(_text(txn, "transactionAmounts/transactionShares/value")),
                price=_float(_text(txn, "transactionAmounts/transactionPricePerShare/value")),
                shares_after=_float(
                    _text(txn, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")
                ),
                accepted_at=accepted_at,
            )
        )
    return out


def upsert_insider_transactions(
    con: duckdb.DuckDBPyConnection, txns: Iterable[InsiderTransaction]
) -> int:
    rows = [
        (
            t.accession_no,
            t.line_no,
            t.cik,
            t.symbol,
            t.insider_name,
            t.insider_title,
            t.transaction_date,
            t.transaction_code,
            t.acquired_disposed,
            t.shares,
            t.price,
            t.shares_after,
            t.accepted_at,
        )
        for t in txns
    ]
    if rows:
        con.executemany(
            """
            INSERT OR REPLACE INTO insider_transactions
                (accession_no, line_no, cik, symbol, insider_name, insider_title,
                 transaction_date, transaction_code, acquired_disposed, shares, price,
                 shares_after, accepted_at, ingested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)
            """,
            rows,
        )
    return len(rows)


def pending_form4(
    con: duckdb.DuckDBPyConnection, cik: int, since: dt.date
) -> list[tuple[str, str, str | None, dt.datetime | None]]:
    """Form 4 filings for a company not parsed yet: (accession, doc_url, symbol, accepted)."""
    return con.execute(
        """
        SELECT accession_no, primary_doc_url, symbol, accepted_at
        FROM filings
        WHERE cik = ? AND form_type IN ('4', '4/A') AND details_parsed_at IS NULL
          AND primary_doc_url IS NOT NULL AND filed_date >= ?
        ORDER BY filed_date
        """,
        [cik, since],
    ).fetchall()


# ------------------------------------------------------------------ 13D / 13G subject

_SUBJECT_CIK = re.compile(r"<SUBJECT-COMPANY>.*?<CIK>\s*(\d+)", re.S)


def headers_url(cik: int, accession_no: str) -> str:
    return HEADERS_URL.format(cik=cik, acc_nodash=accession_no.replace("-", ""), acc=accession_no)


def parse_subject_cik(headers_text: str) -> int | None:
    """SUBJECT-COMPANY CIK from a filing's SEC header (the -index-headers.html page)."""
    m = _SUBJECT_CIK.search(headers_text)
    return int(m.group(1)) if m else None


def pending_stake_filings(con: duckdb.DuckDBPyConnection, cik: int, since: dt.date) -> list[str]:
    """13D/13G accession numbers for a company whose subject isn't resolved yet."""
    rows = con.execute(
        """
        SELECT accession_no FROM filings
        WHERE cik = ? AND details_parsed_at IS NULL AND filed_date >= ?
          AND (form_type LIKE '%13D%' OR form_type LIKE '%13G%')
        ORDER BY filed_date
        """,
        [cik, since],
    ).fetchall()
    return [r[0] for r in rows]


def _mark_parsed(con: duckdb.DuckDBPyConnection, acc: str, subject_cik: int | None) -> None:
    con.execute(
        "UPDATE filings SET details_parsed_at = current_timestamp, subject_cik = ? "
        "WHERE accession_no = ?",
        [subject_cik, acc],
    )


# ------------------------------------------------------------------ orchestration


@dataclass
class EdgarResult:
    symbol: str
    filings: int = 0
    form4_parsed: int = 0
    insider_rows: int = 0
    stake_checked: int = 0
    not_subject: int = 0  # filings the company made about other issuers
    error: str | None = None


def collect_filings(
    con: duckdb.DuckDBPyConnection,
    securities: Iterable[Security],
    client: HttpClient,
    forms: Iterable[str] = DEFAULT_FORMS,
    lookback_days: int = 365,
    form4_lookback_days: int = 90,
    today: dt.date | None = None,
) -> list[EdgarResult]:
    """Collect the filings index and recent Form 4 details for each security with a CIK."""
    today = today or dt.datetime.now(dt.UTC).date()
    since = today - dt.timedelta(days=lookback_days)
    form4_since = today - dt.timedelta(days=form4_lookback_days)
    forms = tuple(forms)
    results = []
    for sec in securities:
        res = EdgarResult(sec.symbol)
        results.append(res)
        if not sec.cik:
            res.error = "no CIK in watchlist"
            log.warning("%s: skipped, no CIK", sec.symbol)
            continue
        try:
            payload = client.get_json(SUBMISSIONS_URL.format(cik=sec.cik))
            res.filings = upsert_filings(
                con, parse_submissions(payload, sec.cik, sec.symbol, forms, since)
            )
        except Exception as exc:
            log.exception("%s: submissions fetch failed", sec.symbol)
            res.error = f"submissions: {exc}"
            continue

        for acc, doc_url, symbol, accepted in pending_form4(con, sec.cik, form4_since):
            doc = doc_url.rsplit("/", 1)[-1]
            try:
                xml_text = client.get_text(form4_xml_url(sec.cik, acc, doc))
                issuer = form4_issuer_cik(xml_text)
                txns = parse_form4(xml_text, acc, sec.cik, symbol, accepted)
            except Exception:
                log.exception("%s: Form 4 %s failed, will retry next run", sec.symbol, acc)
                continue
            if issuer == sec.cik:
                res.insider_rows += upsert_insider_transactions(con, txns)
            else:
                res.not_subject += 1  # the company is the reporting owner, not the issuer
            _mark_parsed(con, acc, issuer)
            res.form4_parsed += 1

        for acc in pending_stake_filings(con, sec.cik, form4_since):
            try:
                subject = parse_subject_cik(client.get_text(headers_url(sec.cik, acc)))
            except Exception:
                log.exception("%s: 13D/13G header %s failed, will retry next run", sec.symbol, acc)
                continue
            if subject != sec.cik:
                res.not_subject += 1
            _mark_parsed(con, acc, subject)
            res.stake_checked += 1
        log.info(
            "%s: %d filings, %d Form 4 parsed (%d transactions), %d 13D/13G checked, "
            "%d filed about other issuers",
            sec.symbol,
            res.filings,
            res.form4_parsed,
            res.insider_rows,
            res.stake_checked,
            res.not_subject,
        )
    return results
