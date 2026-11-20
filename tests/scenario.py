"""A small database that looks like the 20 Nov 2026 digest: a bear market, NVDA down
hard right after its earnings 8-K, JPM with an officer change, insider selling at AAPL."""

import datetime as dt

from market_intel.config import Security

NOW = dt.datetime(2026, 11, 20, 13, 0, tzinfo=dt.UTC)  # 17:00 Tbilisi
WATCH = [
    Security("AAPL", "Apple", 320193),
    Security("NVDA", "NVIDIA", 1045810),
    Security("JPM", "JPMorgan Chase", 19617),
]
NVDA_8K = "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000088/nvda-20261117.htm"
JPM_8K = "https://www.sec.gov/Archives/edgar/data/19617/000001961726000407/jpm-20261117.htm"


def trading_days(n, end=dt.date(2026, 11, 19)):
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= dt.timedelta(days=1)
    return list(reversed(days))


def load(con, symbol, closes, volumes=None):
    days = trading_days(len(closes))
    volumes = volumes or [1_000_000] * len(closes)
    con.executemany(
        "INSERT INTO prices_daily (symbol, trade_date, open, high, low, close, adj_close, "
        "volume, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'test')",
        [(symbol, d, c, c, c, c, c, v) for d, c, v in zip(days, closes, volumes, strict=True)],
    )


def build(con):
    # SPY: 200 days at 730, then a slide to 690 -> bear, about -3% over 5 days.
    load(con, "SPY", [730.0] * 200 + [712.0, 711.0, 705.0, 700.0, 695.0, 690.0])
    load(con, "AAPL", [356.0] * 21 + [350.0, 349.0, 348.0, 346.0, 342.5])  # -3.8% 5d
    load(
        con,
        "NVDA",
        [163.0] * 21 + [160.0, 158.0, 150.0, 146.0, 125.5],  # -23% 5d, -14% 1d
        volumes=[1_000_000] * 25 + [2_400_000],
    )
    load(con, "JPM", [346.0] * 21 + [330.0, 325.0, 322.0, 320.0, 310.5])
    for acc, sym, cik, items, url, when in (
        (
            "0001045810-26-000088",
            "NVDA",
            1045810,
            "2.02,9.01",
            NVDA_8K,
            dt.datetime(2026, 11, 17, 21, 20),
        ),
        ("0000019617-26-000407", "JPM", 19617, "5.02", JPM_8K, dt.datetime(2026, 11, 17, 13, 0)),
    ):
        con.execute(
            "INSERT INTO filings (accession_no, cik, symbol, form_type, filed_date, accepted_at, "
            "items, primary_doc_url, subject_cik) VALUES (?, ?, ?, '8-K', ?, ?, ?, ?, ?)",
            [acc, cik, sym, when.date(), when, items, url, cik],
        )
    con.execute(
        "INSERT INTO filing_documents (accession_no, doc_name, doc_type, url, text) VALUES "
        "('0001045810-26-000088', 'pr.htm', 'EX-99.1', 'https://sec.test/pr.htm', "
        "'NVIDIA reports revenue of $10.0 billion. Outlook: revenue expected to decline.')"
    )
    for i, (name, value) in enumerate((("Insider A", 20e6), ("Insider B", 15.1e6))):
        con.execute(
            "INSERT INTO insider_transactions (accession_no, line_no, cik, symbol, insider_name, "
            "insider_title, transaction_date, transaction_code, shares, price) "
            "VALUES (?, 1, 320193, 'AAPL', ?, 'Officer', ?, 'S', ?, 100.0)",
            [f"s{i}", name, dt.date(2026, 11, 10), value / 100],
        )
