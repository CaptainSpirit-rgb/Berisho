"""Screener universe.

Default: the **S&P 500**, from the constituents table on Wikipedia's "List of S&P 500
companies" page (free; gives ticker, name, GICS sector and sub-industry, and CIK).
S&P's own list isn't free. Each refresh replaces the universe, so companies that left
the index drop out.

Alternative (``screener.universe: all_listed``): every common stock on NYSE or Nasdaq
that files with the SEC, from SEC's ``company_tickers_exchange.json`` (one free
download listing CIK, name, ticker and exchange per registrant). One row per company:
the first plain ticker SEC lists for it, skipping warrants, units and rights.

For that alternative, industry (SIC code) comes from each company's submissions JSON
and is fetched only for companies that don't have it yet.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

import duckdb

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
# Wikipedia asks automated clients for a descriptive User-Agent.
WIKI_USER_AGENT = "market-intel/0.1 (personal research tool; python-requests)"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

DEFAULT_EXCHANGES = ("NYSE", "Nasdaq")

# Plain tickers, optionally with a share-class suffix (BRK-B). Five-letter Nasdaq
# tickers ending in W, U or R are warrants, units and rights.
_PLAIN = re.compile(r"^[A-Z]{1,5}(-[A-Z])?$")
_DERIVATIVE_5TH = re.compile(r"^[A-Z]{4}[WUR]$")


@dataclass(frozen=True)
class Listing:
    cik: int
    ticker: str
    name: str
    exchange: str | None
    sector: str | None = None
    sub_industry: str | None = None


class _TableParser(HTMLParser):
    """Collect the rows of the first <table id="constituents"> as lists of cell text."""

    def __init__(self, table_id: str) -> None:
        super().__init__()
        self.table_id = table_id
        self.depth = 0  # nesting depth inside the wanted table, 0 = outside
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self.done = False

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if tag == "table":
            if self.depth:
                self.depth += 1
            elif dict(attrs).get("id") == self.table_id:
                self.depth = 1
            return
        if self.depth != 1:
            return
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if self.done or not self.depth:
            return
        if tag == "table":
            self.depth -= 1
            if self.depth == 0:
                self.done = True
            return
        if self.depth != 1:
            return
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self.depth == 1 and self._cell is not None:
            self._cell.append(data)


def yahoo_ticker(symbol: str) -> str:
    """Wikipedia/S&P write class shares as BRK.B; Yahoo and SEC use BRK-B."""
    return symbol.strip().upper().replace(".", "-")


def parse_sp500_wikipedia(html: str) -> list[Listing]:
    parser = _TableParser("constituents")
    parser.feed(html)
    if not parser.rows:
        raise ValueError("S&P 500 constituents table not found on the page")
    header = [h.lower() for h in parser.rows[0]]

    def col(*names: str) -> int:
        for i, h in enumerate(header):
            if any(n in h for n in names):
                return i
        raise ValueError(f"Column {names} missing from the S&P 500 table: {header}")

    i_sym, i_name, i_cik = col("symbol"), col("security"), col("cik")
    i_sector, i_sub = col("gics sector"), col("sub-industry", "sub industry")
    seen: set[int] = set()
    out: list[Listing] = []
    for row in parser.rows[1:]:
        try:
            cik = int(row[i_cik])
        except (IndexError, ValueError):
            continue
        if cik in seen:  # GOOGL/GOOG, FOX/FOXA, NWS/NWSA: keep the first class listed
            continue
        seen.add(cik)
        out.append(
            Listing(
                cik=cik,
                ticker=yahoo_ticker(row[i_sym]),
                name=row[i_name],
                exchange=None,
                sector=row[i_sector] or None,
                sub_industry=row[i_sub] or None,
            )
        )
    if len(out) < 400:  # the page layout changed or the fetch was cut short
        raise ValueError(f"Only {len(out)} S&P 500 members parsed; expected about 500")
    return out


def fetch_sp500_html(timeout: float = 30) -> str:
    import requests

    resp = requests.get(SP500_URL, headers={"User-Agent": WIKI_USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def replace_universe(con: duckdb.DuckDBPyConnection, listings: Iterable[Listing]) -> int:
    """Make the universe exactly ``listings`` (members that left the index drop out)."""
    listings = list(listings)
    con.execute("DELETE FROM screen_universe")
    con.executemany(
        """
        INSERT INTO screen_universe
            (cik, ticker, name, exchange, sector, sub_industry, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, current_timestamp)
        """,
        [(x.cik, x.ticker, x.name, x.exchange, x.sector, x.sub_industry) for x in listings],
    )
    return len(listings)


def refresh_sp500(con: duckdb.DuckDBPyConnection, html: str | None = None) -> int:
    return replace_universe(con, parse_sp500_wikipedia(html or fetch_sp500_html()))


def is_common_ticker(ticker: str) -> bool:
    return bool(_PLAIN.match(ticker)) and not _DERIVATIVE_5TH.match(ticker)


def parse_tickers_exchange(
    data: dict[str, Any], exchanges: Iterable[str] = DEFAULT_EXCHANGES
) -> list[Listing]:
    fields = data.get("fields") or []
    idx = {name: i for i, name in enumerate(fields)}
    allowed = set(exchanges)
    seen: set[int] = set()
    out: list[Listing] = []
    for row in data.get("data") or []:
        try:
            cik = int(row[idx["cik"]])
            ticker = str(row[idx["ticker"]] or "").upper().strip()
            exchange = row[idx["exchange"]]
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if cik in seen or exchange not in allowed or not is_common_ticker(ticker):
            continue
        seen.add(cik)
        out.append(Listing(cik, ticker, str(row[idx["name"]] or ""), exchange))
    return out


def upsert_universe(con: duckdb.DuckDBPyConnection, listings: Iterable[Listing]) -> int:
    """Insert/refresh listings, keeping any SIC already fetched."""
    rows = [(x.cik, x.ticker, x.name, x.exchange) for x in listings]
    if rows:
        con.executemany(
            """
            INSERT INTO screen_universe (cik, ticker, name, exchange, updated_at)
            VALUES (?, ?, ?, ?, current_timestamp)
            ON CONFLICT (cik) DO UPDATE SET
                ticker = excluded.ticker, name = excluded.name,
                exchange = excluded.exchange, updated_at = excluded.updated_at
            """,
            rows,
        )
    return len(rows)


def missing_sic(con: duckdb.DuckDBPyConnection, ciks: Iterable[int] | None = None) -> list[int]:
    rows = con.execute("SELECT cik FROM screen_universe WHERE sic IS NULL ORDER BY cik").fetchall()
    found = [r[0] for r in rows]
    if ciks is None:
        return found
    wanted = set(ciks)
    return [c for c in found if c in wanted]


def parse_sic(submissions: dict[str, Any]) -> tuple[int | None, str | None]:
    raw = submissions.get("sic")
    try:
        sic = int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        sic = None
    return sic, submissions.get("sicDescription") or None


def fill_sic(con: duckdb.DuckDBPyConnection, client, ciks: Iterable[int]) -> int:
    """Fetch SIC codes for ``ciks``. One failure doesn't stop the rest."""
    n = 0
    for cik in ciks:
        try:
            sic, desc = parse_sic(client.get_json(SUBMISSIONS_URL.format(cik=cik)))
        except Exception as exc:  # noqa: BLE001 - keep going, log and move on
            log.warning("SIC lookup failed for CIK %s: %s", cik, exc)
            continue
        # 0 marks "looked up, none given" so we don't ask again every week.
        con.execute(
            "UPDATE screen_universe SET sic = ?, sic_description = ? WHERE cik = ?",
            [sic if sic is not None else 0, desc, cik],
        )
        n += 1
        if n % 250 == 0:
            log.info("SIC codes: %d fetched", n)
    return n


def refresh(con: duckdb.DuckDBPyConnection, client, exchanges=DEFAULT_EXCHANGES) -> int:
    listings = parse_tickers_exchange(client.get_json(TICKERS_URL), exchanges)
    return upsert_universe(con, listings)
