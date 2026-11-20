import datetime as dt
import json
from pathlib import Path

import pytest

from market_intel.collectors import edgar
from market_intel.config import Security

FIXTURES = Path(__file__).parent / "fixtures"
SUBMISSIONS = json.loads((FIXTURES / "submissions_sample.json").read_text())
FORM4_XML = (FIXTURES / "form4_sample.xml").read_text()
HEADERS_AAPL = (FIXTURES / "headers_13g_subject_aapl.html").read_text()
HEADERS_GS = (FIXTURES / "headers_13g_filed_by_gs.html").read_text()
FORM4_URL = (
    "https://www.sec.gov/Archives/edgar/data/320193/000032019326000100/wk-form4_1763506882.xml"
)
HEADERS_URL = (
    "https://www.sec.gov/Archives/edgar/data/320193/000114036126000050/"
    "0001140361-26-000050-index-headers.html"
)
AAPL = Security("AAPL", "Apple Inc.", 320193, "XLK")
TODAY = dt.date(2026, 11, 20)


class FakeClient:
    def __init__(self, json_by_url=None, text_by_url=None):
        self.json_by_url = json_by_url or {}
        self.text_by_url = text_by_url or {}
        self.calls = []

    def get_json(self, url):
        self.calls.append(url)
        return self.json_by_url[url]

    def get_text(self, url):
        self.calls.append(url)
        return self.text_by_url[url]


def apple_client(form4_xml=FORM4_XML, headers=HEADERS_AAPL):
    return FakeClient(
        {"https://data.sec.gov/submissions/CIK0000320193.json": SUBMISSIONS},
        {FORM4_URL: form4_xml, HEADERS_URL: headers},
    )


def test_acceptance_time_is_already_utc():
    # Real NVDA 8-K 0001045810-26-000088: index page says "Accepted 2026-11-17 16:20:51"
    # (New York, EST), the submissions JSON says 21:20:51Z. The Z is honest.
    assert edgar.parse_acceptance("2026-11-17T21:20:51.000Z") == dt.datetime(
        2026, 11, 17, 21, 20, 51
    )
    assert edgar.parse_acceptance("2026-07-01T20:30:00.000Z") == dt.datetime(2026, 7, 1, 20, 30)
    assert edgar.parse_acceptance("") is None
    assert edgar.parse_acceptance("garbage") is None


def test_parse_submissions_filters_forms_and_dates():
    rows = edgar.parse_submissions(SUBMISSIONS, 320193, "AAPL", since=dt.date(2026, 1, 1))
    assert [r.accession_no for r in rows] == [
        "0000320193-26-000101",
        "0000320193-26-000100",
        "0001140361-26-000050",
        "0000320193-26-000090",
        "0000320193-26-000080",
    ]
    eight_k = rows[0]
    assert eight_k.form_type == "8-K" and eight_k.items == "5.02,9.01"
    assert eight_k.primary_doc_url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019326000101/aapl-20261119.htm"
    )
    assert rows[2].period_of_report is None  # empty reportDate
    # Own filings are about the company; Form 4 and 13G subjects are resolved later.
    assert [r.subject_cik for r in rows] == [320193, None, None, 320193, 320193]
    only_8k = edgar.parse_submissions(SUBMISSIONS, 320193, "AAPL", forms=["8-K"])
    assert {r.form_type for r in only_8k} == {"8-K"}


def test_parse_submissions_handles_empty_payload():
    assert edgar.parse_submissions({}, 1, "X") == []


def test_form4_xml_url_strips_xsl_directory():
    assert edgar.form4_xml_url(320193, "0000320193-26-000100", "xslF345X05/wk-form4_1.xml") == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019326000100/wk-form4_1.xml"
    )


def test_parse_form4_non_derivative_only():
    accepted = dt.datetime(2026, 11, 18, 23, 1, 22)
    txns = edgar.parse_form4(FORM4_XML, "acc-1", 320193, "AAPL", accepted)
    assert len(txns) == 2  # derivative RSU line skipped
    buy, tax = txns
    assert buy.line_no == 1 and tax.line_no == 2
    assert buy.insider_name == "Doe Jane"
    assert buy.insider_title == "Chief Financial Officer, Director"
    assert buy.transaction_code == "P" and buy.acquired_disposed == "A"
    assert (buy.shares, buy.price, buy.shares_after) == (1000.0, 230.5, 51000.0)
    assert buy.transaction_date == dt.date(2026, 11, 14)
    assert tax.transaction_code == "F" and tax.price is None
    assert buy.accepted_at == accepted


def test_collect_filings_end_to_end_and_idempotent(con):
    client = apple_client()
    [res] = edgar.collect_filings(con, [AAPL], client, today=TODAY)
    assert res.error is None
    assert (res.filings, res.form4_parsed, res.insider_rows) == (5, 1, 2)
    assert (res.stake_checked, res.not_subject) == (1, 0)
    # The old 2025 Form 4 is outside the 365-day window and never fetched.
    assert len(client.calls) == 3  # submissions, Form 4 XML, 13G header

    [res2] = edgar.collect_filings(con, [AAPL], client, today=TODAY)
    assert res2.form4_parsed == 0 and res2.stake_checked == 0  # not re-fetched
    assert len(client.calls) == 4  # only the submissions JSON again
    subjects = con.execute("SELECT DISTINCT subject_cik FROM filings").fetchall()
    assert subjects == [(320193,)]  # the re-run upsert kept the resolved subjects
    assert con.execute("SELECT count(*) FROM filings").fetchone() == (5,)
    assert con.execute("SELECT count(*) FROM insider_transactions").fetchone() == (2,)
    parsed = con.execute(
        "SELECT details_parsed_at IS NOT NULL FROM filings WHERE form_type = '4'"
    ).fetchone()
    assert parsed == (True,)


def test_parse_subject_cik_from_sec_header():
    assert edgar.parse_subject_cik(HEADERS_GS) == 1935172  # Xiao-I, not Goldman (886982)
    assert edgar.parse_subject_cik(HEADERS_AAPL) == 320193
    assert edgar.parse_subject_cik("<html>no header</html>") is None


def test_stake_filing_about_another_issuer_is_not_ours(con):
    client = apple_client(headers=HEADERS_GS)
    [res] = edgar.collect_filings(con, [AAPL], client, today=TODAY)
    assert (res.stake_checked, res.not_subject) == (1, 1)
    row = con.execute(
        "SELECT subject_cik, details_parsed_at IS NOT NULL FROM filings "
        "WHERE accession_no = '0001140361-26-000050'"
    ).fetchone()
    assert row == (1935172, True)


def test_form4_where_company_is_reporting_owner_is_skipped(con):
    other_issuer = FORM4_XML.replace(
        "<issuerCik>0000320193</issuerCik>", "<issuerCik>0000999999</issuerCik>"
    )
    [res] = edgar.collect_filings(con, [AAPL], apple_client(form4_xml=other_issuer), today=TODAY)
    assert (res.form4_parsed, res.insider_rows, res.not_subject) == (1, 0, 1)
    assert con.execute("SELECT count(*) FROM insider_transactions").fetchone() == (0,)
    assert con.execute("SELECT subject_cik FROM filings WHERE form_type = '4'").fetchone() == (
        999999,
    )


def test_form4_failure_is_retried_next_run(con):
    client = apple_client()
    good = dict(client.text_by_url)
    client.text_by_url = {HEADERS_URL: HEADERS_AAPL}  # Form 4 fetch raises KeyError
    [res] = edgar.collect_filings(con, [AAPL], client, today=TODAY)
    assert res.error is None and res.form4_parsed == 0
    client.text_by_url = good
    [res] = edgar.collect_filings(con, [AAPL], client, today=TODAY)
    assert res.form4_parsed == 1


def test_security_without_cik_is_skipped(con):
    client = FakeClient()
    [res] = edgar.collect_filings(con, [Security("ZZZ", "No CIK")], client, today=TODAY)
    assert res.error == "no CIK in watchlist" and client.calls == []


def test_submissions_failure_reported_per_symbol(con):
    gs = Security("GS", "Goldman", 886982)
    [aapl_res, gs_res] = edgar.collect_filings(con, [AAPL, gs], apple_client(), today=TODAY)
    assert aapl_res.error is None
    assert gs_res.error and gs_res.error.startswith("submissions")


def test_sec_client_requires_user_agent_with_email(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(edgar.MissingUserAgentError):
        edgar.SecClient()
    with pytest.raises(edgar.MissingUserAgentError):
        edgar.SecClient(user_agent="market-intel")
    client = edgar.SecClient(user_agent="market-intel me@example.com")
    assert client._session.headers["User-Agent"] == "market-intel me@example.com"


def test_sec_client_retries_on_429(monkeypatch):
    class Resp:
        def __init__(self, code):
            self.status_code = code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)

        def json(self):
            return {"ok": True}

    responses = [Resp(429), Resp(200)]
    client = edgar.SecClient(user_agent="t me@example.com", max_requests_per_second=1000)
    monkeypatch.setattr(client._session, "get", lambda url, timeout: responses.pop(0))
    monkeypatch.setattr(edgar.time, "sleep", lambda s: None)
    assert client.get_json("https://example.test") == {"ok": True}
    assert responses == []
