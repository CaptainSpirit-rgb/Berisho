import datetime as dt

from scenario import NOW, WATCH, build

from market_intel import config
from market_intel.collectors import news
from market_intel.delivery import analysis, digest, render

NEW_SHAPE = {
    "id": "abc",
    "content": {
        "title": "Nvidia  shares slide after\nguidance",
        "summary": "Chipmaker's outlook disappointed.",
        "pubDate": "2026-11-19T21:05:00Z",
        "provider": {"displayName": "Reuters"},
        "canonicalUrl": {"url": "https://news.test/nvda-slide"},
    },
}
OLD_SHAPE = {
    "title": "Nvidia CEO speaks",
    "publisher": "Bloomberg",
    "link": "https://news.test/ceo",
    "providerPublishTime": int(dt.datetime(2026, 11, 19, 15, 0, tzinfo=dt.UTC).timestamp()),
}


def test_parse_both_yfinance_shapes():
    h = news.parse_item("NVDA", NEW_SHAPE)
    assert h.headline == "Nvidia shares slide after guidance"
    assert h.published_at == dt.datetime(2026, 11, 19, 21, 5)  # naive UTC
    assert (h.source, h.url) == ("Reuters", "https://news.test/nvda-slide")
    old = news.parse_item("NVDA", OLD_SHAPE)
    assert old.published_at == dt.datetime(2026, 11, 19, 15, 0) and old.source == "Bloomberg"
    assert news.parse_item("NVDA", {"content": {"title": "no date"}}) is None


def test_same_article_two_tickers_two_rows_and_rerun_keeps_sentiment(con):
    assert news.collect_news(con, ["NVDA", "AAPL"], lambda s: [NEW_SHAPE]) == {
        "NVDA": 1,
        "AAPL": 1,
    }
    con.execute("UPDATE news SET sentiment_score = -0.8")
    assert news.collect_news(con, ["NVDA"], lambda s: [NEW_SHAPE, OLD_SHAPE]) == {"NVDA": 1}
    rows = con.execute(
        "SELECT symbol, sentiment_score FROM news ORDER BY symbol, published_at"
    ).fetchall()
    assert rows == [("AAPL", -0.8), ("NVDA", None), ("NVDA", -0.8)]


def test_fetch_failure_reported_per_symbol(con):
    def fetch(symbol):
        if symbol == "AAPL":
            raise RuntimeError("yahoo down")
        return [OLD_SHAPE]

    assert news.collect_news(con, ["AAPL", "NVDA"], fetch) == {"AAPL": -1, "NVDA": 1}


def test_headlines_reach_the_digest(con):
    build(con)
    news.collect_news(con, ["NVDA", "AAPL"], lambda s: [NEW_SHAPE])
    settings = config.load_settings()
    d = digest.build_digest(con, WATCH, settings, now=NOW)
    assert [n["symbol"] for n in d.news] == ["AAPL", "NVDA"]
    a = analysis.rule_based(d, settings)
    assert any('Latest headline: "Nvidia shares slide' in b for b in a.background if "AAPL" in b)
    text = render.render_text(d, a)
    assert "News (20 Nov 01:05, Reuters): Nvidia shares slide after guidance" in text
    html = render.render_html(d, a)
    assert '<a href="https://news.test/nvda-slide">Nvidia shares slide' in html


def test_old_headlines_are_left_out(con):
    stale = dict(OLD_SHAPE, providerPublishTime=int(dt.datetime(2026, 11, 1).timestamp()))
    news.collect_news(con, ["NVDA"], lambda s: [stale])
    d = digest.build_digest(con, WATCH, config.load_settings(), now=NOW)
    assert d.news == []


def test_duplicate_headlines_shown_once(con):
    dup = {"content": dict(NEW_SHAPE["content"], canonicalUrl={"url": "https://other.test/x"})}
    news.collect_news(con, ["NVDA"], lambda s: [NEW_SHAPE, dup])
    assert con.execute("SELECT count(*) FROM news").fetchone()[0] == 2  # both stored
    d = digest.build_digest(con, WATCH, config.load_settings(), now=NOW)
    assert len(d.headlines("NVDA")) == 1


def test_sentiment_note_says_when_finbert_is_missing(con, monkeypatch):
    from market_intel.signals import sentiment

    news.collect_news(con, ["NVDA"], lambda s: [NEW_SHAPE])
    monkeypatch.setattr(sentiment, "finbert_available", lambda: False)
    d = digest.build_digest(con, WATCH, config.load_settings(), now=NOW)
    assert "FinBERT isn't installed, so 1 of 1 collected headlines" in d.sentiment_note
    assert "pip install -e .[sentiment]" in render.render_text(
        d, analysis.rule_based(d, config.load_settings())
    ).replace("\n", " ")
    monkeypatch.setattr(sentiment, "finbert_available", lambda: True)
    d = digest.build_digest(con, WATCH, config.load_settings(), now=NOW)
    assert "signals start after 20 days" in d.sentiment_note
