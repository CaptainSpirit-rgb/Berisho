"""Fetch the text of recent 8-K filings: the main document plus press-release exhibits.

An 8-K's item numbers only say *that* something happened ("2.02 results of operations").
What happened is in the filing itself and, for earnings, in exhibit 99.1 (the press
release). For each recent 8-K without stored text, this reads the filing's EDGAR index
page, fetches the main document and any EX-99.x exhibits, strips the HTML and upserts
the plain text into ``filing_documents``. Idempotent: filings already fetched are skipped.
"""

from __future__ import annotations

import datetime as dt
import html
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser

import duckdb

from market_intel.collectors.edgar import HttpClient

log = logging.getLogger(__name__)

SEC_BASE = "https://www.sec.gov"
INDEX_URL = SEC_BASE + "/Archives/edgar/data/{cik}/{acc_nodash}/{acc}-index.htm"
TEXT_EXTENSIONS = (".htm", ".html", ".txt")


def index_url(cik: int, accession_no: str) -> str:
    return INDEX_URL.format(cik=cik, acc_nodash=accession_no.replace("-", ""), acc=accession_no)


@dataclass(frozen=True)
class IndexDoc:
    name: str
    url: str
    doc_type: str
    description: str


_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_HREF = re.compile(r'href="([^"]+)"', re.I)
_TAG = re.compile(r"<[^>]+>")


def _cell_text(cell: str) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", cell)).split())


def parse_index(page: str) -> list[IndexDoc]:
    """Documents listed in a filing's ``-index.htm`` page (Seq, Description, Document,
    Type, Size). The iXBRL viewer prefix (``/ix?doc=``) is removed from links."""
    docs = []
    for row in _ROW.findall(page):
        cells = _CELL.findall(row)
        if len(cells) < 4:
            continue
        m = _HREF.search(cells[2])
        if not m:
            continue
        href = m.group(1).replace("/ix?doc=", "")
        url = href if href.startswith("http") else SEC_BASE + href
        docs.append(
            IndexDoc(
                name=url.rsplit("/", 1)[-1],
                url=url,
                doc_type=_cell_text(cells[3]),
                description=_cell_text(cells[1]),
            )
        )
    return docs


def wanted_documents(docs: list[IndexDoc], form_type: str) -> list[IndexDoc]:
    """The main document (type = the form type) and press-release exhibits (EX-99.x)."""
    base = form_type.split("/")[0]
    keep = [
        d
        for d in docs
        if d.name.lower().endswith(TEXT_EXTENSIONS)
        and (d.doc_type.split("/")[0] == base or d.doc_type.upper().startswith("EX-99"))
    ]
    return keep


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"}
    SKIP = {"script", "style", "ix:header", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(page: str) -> str:
    """Readable plain text from filing HTML: tags dropped, whitespace collapsed, one
    paragraph per line."""
    parser = _TextExtractor()
    parser.feed(page)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


def pending_filings(
    con: duckdb.DuckDBPyConnection, symbols: list[str], since: dt.datetime
) -> list[tuple[str, int, str]]:
    """Recent 8-Ks about watchlist companies with no stored text yet:
    (accession_no, cik, form_type)."""
    return con.execute(
        """
        SELECT f.accession_no, f.cik, f.form_type FROM filings f
        WHERE f.symbol IN (SELECT unnest(?))
          AND f.form_type IN ('8-K', '8-K/A')
          AND f.subject_cik = f.cik
          AND coalesce(f.accepted_at, f.filed_date::TIMESTAMP) >= ?
          AND NOT EXISTS (SELECT 1 FROM filing_documents d WHERE d.accession_no = f.accession_no)
        ORDER BY f.accepted_at
        """,
        [symbols, since],
    ).fetchall()


def collect_filing_text(
    con: duckdb.DuckDBPyConnection,
    client: HttpClient,
    symbols: list[str],
    since: dt.datetime,
) -> tuple[int, list[str]]:
    """Fetch and store text for pending 8-Ks. Returns (filings done, accessions failed).
    A failed filing stores nothing, so it is retried on the next run."""
    done, failed = 0, []
    for acc, cik, form in pending_filings(con, symbols, since):
        try:
            docs = wanted_documents(parse_index(client.get_text(index_url(cik, acc))), form)
            rows = [
                (
                    acc,
                    d.name,
                    d.doc_type,
                    d.description,
                    d.url,
                    html_to_text(client.get_text(d.url)),
                )
                for d in docs
            ]
        except Exception as exc:  # network or parse error: keep going, retry next run
            log.warning("Filing text for %s failed: %s", acc, exc)
            failed.append(acc)
            continue
        if not rows:  # nothing usable; store a marker so we don't refetch every run
            rows = [(acc, "-", None, "no text documents found", index_url(cik, acc), "")]
        con.executemany(
            "INSERT OR REPLACE INTO filing_documents "
            "(accession_no, doc_name, doc_type, description, url, text) VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        done += 1
    return done, failed


def documents_for(con: duckdb.DuckDBPyConnection, accession_no: str) -> list[dict]:
    rows = con.execute(
        """
        SELECT doc_name, doc_type, description, url, text FROM filing_documents
        WHERE accession_no = ? AND text <> ''
        ORDER BY CASE WHEN doc_type LIKE 'EX-99%' THEN 0 ELSE 1 END, doc_type, doc_name
        """,
        [accession_no],
    ).fetchall()
    keys = ("doc_name", "doc_type", "description", "url", "text")
    return [dict(zip(keys, r, strict=True)) for r in rows]
