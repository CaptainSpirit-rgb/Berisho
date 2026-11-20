import datetime as dt

from market_intel import config
from market_intel.config import Security
from market_intel.delivery import digest

NOW = dt.datetime(2026, 11, 20, 13, 0, tzinfo=dt.UTC)  # 17:00 Tbilisi
WATCH = [Security("AAPL", "Apple", 320193), Security("NVDA", "NVIDIA", 1045810)]


def trading_days(n, end=dt.date(2026, 11, 19)):
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= dt.timedelta(days=1)
    return list(reversed(days))


def load_prices(con, symbol, closes, volumes=None, last_open=None):
    days = trading_days(len(closes))
    volumes = volumes or [1_000_000] * len(closes)
    rows = []
    for i, (d, c, v) in enumerate(zip(days, closes, volumes, strict=True)):
        o = last_open if (last_open is not None and i == len(days) - 1) else c
        rows.append((symbol, d, o, c, c, c, c, v, "test"))
    con.executemany(
        "INSERT INTO prices_daily (symbol, trade_date, open, high, low, close, adj_close, "
        "volume, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def add_filing(con, acc, symbol, form, accepted, items=None, subject_cik=1):
    con.execute(
        "INSERT INTO filings (accession_no, cik, symbol, form_type, filed_date, accepted_at, "
        "items, primary_doc_url, subject_cik) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)",
        [
            acc,
            symbol,
            form,
            accepted.date(),
            accepted,
            items,
            f"https://sec.test/{acc}",
            subject_cik,
        ],
    )


def add_insider(con, acc, symbol, name, code, day, shares=100, price=10.0):
    con.execute(
        "INSERT INTO insider_transactions (accession_no, line_no, cik, symbol, insider_name, "
        "transaction_date, transaction_code, shares, price) VALUES (?, 1, 1, ?, ?, ?, ?, ?, ?)",
        [acc, symbol, name, day, code, shares, price],
    )


def settings():
    return config.load_settings()


def test_flags_gap_and_volume_spike(con):
    # AAPL: flat at 100 then opens at 106 (+6% gap) and closes at 107.
    load_prices(con, "AAPL", [100.0] * 25 + [107.0], last_open=106.0)
    # NVDA: steady volume, last day 4x.
    load_prices(con, "NVDA", [50.0] * 26, volumes=[1_000_000] * 25 + [4_000_000])
    d = digest.build_digest(con, WATCH, settings(), now=NOW)
    flags = dict(d.flags)
    assert "gap" in flags["AAPL"] and "+6.0%" in flags["AAPL"]
    assert "4.0x" in flags["NVDA"]
    aapl = d.rows[0]
    assert round(aapl.change_1d_pct, 1) == 7.0 and round(aapl.change_5d_pct, 1) == 7.0


def test_volume_ratio_needs_full_lookback(con):
    load_prices(con, "AAPL", [100.0] * 5, volumes=[1] * 4 + [100])
    d = digest.build_digest(con, WATCH, settings(), now=NOW)
    assert d.rows[0].volume_ratio is None
    assert d.rows[1].close is None  # NVDA has no data


def test_regime_bull_and_insufficient_history(con):
    d = digest.build_digest(con, WATCH, settings(), now=NOW)
    assert d.regime == {"benchmark": "SPY", "history": 0, "needed": 200}
    load_prices(con, "SPY", [400.0] * 199 + [500.0])
    d = digest.build_digest(con, WATCH, settings(), now=NOW)
    assert d.regime["regime"] == "bull" and round(d.regime["ma"], 1) == 400.5


def test_recent_8k_flagged_and_old_filings_excluded(con):
    add_filing(con, "a1", "AAPL", "8-K", dt.datetime(2026, 11, 19, 21, 30), "5.02,9.01")
    add_filing(con, "a2", "AAPL", "10-K", dt.datetime(2026, 11, 15, 11, 0))
    add_filing(con, "a3", "AAPL", "8-K", dt.datetime(2026, 11, 1, 11, 0), "2.02")
    add_filing(con, "a4", "AAPL", "4", dt.datetime(2026, 11, 19, 22, 0))
    d = digest.build_digest(con, WATCH, settings(), now=NOW)
    assert [f["form_type"] for f in d.filings] == ["8-K", "10-K"]
    assert ("AAPL", "new 8-K (5.02 officer/director change)") in d.flags


def test_filings_about_other_issuers_are_excluded(con):
    # e.g. Goldman's own 13G/13D stakes in other companies show up in its EDGAR feed.
    add_filing(con, "g1", "AAPL", "SCHEDULE 13D", dt.datetime(2026, 11, 19, 12), subject_cik=99)
    add_filing(con, "g2", "AAPL", "SC 13G", dt.datetime(2026, 11, 19, 12), subject_cik=None)
    add_filing(con, "g3", "AAPL", "SCHEDULE 13D", dt.datetime(2026, 11, 18, 12))
    d = digest.build_digest(con, WATCH, settings(), now=NOW)
    assert [f["form_type"] for f in d.filings] == ["SCHEDULE 13D"]
    assert [t for s, t in d.flags if s == "AAPL"] == ["new SCHEDULE 13D (activist-size stake)"]


def test_insider_cluster_buy(con):
    for i, name in enumerate(["A", "B", "C"]):
        add_insider(
            con, f"p{i}", "NVDA", name, "P", dt.date(2026, 11, 15), shares=1000, price=100.0
        )
    add_insider(con, "s1", "NVDA", "D", "S", dt.date(2026, 11, 1), shares=10, price=100.0)
    add_insider(con, "old", "NVDA", "E", "P", dt.date(2026, 9, 1))  # outside 30 days
    d = digest.build_digest(con, WATCH, settings(), now=NOW)
    [ins] = d.insiders
    assert (ins["buyers"], ins["buy_value"], ins["sellers"]) == (3, 300_000.0, 1)
    assert any("insider cluster buy: 3 insiders" in t for s, t in d.flags if s == "NVDA")


def test_describe_items():
    assert digest.describe_items("2.02,9.01") == "2.02 results of operations"
    assert digest.describe_items("9.99") == "9.99"
    assert digest.describe_items(None) == ""


def add_headline(con, nid, symbol, published_at, score):
    con.execute(
        """
        INSERT INTO news (news_id, symbol, published_at, source, headline, sentiment_score,
                          sentiment_label, ingested_at)
        VALUES (?, ?, ?, 'Test', ?, ?, 'neutral', ?)
        """,
        [nid, symbol, published_at, f"headline {nid}", score, published_at],
    )


def test_no_sentiment_section_without_news(con):
    from market_intel.delivery import analysis, render

    d = digest.build_digest(con, WATCH, settings(), now=NOW)
    assert d.sentiment == []
    text = render.render_text(d, analysis.rule_based(d, settings()))
    assert "NEWS SENTIMENT" not in text


def test_sentiment_section_and_flag(con):
    from market_intel.delivery import analysis, render

    now = NOW.replace(tzinfo=None)
    for day in range(1, 41):  # 40 days of mildly mixed AAPL news, 2 a day
        for k in range(2):
            t = now - dt.timedelta(days=day, hours=k)
            add_headline(con, f"b{day}-{k}", "AAPL", t, 0.1 if k else -0.1)
    for h in range(6):  # a burst of very negative headlines in the last day
        add_headline(con, f"c{h}", "AAPL", now - dt.timedelta(hours=1 + h), -0.9)
    add_headline(con, "n1", "NVDA", now - dt.timedelta(hours=2), 0.5)  # just started

    d = digest.build_digest(con, WATCH, settings(), now=NOW)
    lines = dict(d.sentiment)
    assert "z " in lines["AAPL"]
    assert "building baseline (0/20 days" in lines["NVDA"]
    aapl_flags = [t for s, t in d.flags if s == "AAPL"]
    assert any("turned negative" in t for t in aapl_flags)
    assert any(t.startswith("news volume ") for t in aapl_flags), aapl_flags
    text = render.render_text(d, analysis.rule_based(d, settings()))
    assert "NEWS SENTIMENT" in text
    html = render.render_html(d, analysis.rule_based(d, settings()))
    assert "<b>News sentiment</b>" in html
