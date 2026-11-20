import datetime as dt
import itertools
import json

import pytest

from market_intel.signals import regime, sentiment

NOW = dt.datetime(2026, 3, 2, 12, 0)  # naive UTC
SETTINGS = {"sentiment": {"min_baseline_days": 20, "min_baseline_headlines": 30}}


class FakeScorer:
    """Scores by keyword so tests don't need the FinBERT model."""

    def __init__(self):
        self.calls = []

    def score(self, texts):
        self.calls.append(list(texts))
        out = []
        for t in texts:
            if "beats" in t:
                out.append((0.9, "positive"))
            elif "misses" in t:
                out.append((-0.9, "negative"))
            else:
                out.append((0.0, "neutral"))
        return out


_ids = itertools.count()


def add_news(con, symbol, published_at, headline, score=None, ingested_at=None):
    label = (
        None
        if score is None
        else ("positive" if score > 0.3 else "negative" if score < -0.3 else "neutral")
    )
    con.execute(
        """
        INSERT INTO news (news_id, symbol, published_at, source, headline, sentiment_score,
                          sentiment_label, ingested_at)
        VALUES (?, ?, ?, 'Test', ?, ?, ?, ?)
        """,
        [
            f"id{next(_ids)}",
            symbol,
            published_at,
            headline,
            score,
            label,
            ingested_at or published_at,
        ],
    )


def add_baseline(con, symbol, days=40, per_day=2, scores=(0.1, -0.1)):
    """`per_day` headlines a day for `days` days before the current window."""
    start = NOW - dt.timedelta(days=days + 1)
    for d in range(days):
        for k in range(per_day):
            t = start + dt.timedelta(days=d, hours=2 + k)
            add_news(
                con,
                symbol,
                t,
                f"{symbol} routine {d}-{k}",
                scores[k % len(scores)],
                ingested_at=start if d == 0 else t,
            )


# ---------------------------------------------------------------- scoring


def test_probs_to_score_uses_model_labels():
    # ProsusAI/finbert order: 0 positive, 1 negative, 2 neutral
    labels = {0: "positive", 1: "negative", 2: "neutral"}
    score, label = sentiment.probs_to_score([0.7, 0.2, 0.1], labels)
    assert score == pytest.approx(0.5)
    assert label == "positive"
    score, label = sentiment.probs_to_score([0.1, 0.1, 0.8], labels)
    assert score == pytest.approx(0.0)
    assert label == "neutral"


def test_score_unscored_scores_once_and_is_idempotent(con):
    add_news(con, "AAPL", NOW, "Apple beats estimates")
    add_news(con, "MSFT", NOW, "Apple beats estimates")  # same article, two tickers
    add_news(con, "KO", NOW, "Coca-Cola misses on volume")
    add_news(con, "KO", NOW, "Already scored", score=0.42)
    scorer = FakeScorer()

    assert sentiment.score_unscored(con, scorer) == 3
    assert len(scorer.calls[0]) == 2  # duplicate headline scored once
    rows = dict(con.execute("SELECT headline, sentiment_score FROM news").fetchall())
    assert rows["Apple beats estimates"] == pytest.approx(0.9)
    assert rows["Coca-Cola misses on volume"] == pytest.approx(-0.9)
    assert rows["Already scored"] == pytest.approx(0.42)  # untouched

    assert sentiment.score_unscored(con, scorer) == 0
    assert len(scorer.calls) == 1


# ---------------------------------------------------------------- daily summary


def test_refresh_daily_includes_zero_days(con):
    d0 = dt.datetime(2026, 3, 1, 9)
    add_news(con, "AAPL", d0, "a", 0.5)
    add_news(con, "AAPL", d0 + dt.timedelta(hours=1), "b", -0.1)
    add_news(con, "AAPL", d0 + dt.timedelta(days=2), "c", 0.9)

    sentiment.refresh_daily(con, through=dt.date(2026, 3, 4))
    rows = con.execute(
        "SELECT day, n_headlines, mean_score, n_positive FROM sentiment_daily ORDER BY day"
    ).fetchall()
    assert [r[0] for r in rows] == [dt.date(2026, 3, d) for d in (1, 2, 3, 4)]
    assert [r[1] for r in rows] == [2, 0, 1, 0]
    assert rows[0][2] == pytest.approx(0.2)
    assert rows[1][2] is None
    # rebuilding gives the same result
    sentiment.refresh_daily(con, through=dt.date(2026, 3, 4))
    assert con.execute("SELECT count(*) FROM sentiment_daily").fetchone()[0] == 4


# ---------------------------------------------------------------- signals


def test_no_news_and_warming_up(con):
    add_baseline(con, "AAPL", days=10)  # only 10 days of history
    res = {r.symbol: r for r in sentiment.detect(con, ["AAPL", "KO"], SETTINGS, now=NOW)}
    assert res["KO"].status == "no_news"
    assert res["AAPL"].status == "warming_up"
    assert res["AAPL"].history_days == 10
    cfg = sentiment.sentiment_settings(SETTINGS)
    assert "building baseline (10/20 days" in sentiment.describe(res["AAPL"], cfg)
    assert con.execute("SELECT count(*) FROM signals").fetchone()[0] == 0


def test_backfilled_headlines_dont_count_as_history(con):
    # First fetch on day X returns headlines published 30 days earlier.
    fetched = NOW - dt.timedelta(days=3)
    for d in range(30):
        add_news(con, "AAPL", fetched - dt.timedelta(days=d), f"old {d}", 0.1, ingested_at=fetched)
    (r,) = sentiment.detect(con, ["AAPL"], SETTINGS, now=NOW)
    assert r.status == "warming_up"
    assert r.history_days == 2


def test_quiet_day_raises_nothing(con):
    add_baseline(con, "AAPL")
    for h in range(2):
        add_news(con, "AAPL", NOW - dt.timedelta(hours=3 + h), f"routine today {h}", 0.0)
    (r,) = sentiment.detect(con, ["AAPL"], SETTINGS, now=NOW)
    assert r.status == "ready"
    assert r.signals == []
    assert r.baseline_mean == pytest.approx(0.0)
    assert r.daily_avg_count == pytest.approx(2.0)


def test_positive_shift_and_volume_spike(con):
    add_baseline(con, "NVDA")
    for h in range(8):
        add_news(con, "NVDA", NOW - dt.timedelta(hours=1 + h), f"NVDA beats {h}", 0.9)
    (r,) = sentiment.detect(con, ["NVDA"], SETTINGS, now=NOW, regime="bull")
    types = {s["signal_type"]: s for s in r.signals}
    assert set(types) == {"sentiment_shift", "news_volume_spike"}
    shift = types["sentiment_shift"]
    assert shift["details"]["direction"] == "positive"
    assert shift["details"]["z"] > 2
    assert 0 < shift["strength"] <= 1
    assert types["news_volume_spike"]["details"]["ratio"] == pytest.approx(4.0)

    rows = con.execute("SELECT signal_type, details FROM signals ORDER BY signal_type").fetchall()
    assert [t for t, _ in rows] == ["news_volume_spike", "sentiment_shift"]
    assert json.loads(rows[1][1])["regime"] == "bull"
    assert "turned positive" in sentiment.flag_text(shift)


def test_negative_shift_is_halved_in_bear_market(con):
    add_baseline(con, "KO")
    for h in range(4):
        add_news(con, "KO", NOW - dt.timedelta(hours=1 + h), f"KO misses {h}", -0.9)
    (bull,) = sentiment.detect(con, ["KO"], SETTINGS, now=NOW, regime="bull", write=False)
    (bear,) = sentiment.detect(con, ["KO"], SETTINGS, now=NOW, regime="bear", write=False)
    b, s = bull.signals[0], bear.signals[0]
    assert s["details"]["direction"] == "negative"
    assert s["strength"] == pytest.approx(b["strength"] * 0.5)
    assert "bear market weight x0.5" in sentiment.flag_text(s)
    assert con.execute("SELECT count(*) FROM signals").fetchone()[0] == 0  # write=False


def test_rerun_same_day_replaces_signals(con):
    add_baseline(con, "NVDA")
    for h in range(8):
        add_news(con, "NVDA", NOW - dt.timedelta(hours=1 + h), f"NVDA beats {h}", 0.9)
    sentiment.detect(con, ["NVDA"], SETTINGS, now=NOW)
    sentiment.detect(con, ["NVDA"], SETTINGS, now=NOW + dt.timedelta(hours=1))
    assert con.execute("SELECT count(*) FROM signals").fetchone()[0] == 2


def test_settings_defaults_merge():
    cfg = sentiment.sentiment_settings(
        {"sentiment": {"shift_z": 3, "regime_multiplier": {"bear": 0.2}}}
    )
    assert cfg["shift_z"] == 3
    assert cfg["baseline_days"] == 60
    assert cfg["regime_multiplier"] == {"bull": 1.0, "bear": 0.2}


# ---------------------------------------------------------------- regime


def test_current_regime(con):
    start = dt.date(2025, 1, 1)
    for i in range(200):
        con.execute(
            "INSERT INTO prices_daily (symbol, trade_date, close, source) "
            "VALUES ('SPY', ?, ?, 't')",
            [start + dt.timedelta(days=i), 100 + i],
        )
    assert regime.current_regime(con, "SPY", 200) == "bull"
    assert regime.current_regime(con, "SPY", 201) is None
    con.execute(
        "UPDATE prices_daily SET close = 50 WHERE trade_date = ?", [start + dt.timedelta(days=199)]
    )
    assert regime.current_regime(con, "SPY", 200) == "bear"
