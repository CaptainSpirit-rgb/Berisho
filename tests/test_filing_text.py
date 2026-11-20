import datetime as dt
from pathlib import Path

from market_intel.collectors import filing_text

INDEX = (Path(__file__).parent / "fixtures" / "index_8k_sample.htm").read_text()
ACC = "0001045810-26-000088"
BASE = "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000088/"


def add_8k(con, acc=ACC, accepted=dt.datetime(2026, 11, 17, 21, 20)):
    con.execute(
        "INSERT INTO filings (accession_no, cik, symbol, form_type, filed_date, accepted_at, "
        "items, subject_cik) VALUES (?, 1045810, 'NVDA', '8-K', ?, ?, '2.02,9.01', 1045810)",
        [acc, accepted.date(), accepted],
    )


class FakeSec:
    def __init__(self, pages, fail=()):
        self.pages, self.fail, self.calls = pages, set(fail), []

    def get_json(self, url):
        raise AssertionError("not used")

    def get_text(self, url):
        self.calls.append(url)
        if url in self.fail:
            raise RuntimeError("boom")
        return self.pages[url]


def pages():
    return {
        filing_text.index_url(1045810, ACC): INDEX,
        BASE + "nvda-20261117.htm": "<html><div style='display:none'><ix:header>HIDDEN"
        "</ix:header></div><p>Item 2.02 Results of Operations</p></html>",
        BASE + "q3fy27pr.htm": "<p>Revenue was <b>$1.0 billion</b>,</p><p>down&nbsp;5%.</p>"
        "<script>x()</script><table><tr><td>Q3</td><td>1.0</td></tr></table>",
        BASE + "q3fy27cfo.htm": "<p>CFO commentary</p>",
    }


def test_parse_index_and_wanted_documents():
    docs = filing_text.parse_index(INDEX)
    assert [d.doc_type for d in docs] == ["8-K", "EX-99.1", "EX-99.2", "GRAPHIC", "EX-101.SCH"]
    assert docs[0].url == BASE + "nvda-20261117.htm"  # iXBRL viewer prefix removed
    wanted = filing_text.wanted_documents(docs, "8-K")
    assert [d.name for d in wanted] == ["nvda-20261117.htm", "q3fy27pr.htm", "q3fy27cfo.htm"]


def test_html_to_text():
    text = filing_text.html_to_text(pages()[BASE + "q3fy27pr.htm"])
    assert text == "Revenue was $1.0 billion,\ndown 5%.\nQ3 1.0"
    hidden = filing_text.html_to_text(pages()[BASE + "nvda-20261117.htm"])
    assert "HIDDEN" not in hidden and "Item 2.02" in hidden


def test_collect_is_idempotent_and_press_release_first(con):
    add_8k(con)
    add_8k(con, "old-1", dt.datetime(2026, 10, 1))  # outside the window
    sec = FakeSec(pages())
    since = dt.datetime(2026, 11, 13)
    assert filing_text.collect_filing_text(con, sec, ["NVDA"], since) == (1, [])
    n_calls = len(sec.calls)
    assert filing_text.collect_filing_text(con, sec, ["NVDA"], since) == (0, [])
    assert len(sec.calls) == n_calls  # nothing refetched
    docs = filing_text.documents_for(con, ACC)
    assert [d["doc_type"] for d in docs] == ["EX-99.1", "EX-99.2", "8-K"]
    assert "down 5%" in docs[0]["text"]


def test_failed_filing_is_retried_next_run(con):
    add_8k(con)
    since = dt.datetime(2026, 11, 13)
    bad = FakeSec(pages(), fail={BASE + "q3fy27pr.htm"})
    assert filing_text.collect_filing_text(con, bad, ["NVDA"], since) == (0, [ACC])
    assert filing_text.documents_for(con, ACC) == []
    assert filing_text.collect_filing_text(con, FakeSec(pages()), ["NVDA"], since) == (1, [])
