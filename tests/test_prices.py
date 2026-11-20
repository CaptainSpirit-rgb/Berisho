import datetime as dt
import sys
import types

import pandas as pd

from market_intel.collectors import prices
from market_intel.collectors.prices import PriceBar

TODAY = dt.date(2026, 11, 20)


def bar(symbol, day, close, volume=1_000):
    return PriceBar(symbol, day, close, close, close, close, close, volume)


class FakeFetcher:
    def __init__(self, bars_by_symbol=None, fail=()):
        self.bars = bars_by_symbol or {}
        self.fail = set(fail)
        self.calls = []

    def __call__(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        if symbol in self.fail:
            raise ConnectionError("boom")
        return [b for b in self.bars.get(symbol, []) if start <= b.trade_date < end]


def test_first_run_uses_lookback_and_upserts(con):
    fetch = FakeFetcher({"AAPL": [bar("AAPL", TODAY, 100.0)]})
    result = prices.collect_prices(
        con, ["aapl"], fetch, source="fake", today=TODAY, lookback_days=400
    )
    assert result == {"AAPL": 1}
    assert fetch.calls == [("AAPL", TODAY - dt.timedelta(days=400), TODAY + dt.timedelta(days=1))]
    assert con.execute("SELECT symbol, close, source FROM prices_daily").fetchall() == [
        ("AAPL", 100.0, "fake")
    ]


def test_rerun_is_idempotent_and_incremental(con):
    day1, day2 = dt.date(2026, 11, 18), dt.date(2026, 11, 19)
    fetch = FakeFetcher({"AAPL": [bar("AAPL", day1, 100.0), bar("AAPL", day2, 101.0)]})
    prices.collect_prices(con, ["AAPL"], fetch, today=TODAY)
    # Vendor corrects day2's close; re-run should replace, not duplicate.
    fetch.bars["AAPL"][1] = bar("AAPL", day2, 102.0)
    prices.collect_prices(con, ["AAPL"], fetch, today=TODAY)
    assert fetch.calls[1][1] == day2 - dt.timedelta(days=prices.REFETCH_OVERLAP_DAYS)
    assert con.execute(
        "SELECT trade_date, close FROM prices_daily ORDER BY trade_date"
    ).fetchall() == [(day1, 100.0), (day2, 102.0)]


def test_one_symbol_failing_does_not_stop_others(con):
    fetch = FakeFetcher({"GS": [bar("GS", TODAY, 500.0)]}, fail={"AAPL"})
    result = prices.collect_prices(con, ["AAPL", "GS"], fetch, today=TODAY)
    assert result == {"AAPL": -1, "GS": 1}


def test_bars_without_close_are_skipped(con):
    fetch = FakeFetcher({"XOM": [bar("XOM", TODAY, None)]})
    assert prices.collect_prices(con, ["XOM"], fetch, today=TODAY) == {"XOM": 0}


def test_yfinance_fetcher_maps_columns(monkeypatch):
    idx = pd.DatetimeIndex(
        [
            pd.Timestamp("2026-11-18", tz="America/New_York"),
            pd.Timestamp("2026-11-19", tz="America/New_York"),
        ]
    )
    df = pd.DataFrame(
        {
            "Open": [1.0, 2.0],
            "High": [1.5, 2.5],
            "Low": [0.5, 1.5],
            "Close": [1.2, float("nan")],
            "Adj Close": [1.1, 2.1],
            "Volume": [100, 200],
        },
        index=idx,
    )
    seen = {}

    class FakeTicker:
        def __init__(self, symbol):
            seen["symbol"] = symbol

        def history(self, **kwargs):
            seen.update(kwargs)
            return df

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=FakeTicker))
    bars = prices.yfinance_fetcher("NVDA", dt.date(2026, 11, 1), dt.date(2026, 11, 20))
    assert seen["symbol"] == "NVDA" and seen["auto_adjust"] is False
    assert bars[0] == PriceBar("NVDA", dt.date(2026, 11, 18), 1.0, 1.5, 0.5, 1.2, 1.1, 100)
    assert bars[1].close is None  # NaN becomes None and is skipped on upsert
