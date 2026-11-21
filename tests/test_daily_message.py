"""The combined daily message: watchlist digest + FinBERT sentiment + the latest weekly
screener candidates, in one Telegram message (split only if it gets too long)."""

import datetime as dt
import json

from scenario import NOW, WATCH, build

from market_intel import config, screener
from market_intel.delivery import analysis, digest, export, render, telegram
from market_intel.signals import sentiment


def save_run(con, run_date, picks):
    for rank, (ticker, cik, sector) in enumerate(picks, 1):
        con.execute(
            "INSERT OR REPLACE INTO screen_universe (cik, ticker, name, sector) "
            "VALUES (?, ?, ?, ?)",
            [cik, ticker, f"{ticker} Corp", sector],
        )
        con.execute(
            """
            INSERT INTO screen_results
                (run_date, cik, ticker, name, rank, score, pillars, metrics, reasons, warnings)
            VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?, ?)
            """,
            [
                run_date,
                cik,
                ticker,
                f"{ticker} Corp",
                rank,
                0.9 - rank / 100,
                json.dumps({"market_cap": 50e9 + rank * 1e9}),
                json.dumps([f"Sales +{30 - rank}% over the last 12 months", "Net cash"]),
                json.dumps(["Growth slowing"] if rank % 2 else []),
            ],
        )


PICKS = [
    ("ANET", 1, "Information Technology"),
    ("AAPL", 320193, "Information Technology"),
    ("LLY", 3, "Health Care"),
    ("CAT", 4, "Industrials"),
    ("ODFL", 5, "Industrials"),
    ("FICO", 6, "Information Technology"),
    ("MPWR", 7, "Information Technology"),
]


def add_news(con, nid, symbol, hours_ago, score):
    t = NOW.replace(tzinfo=None) - dt.timedelta(hours=hours_ago)
    con.execute(
        """
        INSERT INTO news (news_id, symbol, published_at, source, headline, url,
                          sentiment_score, sentiment_label, ingested_at)
        VALUES (?, ?, ?, 'Reuters', ?, 'https://example.com/a', ?, 'x', ?)
        """,
        [nid, symbol, t, f"{symbol} headline {nid}", score, t - dt.timedelta(days=3)],
    )


def daily(con, monkeypatch, settings=None):
    monkeypatch.setattr(sentiment, "finbert_available", lambda: True)
    settings = settings or config.load_settings()
    d = digest.build_digest(con, WATCH, settings, now=NOW)
    return d, analysis.rule_based(d, settings)


def test_combined_message_has_digest_sentiment_and_candidates(con, monkeypatch):
    build(con)
    screener.init_schema(con)
    save_run(con, dt.date(2026, 11, 9), PICKS[2:])
    save_run(con, dt.date(2026, 11, 16), PICKS)
    for h in range(3):
        add_news(con, f"n{h}", "NVDA", 2 + h, -0.6)

    d, a = daily(con, monkeypatch)
    c = d.candidates
    assert c.run_date == dt.date(2026, 11, 16) and c.age_days == 4 and not c.stale
    assert [p["ticker"] for p in c.picks] == ["ANET", "AAPL", "LLY", "CAT", "ODFL"]
    assert c.picks[0]["new"] and not c.picks[2]["new"]  # LLY was in last week's list
    assert c.picks[1]["on_watchlist"] and c.picks[0]["sector"] == "Information Technology"

    text = render.render_text(d, a)
    assert "NEW CANDIDATES (from the weekly S&P 500 screen of 16 Nov, top 5 of 7)" in text
    assert "[new this week, on your watchlist]" in text.replace("\n   ", " ")
    assert "+ Sales +29% over the last 12 months" in text and "! Growth slowing" in text
    assert "FICO" not in text  # only the top 5
    # Sentiment: raw tone while the baseline builds, and when signals will start.
    assert "negative -0.60 (3 headlines, last 24h) · baseline" in text
    assert "raw FinBERT tone" in d.sentiment_note and "the first around" in d.sentiment_note
    # Order: watchlist numbers, then sentiment, then the candidates.
    assert text.index("NUMBERS") < text.index("NEWS SENTIMENT") < text.index("NEW CANDIDATES")

    html = render.render_html(d, a)
    assert "<b>New candidates</b>" in html
    assert (
        "1. <b>ANET</b> ANET Corp ($51.0B, Information Technology) · score 0.89 [new this week]"
        in html
    )
    assert html.rstrip().endswith("not financial advice.</i>")
    messages = telegram.split_html(html)
    assert all(len(m) <= telegram.MAX_MESSAGE_CHARS for m in messages)

    data = export.build_export(con, d, a, config.load_settings())
    assert data["candidates"]["picks"][0]["ticker"] == "ANET"
    json.dumps(data, default=str)


def test_stale_screen_says_how_to_refresh(con, monkeypatch):
    build(con)
    screener.init_schema(con)
    save_run(con, dt.date(2026, 10, 30), PICKS)
    d, a = daily(con, monkeypatch)
    assert d.candidates.stale and d.candidates.age_days == 21
    assert "This list is 21 days old. Run market-intel run-weekly" in render.render_html(d, a)


def test_no_screen_yet_shows_a_hint(con, monkeypatch):
    build(con)  # screener tables never created
    d, a = daily(con, monkeypatch)
    assert d.candidates.run_date is None
    text = render.render_text(d, a).replace("\n", " ")
    assert "No screen saved yet. Run market-intel run-weekly once" in text
    screener.init_schema(con)  # tables exist but are empty: same hint
    d, a = daily(con, monkeypatch)
    assert "No screen saved yet" in render.render_html(d, a)


def test_top_n_comes_from_settings(con, monkeypatch):
    build(con)
    screener.init_schema(con)
    save_run(con, dt.date(2026, 11, 16), PICKS)
    s = config.load_settings()
    s["digest"]["candidates_top_n"] = 2
    d, _ = daily(con, monkeypatch, s)
    assert [p["ticker"] for p in d.candidates.picks] == ["ANET", "AAPL"]


def test_sentiment_ready_has_no_warming_note(con, monkeypatch):
    build(con)
    now = NOW.replace(tzinfo=None)
    for day in range(1, 41):
        for k in range(2):
            t = now - dt.timedelta(days=day, hours=k)
            con.execute(
                "INSERT INTO news (news_id, symbol, published_at, source, headline, "
                "sentiment_score, sentiment_label, ingested_at) VALUES (?, ?, ?, 'T', 'h', ?, "
                "'x', ?)",
                [f"{sym}{day}{k}", sym, t, 0.1, t],
            ) if (sym := "AAPL") else None
    d, _ = daily(con, monkeypatch)
    assert d.sentiment_note == ""
