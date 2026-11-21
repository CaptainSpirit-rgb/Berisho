import datetime as dt
import json
import zipfile

import pytest
from screener_helpers import companyfacts, flat_prices

from market_intel import screener
from market_intel.collectors.prices import upsert_bars
from market_intel.screener import facts, metrics, prices, screen, universe

AS_OF = dt.date(2025, 11, 20)  # Q3 2025 10-Qs (filed ~Nov 9) are known
SIZED = {"screener": {"min_market_cap": 300e6, "max_market_cap": 10e9}}


@pytest.fixture
def scon(con):
    screener.init_schema(con)
    return con


def load(con, doc):
    return facts.upsert_facts(con, facts.parse_companyfacts(doc))


def add_company(con, cik, ticker, sic=3570, price=10.0, sic_desc="Computers", **kw):
    load(con, companyfacts(cik, [2022, 2023, 2024, 2025], **kw))
    con.execute(
        "INSERT INTO screen_universe (cik, ticker, name, exchange, sic, sic_description) "
        "VALUES (?, ?, ?, 'NYSE', ?, ?)",
        [cik, ticker, f"{ticker} Inc", sic, sic_desc],
    )
    upsert_bars(con, flat_prices(ticker, AS_OF - dt.timedelta(days=10), AS_OF, price), "test")


def growing(base, g):
    """Quarterly revenue growing g per year."""
    return lambda y, q: base * (1 + g) ** ((y - 2022) + (q - 1) / 4)


# ------------------------------------------------------------------ universe
def test_universe_keeps_one_plain_ticker_per_company_on_allowed_exchanges():
    data = {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [
            [1, "Alpha", "ALPH", "Nasdaq"],
            [1, "Alpha", "ALPHW", "Nasdaq"],  # duplicate CIK
            [2, "Beta Warrants", "BETAW", "Nasdaq"],  # warrant
            [3, "Gamma", "GAM", "OTC"],  # wrong exchange
            [4, "Delta B", "DLT-B", "NYSE"],  # share class is fine
            [5, "Eps Units", "EPSIU", "Nasdaq"],  # unit
            [6, "Zeta Pref", "ZET-PA", "NYSE"],  # preferred
        ],
    }
    got = universe.parse_tickers_exchange(data)
    assert [(x.cik, x.ticker) for x in got] == [(1, "ALPH"), (4, "DLT-B")]


def test_universe_upsert_keeps_sic(scon):
    universe.upsert_universe(scon, [universe.Listing(1, "AAA", "A", "NYSE")])
    scon.execute("UPDATE screen_universe SET sic = 3570 WHERE cik = 1")
    universe.upsert_universe(scon, [universe.Listing(1, "AAB", "A new", "NYSE")])
    assert scon.execute("SELECT ticker, sic FROM screen_universe").fetchall() == [("AAB", 3570)]


def test_fill_sic_marks_missing_as_zero_and_survives_errors(scon):
    universe.upsert_universe(scon, [universe.Listing(c, f"T{c}", "", "NYSE") for c in (1, 2, 3)])

    class Client:
        def get_json(self, url):
            if "0000000002" in url:
                raise ConnectionError("boom")
            if "0000000003" in url:
                return {"sic": ""}
            return {"sic": "7372", "sicDescription": "Prepackaged software"}

    assert universe.fill_sic(scon, Client(), universe.missing_sic(scon)) == 2
    rows = scon.execute("SELECT cik, sic FROM screen_universe ORDER BY cik").fetchall()
    assert rows == [(1, 7372), (2, None), (3, 0)]
    assert universe.missing_sic(scon) == [2]


# ------------------------------------------------------------------ facts
def test_parse_companyfacts_keeps_known_concepts_and_report_forms():
    doc = companyfacts(42, [2024], growing(100e6, 0.1))
    doc["facts"]["us-gaap"]["Revenues"]["units"]["USD"].append(
        {
            "end": "2024-03-31",
            "start": "2024-01-01",
            "val": 1,
            "accn": "x",
            "form": "8-K",
            "filed": "2024-04-02",
        }
    )
    doc["facts"]["us-gaap"]["SomethingElse"] = {
        "units": {
            "USD": [
                {"end": "2024-03-31", "val": 1, "accn": "y", "form": "10-Q", "filed": "2024-05-01"}
            ]
        }
    }
    rows = facts.parse_companyfacts(doc)
    assert {r.form_type for r in rows} <= {"10-Q", "10-K"}
    assert "SomethingElse" not in {r.concept for r in rows}
    q1 = [r for r in rows if r.metric == "revenue" and r.duration_days < 100]
    assert q1 and all(80 <= r.duration_days <= 100 for r in q1)
    shares = [r for r in rows if r.metric == "shares"]
    assert shares and all(r.duration_days == 0 for r in shares)


def test_upsert_is_idempotent(scon):
    doc = companyfacts(42, [2024], growing(100e6, 0.1))
    n = load(scon, doc)
    load(scon, doc)
    assert scon.execute("SELECT count(*) FROM xbrl_facts").fetchone()[0] == n


def test_bulk_zip_ingest_filters_ciks(scon, tmp_path):
    path = tmp_path / "companyfacts.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for cik in (1, 2):
            zf.writestr(
                f"CIK{cik:010d}.json", json.dumps(companyfacts(cik, [2024], growing(1e8, 0)))
            )
        zf.writestr("readme.txt", "x")
    companies, rows = facts.ingest_bulk_zip(scon, path, {2})
    assert companies == 1 and rows > 0
    assert scon.execute("SELECT DISTINCT cik FROM xbrl_facts").fetchall() == [(2,)]


# ------------------------------------------------------------------ metrics
def test_ttm_derives_q4_and_growth(scon):
    rev = growing(100e6, 0.20)
    load(scon, companyfacts(7, [2023, 2024, 2025], rev))
    m = metrics.company_metrics(scon, AS_OF)[7]
    assert m["period_end"] == dt.date(2025, 9, 30)
    expected_ttm = sum(rev(y, q) for y, q in [(2024, 4), (2025, 1), (2025, 2), (2025, 3)])
    assert m["revenue_ttm"] == pytest.approx(expected_ttm)  # uses a derived Q4 2024
    assert m["revenue_growth"] == pytest.approx(0.20, abs=1e-6)
    assert m["quarter_growth"] == pytest.approx(0.20, abs=1e-6)
    assert m["growth_acceleration"] == pytest.approx(0.0, abs=1e-6)
    assert m["operating_margin"] == pytest.approx(0.15)
    # cash flow is year-to-date only: TTM = FY2024 + 9M 2025 - 9M 2024
    assert m["fcf_ttm"] == pytest.approx(expected_ttm * (0.12 - 0.03))
    assert m["net_debt"] == pytest.approx(20e6 - 50e6)
    assert m["shares"] == 100e6


def test_point_in_time_ignores_later_filings(scon):
    load(scon, companyfacts(7, [2023, 2024, 2025], growing(100e6, 0.2)))
    # Before the Q3 2025 10-Q was filed (Nov 9), the latest known quarter is Q2.
    m = metrics.company_metrics(scon, dt.date(2025, 11, 1))[7]
    assert m["period_end"] == dt.date(2025, 6, 30)
    # A restatement filed later doesn't leak back in time.
    scon.execute(
        "INSERT INTO xbrl_facts (cik, metric, concept, period_start, period_end, duration_days,"
        " value, form_type, accession_no, filed_date) VALUES (7, 'revenue', 'Revenues',"
        " '2025-04-01', '2025-06-30', 91, 1, '10-Q/A', 'restated', '2025-12-15')"
    )
    before = metrics.company_metrics(scon, dt.date(2025, 12, 10))[7]
    after = metrics.company_metrics(scon, dt.date(2025, 12, 20))[7]
    assert before["quarter_growth_prev"] == pytest.approx(0.2, abs=1e-6)
    assert after["quarter_growth_prev"] < -0.99  # the restated Q2 now counts


def test_stale_companies_are_dropped(scon):
    load(scon, companyfacts(7, [2022, 2023], growing(100e6, 0.2)))
    assert 7 not in metrics.company_metrics(scon, AS_OF, max_staleness_days=200)


def test_growth_acceleration_detected(scon):
    def rev(y, q):  # flat until 2025 Q2, then jumps
        base = 100e6
        return base * (1.5 if (y, q) >= (2025, 3) else 1.0)

    load(scon, companyfacts(7, [2023, 2024, 2025], rev))
    m = metrics.company_metrics(scon, AS_OF)[7]
    assert m["quarter_growth"] == pytest.approx(0.5)
    assert m["growth_acceleration"] == pytest.approx(0.5)


def test_concept_used_for_latest_period_is_preferred(scon):
    # Old periods tagged SalesRevenueNet, recent ones with the ASC 606 tag.
    old = companyfacts(7, [2022, 2023], growing(100e6, 0.1), revenue_concept="SalesRevenueNet")
    new = companyfacts(
        7,
        [2024, 2025],
        growing(100e6, 0.1),
        revenue_concept="RevenueFromContractWithCustomerExcludingAssessedTax",
    )
    load(scon, old)
    load(scon, new)
    m = metrics.company_metrics(scon, AS_OF)[7]
    assert m["revenue_growth"] == pytest.approx(0.1, abs=1e-6)


# ------------------------------------------------------------------ screen
def test_percentile_ranks_handles_ties_and_direction():
    got = screen.percentile_ranks({"a": 1, "b": 2, "c": 2, "d": 3, "e": None}, True)
    assert got == {"a": 0.0, "b": 0.5, "c": 0.5, "d": 1.0}
    assert screen.percentile_ranks({"a": 1, "b": 3}, False) == {"a": 1.0, "b": 0.0}


def build_universe(con):
    # Strong: fast growth, rising margin, net cash, cheap.
    add_company(
        con,
        1,
        "GOOD",
        revenue=growing(100e6, 0.40),
        op_margin=lambda y, q: 0.14 + 0.01 * (y - 2022),
        price=12.0,
    )
    add_company(con, 2, "SLOW", revenue=growing(100e6, 0.02), price=30.0, debt=300e6)
    add_company(con, 3, "MID", revenue=growing(100e6, 0.10), price=20.0)
    add_company(con, 4, "BANK", sic=6022, revenue=growing(100e6, 0.3), price=15.0)
    add_company(con, 5, "HUGE", revenue=growing(100e6, 0.3), price=500.0)  # $50B
    add_company(con, 6, "LOSS", revenue=growing(100e6, 0.3), op_margin=-0.1, price=10.0)
    add_company(con, 7, "TINY", revenue=growing(100e6, 0.3), price=1.0)  # $100M
    con.execute(
        "INSERT INTO screen_universe (cik, ticker, name, exchange) VALUES (8, 'NOFIN', 'x', 'NYSE')"
    )


def test_screen_filters_ranks_and_explains(scon):
    build_universe(scon)
    res = screen.run_screen(scon, AS_OF, SIZED)
    assert [c.ticker for c in res.candidates][0] == "GOOD"
    assert {c.ticker for c in res.candidates} == {"GOOD", "SLOW", "MID"}
    assert res.excluded == {
        "financials/REITs (different accounts)": 1,
        "outside the market-cap range": 2,
        "not profitable (operating loss)": 1,
        "no recent financials": 1,
    }
    good = res.candidates[0]
    assert good.metrics["market_cap"] == pytest.approx(1.2e9)
    assert any(r.startswith("Sales +40%") for r in good.reasons)
    assert any(r.startswith("Net cash") for r in good.reasons)
    assert any("pts vs a year ago" in r for r in good.reasons)
    slow = next(c for c in res.candidates if c.ticker == "SLOW")
    assert slow.pillars["balance"] < good.pillars["balance"]


def test_screen_respects_config_overrides(scon):
    build_universe(scon)
    res = screen.run_screen(
        scon,
        AS_OF,
        {"screener": {"require_positive_operating_income": False, "max_market_cap": 1e12}},
    )
    assert {"LOSS", "HUGE"} <= {c.ticker for c in res.candidates}


def test_save_render_and_new_this_week(scon):
    build_universe(scon)
    first = screen.run_screen(scon, AS_OF - dt.timedelta(days=7), SIZED)
    screen.save_results(scon, first, top_n=1)
    res = screen.run_screen(scon, AS_OF, SIZED)
    prev = screen.previous_tickers(scon, AS_OF)
    assert prev == {first.candidates[0].ticker}
    screen.save_results(scon, res, top_n=3)
    screen.save_results(scon, res, top_n=3)  # re-run same day replaces
    assert (
        scon.execute("SELECT count(*) FROM screen_results WHERE run_date = ?", [AS_OF]).fetchone()[
            0
        ]
        == 3
    )
    text = screen.render_section(res, 3, previous={"GOOD"}, watchlist={"MID"})
    assert "NEW CANDIDATES" in text
    assert "1. GOOD GOOD Inc ($1.2B, Computers)" in text
    assert "MID MID Inc" in text and "on your watchlist" in text
    assert "new this week" in text  # SLOW and MID weren't in previous
    assert "Not financial advice" in text


def test_render_with_no_candidates():
    res = screen.ScreenResult(AS_OF, [], {}, 0)
    assert "No company passed" in screen.render_section(res, 10)


# ------------------------------------------------------------------ bulk prices
def test_bulk_prices_chunks_and_reports_missing(scon):
    calls = []

    def downloader(tickers, start, end):
        calls.append(list(tickers))
        if "BAD" in tickers:
            raise ConnectionError("boom")
        return [b for t in tickers if t != "NONE" for b in flat_prices(t, start, start, 5.0)]

    rows, missing = prices.collect(
        scon,
        ["a", "b", "NONE", "BAD", "c"],
        dt.date(2025, 11, 17),
        dt.date(2025, 11, 18),
        downloader=downloader,
        chunk_size=3,
    )
    assert calls == [["A", "B", "NONE"], ["BAD", "C"]]
    assert rows == 2 and missing == ["NONE", "BAD", "C"]


def test_cli_screen_dry_run_prints_candidates(tmp_path, capsys):
    from market_intel import cli
    from market_intel.storage import db as dbmod

    path = tmp_path / "t.duckdb"
    con = dbmod.connect(path)
    dbmod.init_schema(con)
    screener.init_schema(con)
    build_universe(con)
    con.close()
    assert cli.main(["screen", "--dry-run", "--db", str(path), "--date", AS_OF.isoformat()]) == 0
    out = capsys.readouterr().out
    assert "NEW CANDIDATES" in out and "GOOD" in out
    con = dbmod.connect(path)
    # Default settings: no size filter, so HUGE and TINY pass too.
    assert con.execute("SELECT count(*) FROM screen_results").fetchone()[0] == 5
    assert "MARKET (weekly)" in out


def test_default_settings_have_no_size_filter_and_exclude_by_sector(scon):
    build_universe(scon)
    scon.execute("UPDATE screen_universe SET sector = 'Information Technology'")
    scon.execute("UPDATE screen_universe SET sector = 'Financials' WHERE ticker = 'MID'")
    res = screen.run_screen(scon, AS_OF, {})
    tickers = {c.ticker for c in res.candidates}
    assert {"HUGE", "TINY", "GOOD", "SLOW"} <= tickers
    assert "MID" not in tickers and "BANK" not in tickers
    assert res.candidates[0].industry == "Information Technology"
    text = screen.render_section(res, 2)
    assert "Information Technology" in text
