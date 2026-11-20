import datetime as dt

import pytest
from screener_helpers import companyfacts, flat_prices

from market_intel import screener
from market_intel.screener import backtest, facts, prices


@pytest.fixture
def scon(con):
    screener.init_schema(con)
    return con


def growing(base, g):
    return lambda y, q: base * (1 + g) ** ((y - 2020) + (q - 1) / 4)


def add(con, cik, ticker, g, price, daily_growth, file_lag_days=40):
    facts.upsert_facts(
        con,
        facts.parse_companyfacts(
            companyfacts(
                cik, list(range(2020, 2025)), growing(100e6, g), file_lag_days=file_lag_days
            )
        ),
    )
    con.execute(
        "INSERT INTO screen_universe (cik, ticker, name, exchange, sic) "
        "VALUES (?, ?, ?, 'NYSE', 3570)",
        [cik, ticker, ticker],
    )
    prices.upsert_bars_bulk(
        con,
        flat_prices(ticker, dt.date(2021, 1, 1), dt.date(2024, 12, 31), price, daily_growth),
        "test",
    )


def test_rebalance_dates():
    got = backtest.rebalance_dates(dt.date(2022, 3, 1), dt.date(2023, 5, 25), [2, 5, 8, 11], 20)
    assert got[0] == dt.date(2022, 5, 20) and got[-1] == dt.date(2023, 5, 20)
    assert len(got) == 5


def test_forward_return_uses_entry_before_and_exit_after(scon):
    prices.upsert_bars_bulk(
        scon, flat_prices("X", dt.date(2024, 1, 1), dt.date(2024, 3, 1), 100, 0.01), "t"
    )
    book = backtest.PriceBook(scon)
    r = book.forward_return("X", dt.date(2024, 1, 6), 14)  # Saturday entry -> Friday close
    entry = book.on_or_before("X", dt.date(2024, 1, 6))
    exit_ = book.on_or_after("X", dt.date(2024, 1, 20))
    assert entry[0] == dt.date(2024, 1, 5) and exit_[0] == dt.date(2024, 1, 22)
    assert r == pytest.approx(exit_[1] / entry[1] - 1)
    # Stopped trading before the horizon: last close is used.
    assert book.forward_return("X", dt.date(2024, 2, 20), 60) == pytest.approx(
        book.on_or_before("X", dt.date(2024, 3, 1))[1]
        / book.on_or_before("X", dt.date(2024, 2, 20))[1]
        - 1
    )
    assert book.forward_return("NOPE", dt.date(2024, 2, 1), 10) is None


def test_backtest_picks_winners_and_reports(scon):
    # Growers are also the stocks whose prices rise, so the score should beat the universe.
    for i in range(6):
        g = 0.05 * i
        add(scon, 100 + i, f"T{i}", g, price=10.0 + i, daily_growth=0.0002 * i)
    settings = {"screener": {"top_n": 2, "backtest": {"horizons_days": [91], "benchmark": None}}}
    periods, summary = backtest.run_backtest(
        scon, settings, start=dt.date(2022, 4, 1), end=dt.date(2024, 8, 31)
    )
    assert periods and all(p.n_eligible == 6 for p in periods)
    assert periods[-1].picks["growth"][0] == "T5" and "T0" not in periods[-1].picks["growth"]
    s = summary[91]
    assert s["growth"]["avg_excess_vs_universe"] > 0
    assert s["growth"]["beat_rate"] == 1.0
    report = backtest.render_report(periods, summary, 2, settings)
    assert "survivorship" in report.lower()
    assert "Growth pillar only" in report


def test_backtest_has_no_look_ahead(scon):
    # Filings arrive 120 days after quarter end, so on a rebalance date only older
    # quarters are known: the screen must not see data filed after the date.
    add(scon, 1, "LATE", 0.3, 10.0, 0.0, file_lag_days=120)
    periods, _ = backtest.run_backtest(
        scon,
        {"screener": {"max_staleness_days": 400, "backtest": {"benchmark": None}}},
        start=dt.date(2023, 5, 1),
        end=dt.date(2023, 5, 31),
    )
    from market_intel.screener.metrics import company_metrics

    m = company_metrics(scon, dt.date(2023, 5, 20), max_staleness_days=400)[1]
    # Q4 2022 10-K files 140 days after Dec 31 (May 20 is day 140): not yet strictly
    # visible if filed after; Q3 2022 10-Q was filed Jan 28.
    assert m["period_end"] <= dt.date(2022, 12, 31)
    filed = scon.execute(
        "SELECT max(filed_date) FROM xbrl_facts WHERE period_end = ?", [m["period_end"]]
    ).fetchone()[0]
    assert filed <= dt.date(2023, 5, 20)
    assert len(periods) == 1


def test_backtest_without_prices_raises(scon):
    with pytest.raises(ValueError, match="No prices"):
        backtest.run_backtest(scon, {})
