"""Screener universe: every common stock on NYSE or Nasdaq that files with the SEC.

Source: SEC's ``company_tickers_exchange.json`` (one free download listing CIK, name,
ticker and exchange for every registrant with a ticker). One row per company is kept:
the first plain ticker SEC lists for it, skipping warrants, units and rights.

Industry (SIC code) comes from each company's submissions JSON and is fetched only for
companies that don't have it yet, because it rarely changes.

Russell 2000 / S&P 400 membership lists aren't free, so the "small and mid cap"
universe is defined by market cap instead (see ``screen``), which is close to what
those indexes hold.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import duckdb

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
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
    exchange: str


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
