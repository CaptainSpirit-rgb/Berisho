import datetime as dt

import pytest
from screener_helpers import companyfacts, flat_prices

from market_intel import screener
from market_intel.screener import facts, market, prices, universe

HEADER = (
    "<tr><th>Symbol</th><th>Security</th><th>GICS Sector</th><th>GICS Sub-Industry</th>"
    "<th>Headquarters Location</th><th>Date added</th><th>CIK</th><th>Founded</th></tr>"
)


def row(sym, name, sector, sub, cik):
    return (
        f'<tr><td><a href="/x">{sym}</a></td><td><a href="/y">{name}</a></td>'
        f"<td>{sector}</td><td>{sub}</td><td>Somewhere</td><td>2000-01-01</td>"
        f"<td>{cik:010d}</td><td>1900<sup>[1]</sup></td></tr>"
    )


def page(extra_rows=450):
    rows = [
        row("AAPL", "Apple Inc.", "Information Technology", "Technology Hardware", 320193),
        row("BRK.B", "Berkshire Hathaway", "Financials", "Multi-Sector Holdings", 1067983),
        row("GOOGL", "Alphabet Inc. (Class A)", "Communication Services", "Interactive", 1652044),
        row("GOOG", "Alphabet Inc. (Class C)", "Communication Services", "Interactive", 1652044),
    ]
    rows += [
        row(f"T{i}", f"Co {i}", "Industrials", "Machinery", 10_000 + i) for i in range(extra_rows)
    ]
    other = "<table id='other'><tr><th>Date</th></tr><tr><td>x</td></tr></table>"
    return (
        f"<html><body>{other}<table class='wikitable' id='constituents'><tbody>"
        f"{HEADER}{''.join(rows)}</tbody></table>"
        "<table id='changes'><tr><td>9999</td></tr></table></body></html>"
    )


@pytest.fixture
def scon(con):
    screener.init_schema(con)
    return con


def test_parse_sp500_wikipedia():
    got = universe.parse_sp500_wikipedia(page())
    assert len(got) == 453  # GOOG dropped as a second class of the same company
    first = {x.ticker: x for x in got[:3]}
    assert first["AAPL"].cik == 320193
    assert first["AAPL"].sector == "Information Technology"
    assert first["AAPL"].name == "Apple Inc."
    assert "BRK-B" in first  # Yahoo-style class ticker
    assert first["GOOGL"].sub_industry == "Interactive"


def test_parse_sp500_rejects_broken_page():
    with pytest.raises(ValueError, match="not found"):
        universe.parse_sp500_wikipedia("<html>nothing here</html>")
    with pytest.raises(ValueError, match="expected about 500"):
        universe.parse_sp500_wikipedia(page(extra_rows=10))


def test_refresh_replaces_universe(scon):
    universe.upsert_universe(scon, [universe.Listing(1, "OLD", "Left the index", "NYSE")])
    n = universe.refresh_sp500(scon, html=page())
    assert n == 453
    assert scon.execute("SELECT count(*) FROM screen_universe WHERE cik = 1").fetchone()[0] == 0
    assert (
        scon.execute("SELECT sector FROM screen_universe WHERE ticker = 'AAPL'").fetchone()[0]
        == "Information Technology"
    )


def test_fetch_companies_continues_after_failures(scon):
    class Client:
        def get_json(self, url):
            if "0000000002" in url:
                raise ConnectionError("boom")
            cik = int(url.split("CIK")[1][:10])
            return companyfacts(cik, [2024], lambda y, q: 1e8)

    rows, failed = facts.fetch_companies(scon, Client(), [1, 2, 3])
    assert failed == [2] and rows > 0
    assert {r[0] for r in scon.execute("SELECT DISTINCT cik FROM xbrl_facts").fetchall()} == {1, 3}


def test_market_overview_returns_and_trend(scon):
    as_of = dt.date(2025, 11, 20)
    start = dt.date(2024, 9, 1)
    prices.upsert_bars_bulk(scon, flat_prices("SPY", start, as_of, 100, 0.001), "t")
    prices.upsert_bars_bulk(scon, flat_prices("XLE", start, as_of, 100, -0.001), "t")
    prices.upsert_bars_bulk(scon, flat_prices("XLK", start, as_of, 100, 0.002), "t")
    settings = {
        "screener": {
            "market": {
                "indexes": [{"symbol": "SPY", "name": "S&P 500"}, "QQQ"],
                "sectors": [{"symbol": "XLE", "name": "Energy"}, {"symbol": "XLK", "name": "Tech"}],
            }
        }
    }
    indexes, sectors = market.overview(scon, as_of, settings)
    spy = indexes[0]
    assert spy.ret_1w == pytest.approx(1.001**5 - 1)
    assert spy.ret_1m == pytest.approx(1.001**21 - 1)
    assert spy.above_ma is True and sectors[0].above_ma is False
    assert spy.ret_ytd is not None and spy.ret_ytd > 0.2
    assert indexes[1].close is None
    text = market.render_overview(indexes, sectors)
    assert "S&P 500 (SPY)" in text and "above 200d" in text and "BELOW 200d" in text
    assert text.index("Tech (XLK)") < text.index("Energy (XLE)")  # best sector first
    assert "no recent prices for QQQ" in text


def test_market_symbols_default():
    syms = market.market_symbols({})
    assert syms[:4] == ["SPY", "QQQ", "DIA", "IWM"] and "XLK" in syms and len(syms) == 15
