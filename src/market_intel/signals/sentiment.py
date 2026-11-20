"""News sentiment, ported from the thesis pipeline.

1. ``score_unscored`` runs FinBERT (``ProsusAI/finbert``) over headlines in ``news`` that
   have no score yet and stores ``sentiment_score`` = P(positive) - P(negative), in
   [-1, 1], plus the top label. Scores are written once; re-runs only touch new rows.
2. ``refresh_daily`` rebuilds ``sentiment_daily`` (one row per ticker and UTC day,
   including zero-headline days) from ``news``. It's a summary for the digest and for
   later evaluation, so rebuilding it is always safe.
3. ``detect`` compares each ticker's recent headlines with its *own* baseline, because
   the thesis found the raw level a weak predictor:

   * ``sentiment_shift``: mean score of the last ``current_window_hours`` vs the
     baseline mean of the prior ``baseline_days``, as a z-score of the mean
     (difference / (baseline std / sqrt(n current))).
   * ``news_volume_spike``: headline count in the current window vs the average daily
     count over the baseline (zero days included).

   A ticker needs ``min_baseline_days`` of collection history and
   ``min_baseline_headlines`` scored headlines before it can raise a signal. Before that
   it reports how far its baseline has got. Signal strength is scaled by the regime
   multiplier (the thesis found sentiment worked better in bull markets).

The baseline only counts days since the app started collecting news for a ticker
(first ``ingested_at``). The first fetch also returns some older headlines; those are
scored but don't make a short history look like a long one.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import duckdb

log = logging.getLogger(__name__)

DEFAULT_MODEL = "ProsusAI/finbert"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "model": DEFAULT_MODEL,
    "batch_size": 32,
    "current_window_hours": 24,
    "baseline_days": 60,
    "min_baseline_days": 20,
    "min_baseline_headlines": 30,
    "min_current_headlines": 3,
    "shift_z": 2.0,
    "volume_multiple": 3.0,
    "regime_multiplier": {"bull": 1.0, "bear": 0.5},
}


def sentiment_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """The ``sentiment`` block of settings.yaml with defaults filled in."""
    merged = {**DEFAULTS, **(settings.get("sentiment") or {})}
    merged["regime_multiplier"] = {
        **DEFAULTS["regime_multiplier"],
        **((settings.get("sentiment") or {}).get("regime_multiplier") or {}),
    }
    return merged


# ---------------------------------------------------------------------------- scoring


class Scorer(Protocol):
    def score(self, texts: Sequence[str]) -> list[tuple[float, str]]:
        """(score in [-1, 1], label) for each text, in order."""
        ...


def probs_to_score(probs: Sequence[float], id2label: dict[int, str]) -> tuple[float, str]:
    """Turn one row of class probabilities into (P(pos) - P(neg), top label)."""
    by_label = {id2label[i].lower(): float(p) for i, p in enumerate(probs)}
    score = by_label.get("positive", 0.0) - by_label.get("negative", 0.0)
    label = max(by_label, key=by_label.__getitem__)
    return score, label


class FinBertScorer:
    """FinBERT on the CPU. Needs the optional extra: ``pip install -e .[sentiment]``.
    The model (~440 MB) downloads from Hugging Face on first use and is cached."""

    def __init__(self, model_name: str = DEFAULT_MODEL, batch_size: int = 32):
        try:
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise ImportError(
                "FinBERT needs the sentiment extra: pip install -e .[sentiment]"
            ) from exc
        self._torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModelForSequenceClassification.from_pretrained(model_name)
        self.model.eval()
        self.id2label = {int(k): v for k, v in self.model.config.id2label.items()}
        self.batch_size = batch_size

    def score(self, texts: Sequence[str]) -> list[tuple[float, str]]:  # pragma: no cover
        out: list[tuple[float, str]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = list(texts[i : i + self.batch_size])
            enc = self.tokenizer(
                batch, padding=True, truncation=True, max_length=128, return_tensors="pt"
            )
            with self._torch.no_grad():
                probs = self._torch.softmax(self.model(**enc).logits, dim=-1).tolist()
            out += [probs_to_score(p, self.id2label) for p in probs]
        return out


def finbert_available() -> bool:
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError:
        return False
    return True


def score_unscored(con: duckdb.DuckDBPyConnection, scorer: Scorer) -> int:
    """Score every headline without a sentiment score. Identical headlines (one article
    mapped to several tickers) are scored once. Returns the number of rows updated."""
    rows = con.execute(
        "SELECT news_id, headline FROM news WHERE sentiment_score IS NULL"
    ).fetchall()
    if not rows:
        return 0
    texts = sorted({h.strip() for _, h in rows})
    scored = dict(zip(texts, scorer.score(texts), strict=True))
    con.executemany(
        "UPDATE news SET sentiment_score = ?, sentiment_label = ? WHERE news_id = ?",
        [[*scored[h.strip()], nid] for nid, h in rows],
    )
    log.info("Scored %d headlines (%d unique)", len(rows), len(texts))
    return len(rows)


# ---------------------------------------------------------------------------- daily summary


def refresh_daily(con: duckdb.DuckDBPyConnection, through: dt.date | None = None) -> int:
    """Rebuild ``sentiment_daily`` from ``news``: one row per ticker per UTC day from the
    day its news collection started through ``through`` (default: today, UTC)."""
    through = through or dt.datetime.now(dt.UTC).date()
    con.execute("DELETE FROM sentiment_daily")
    con.execute(
        """
        INSERT INTO sentiment_daily
            (symbol, day, n_headlines, n_scored, mean_score, n_positive, n_negative, n_neutral)
        WITH starts AS (
            SELECT symbol, min(ingested_at)::DATE AS start_day
            FROM news WHERE symbol IS NOT NULL GROUP BY symbol
        ),
        days AS (
            SELECT s.symbol, d::DATE AS day
            FROM starts s,
                 generate_series(s.start_day::TIMESTAMP, ?::TIMESTAMP, INTERVAL 1 DAY) t(d)
        )
        SELECT d.symbol, d.day,
               count(n.news_id),
               count(n.sentiment_score),
               avg(n.sentiment_score),
               count(*) FILTER (WHERE n.sentiment_label = 'positive'),
               count(*) FILTER (WHERE n.sentiment_label = 'negative'),
               count(*) FILTER (WHERE n.sentiment_label = 'neutral')
        FROM days d
        LEFT JOIN news n ON n.symbol = d.symbol AND n.published_at::DATE = d.day
        GROUP BY d.symbol, d.day
        """,
        [through],
    )
    return con.execute("SELECT count(*) FROM sentiment_daily").fetchone()[0]


# ---------------------------------------------------------------------------- signals


@dataclass
class TickerSentiment:
    symbol: str
    status: str  # 'no_news' | 'warming_up' | 'ready'
    history_days: int = 0
    baseline_headlines: int = 0
    current_n: int = 0
    current_mean: float | None = None
    baseline_mean: float | None = None
    baseline_std: float | None = None
    z: float | None = None
    daily_avg_count: float | None = None
    volume_ratio: float | None = None
    signals: list[dict[str, Any]] = field(default_factory=list)


def _stats(values: list[float]) -> tuple[float, float]:
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / max(len(values) - 1, 1)
    return mean, math.sqrt(var)


def _signal_id(signal_type: str, symbol: str, day: dt.date) -> str:
    return hashlib.sha256(f"{signal_type}|{symbol}|{day.isoformat()}".encode()).hexdigest()[:32]


def analyse_ticker(
    con: duckdb.DuckDBPyConnection,
    symbol: str,
    now: dt.datetime,
    cfg: dict[str, Any],
    regime: str | None = None,
) -> TickerSentiment:
    """Compare one ticker's current window with its own baseline. ``now`` is naive UTC."""
    start = con.execute("SELECT min(ingested_at) FROM news WHERE symbol = ?", [symbol]).fetchone()[
        0
    ]
    if start is None:
        return TickerSentiment(symbol, "no_news")

    window_start = now - dt.timedelta(hours=float(cfg["current_window_hours"]))
    baseline_start = max(
        window_start - dt.timedelta(days=int(cfg["baseline_days"])),
        dt.datetime.combine(start.date(), dt.time()),
    )
    history_days = max((window_start - baseline_start).days, 0)

    rows = con.execute(
        """
        SELECT published_at, sentiment_score FROM news
        WHERE symbol = ? AND published_at >= ? AND published_at < ?
        """,
        [symbol, baseline_start, now],
    ).fetchall()
    base = [s for t, s in rows if t < window_start and s is not None]
    cur_all = [s for t, s in rows if t >= window_start]
    cur = [s for s in cur_all if s is not None]
    base_count = sum(1 for t, _ in rows if t < window_start)

    ts = TickerSentiment(
        symbol,
        "warming_up",
        history_days=history_days,
        baseline_headlines=len(base),
        current_n=len(cur_all),
        current_mean=sum(cur) / len(cur) if cur else None,
    )
    if history_days < int(cfg["min_baseline_days"]) or len(base) < int(
        cfg["min_baseline_headlines"]
    ):
        return ts

    ts.status = "ready"
    ts.baseline_mean, ts.baseline_std = _stats(base)
    window_days = float(cfg["current_window_hours"]) / 24
    ts.daily_avg_count = base_count / history_days
    expected = ts.daily_avg_count * window_days
    ts.volume_ratio = len(cur_all) / expected if expected > 0 else None

    mult = float(cfg["regime_multiplier"].get(regime, 1.0)) if regime else 1.0
    day = now.date()
    common = {
        "regime": regime,
        "regime_multiplier": mult,
        "window_hours": cfg["current_window_hours"],
    }

    if len(cur) >= int(cfg["min_current_headlines"]):
        se = (ts.baseline_std or 0.0) / math.sqrt(len(cur))
        ts.z = (ts.current_mean - ts.baseline_mean) / se if se > 0 else None
        if ts.z is not None and abs(ts.z) >= float(cfg["shift_z"]):
            ts.signals.append(
                {
                    "signal_id": _signal_id("sentiment_shift", symbol, day),
                    "signal_type": "sentiment_shift",
                    "strength": round(min(1.0, abs(ts.z) / (2 * float(cfg["shift_z"]))) * mult, 4),
                    "details": {
                        **common,
                        "direction": "positive" if ts.z > 0 else "negative",
                        "z": round(ts.z, 3),
                        "current_mean": round(ts.current_mean, 4),
                        "current_headlines": len(cur),
                        "baseline_mean": round(ts.baseline_mean, 4),
                        "baseline_std": round(ts.baseline_std, 4),
                        "baseline_headlines": len(base),
                    },
                }
            )
    thr = float(cfg["volume_multiple"])
    if (
        ts.volume_ratio is not None
        and ts.volume_ratio >= thr
        and len(cur_all) >= int(cfg["min_current_headlines"])
    ):
        ts.signals.append(
            {
                "signal_id": _signal_id("news_volume_spike", symbol, day),
                "signal_type": "news_volume_spike",
                "strength": round(min(1.0, ts.volume_ratio / (2 * thr)) * mult, 4),
                "details": {
                    **common,
                    "ratio": round(ts.volume_ratio, 2),
                    "current_headlines": len(cur_all),
                    "baseline_daily_avg": round(ts.daily_avg_count, 3),
                    "history_days": history_days,
                },
            }
        )
    return ts


def detect(
    con: duckdb.DuckDBPyConnection,
    symbols: Sequence[str],
    settings: dict[str, Any],
    now: dt.datetime | None = None,
    regime: str | None = None,
    write: bool = True,
) -> list[TickerSentiment]:
    """Analyse every ticker and (with ``write``) upsert its signals into ``signals``.
    Re-running on the same UTC day replaces that day's signals."""
    cfg = sentiment_settings(settings)
    now = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC).replace(tzinfo=None)
    results = [analyse_ticker(con, s, now, cfg, regime) for s in symbols]
    if write:
        for r in results:
            con.execute(
                """
                DELETE FROM signals WHERE symbol = ? AND as_of::DATE = ?
                  AND signal_type IN ('sentiment_shift', 'news_volume_spike')
                """,
                [r.symbol, now.date()],
            )
            for s in r.signals:
                con.execute(
                    """
                    INSERT OR REPLACE INTO signals
                        (signal_id, symbol, signal_type, as_of, strength, details, source_ref)
                    VALUES (?, ?, ?, ?, ?, ?, NULL)
                    """,
                    [
                        s["signal_id"],
                        r.symbol,
                        s["signal_type"],
                        now,
                        s["strength"],
                        json.dumps(s["details"]),
                    ],
                )
    return results


def describe(ts: TickerSentiment, cfg: dict[str, Any]) -> str:
    """One plain-language line for the digest."""
    if ts.status == "no_news":
        return "no headlines collected yet"
    if ts.status == "warming_up":
        need = int(cfg["min_baseline_days"])
        return (
            f"building baseline ({min(ts.history_days, need)}/{need} days, "
            f"{ts.baseline_headlines} headlines)"
            + (
                f"; last {cfg['current_window_hours']}h: {ts.current_n} headlines"
                if ts.current_n
                else ""
            )
        )
    parts = [f"last {cfg['current_window_hours']}h: {ts.current_n} headlines"]
    if ts.current_mean is not None:
        parts.append(f"mean {ts.current_mean:+.2f} vs usual {ts.baseline_mean:+.2f}")
    if ts.z is not None:
        parts.append(f"z {ts.z:+.1f}")
    if ts.volume_ratio is not None:
        parts.append(f"volume {ts.volume_ratio:.1f}x usual")
    return ", ".join(parts)


def flag_text(signal: dict[str, Any]) -> str:
    d = signal["details"]
    weight = (
        ""
        if d.get("regime_multiplier", 1.0) == 1.0
        else f", {d['regime']} market weight x{d['regime_multiplier']:g}"
    )
    if signal["signal_type"] == "sentiment_shift":
        return (
            f"news sentiment turned {d['direction']}: {d['current_mean']:+.2f} vs its usual "
            f"{d['baseline_mean']:+.2f} (z {d['z']:+.1f}, "
            f"{d['current_headlines']} headlines{weight})"
        )
    return (
        f"news volume {d['ratio']:.1f}x usual ({d['current_headlines']} headlines in "
        f"{d['window_hours']}h{weight})"
    )
