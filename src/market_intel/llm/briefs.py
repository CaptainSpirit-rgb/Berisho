"""Claude-written analysis for the digest.

One API call per digest. The app sends the numbers it computed (prices, moves vs the
market, regime, filings with their text, insider trades) as JSON and asks for a short
summary, a few items worth attention (what happened, why it may matter, bull and bear
case, what to check next, with source links) and background notes. Claude only writes
text; it is told to use only the numbers given and to say when something is unknown.

The answer is cached in ``briefs`` keyed by a hash of the model, prompt version and the
input data, so re-running the digest on the same data costs nothing. Token usage and
cost are stored with every call. Any failure (no key, API error, refusal, bad JSON)
returns ``None`` and the digest falls back to rule-based notes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
from typing import Any

import duckdb

from market_intel.collectors.filing_text import documents_for
from market_intel.delivery.analysis import Analysis, relative_5d
from market_intel.delivery.digest import Digest, describe_items

log = logging.getLogger(__name__)

PROMPT_VERSION = "2026-11-21.1"
DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """\
You write the analysis section of a personal daily stock digest for one private investor.
They hold stocks for months, not days, and use the digest as an attention filter: it
should tell them in a minute what is worth their time today and why, and leave the
decision to them. This is decision support, never advice: don't tell them to buy, sell
or hold, and don't predict prices.

You get a JSON object with everything the app knows today: the market regime (S&P 500
ETF vs its 200-day average), each watchlist stock's close, 1-day and 5-day change,
volume vs its 20-day average, its 5-day change relative to the market, recent SEC
filings (8-K item codes, and for 8-Ks the text of the filing and its press-release
exhibits), the app's rule-based flags, and insider open-market trades.

Rules:
- Use only numbers that appear in the data. Never invent or estimate figures, dates,
  names or events. If the data doesn't explain something, say so plainly (for example
  "no filing explains this; check the news").
- Every item must cite the URL of each filing it relies on in "sources" (copy the URL
  exactly from the data). Items based on price data alone have empty sources.
- Separate a stock-specific move from the market: compare with the market's move.
- Insider selling is weak evidence on its own (taxes, diversification, planned
  programs); insider buying is stronger. Don't assume a trade was pre-planned unless the
  data says so.
- Plain, concrete language for someone who knows finance but has no time. No hype,
  no filler, no hedging boilerplate. Short sentences.

Output (JSON matching the schema):
- summary: 2-4 sentences. What matters today and why, starting with the regime in plain
  words.
- items: the stocks worth attention today, most important first, at most {max_items}.
  Only include a stock if something happened (a large or market-beating move, a new
  filing, insider buying, a flag). An empty list is fine on a quiet day. For each:
  headline (one line), what (1-2 sentences: what happened, with the key numbers from
  the filing if there is one), why (why it may matter for a months-long holder),
  bull (one sentence), bear (one sentence), check_next (one concrete thing to read or
  verify), sources (filing URLs).
- background: one short line per remaining watchlist stock (and one line on insider
  selling if there is any), e.g. "AAPL: down 3.8% over 5 days, in line with the market;
  nothing filed."
"""

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "headline": {"type": "string"},
                    "what": {"type": "string"},
                    "why": {"type": "string"},
                    "bull": {"type": "string"},
                    "bear": {"type": "string"},
                    "check_next": {"type": "string"},
                    "sources": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "symbol",
                    "headline",
                    "what",
                    "why",
                    "bull",
                    "bear",
                    "check_next",
                    "sources",
                ],
                "additionalProperties": False,
            },
        },
        "background": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "items", "background"],
    "additionalProperties": False,
}


def _r(v: float | None, n: int = 2) -> float | None:
    return round(v, n) if v is not None else None


def build_payload(
    con: duckdb.DuckDBPyConnection, d: Digest, settings: dict[str, Any]
) -> dict[str, Any]:
    """Everything Claude sees, as plain JSON. Deterministic for the same data (no run
    time in it), so it doubles as the cache key."""
    llm = settings.get("llm", {})
    doc_chars = int(llm.get("max_chars_per_document", 15000))
    filing_chars = int(llm.get("max_chars_per_filing", 30000))

    r = d.regime or {}
    regime = (
        {
            "benchmark": r["benchmark"],
            "regime": r["regime"],
            "close": _r(r["close"]),
            "moving_average": _r(r["ma"]),
            "moving_average_days": r["ma_days"],
            "pct_vs_average": _r(100 * (r["close"] / r["ma"] - 1), 1),
            "as_of": str(r["as_of"]),
        }
        if "regime" in r
        else {"regime": "unknown", "note": "not enough benchmark history"}
    )
    market = d.market
    stocks = [
        {
            "symbol": row.symbol,
            "name": d.names.get(row.symbol, ""),
            "last_bar": str(row.trade_date) if row.trade_date else None,
            "close": _r(row.close),
            "change_1d_pct": _r(row.change_1d_pct, 1),
            "change_5d_pct": _r(row.change_5d_pct, 1),
            "change_5d_vs_market_pts": _r(relative_5d(row, market), 1),
            "opening_gap_pct": _r(row.gap_pct, 1),
            "volume_vs_20d_avg": _r(row.volume_ratio, 1),
        }
        for row in d.rows
    ]
    filings = []
    for f in d.filings:
        entry: dict[str, Any] = {
            "symbol": f["symbol"],
            "form": f["form_type"],
            "accepted_utc": str(f["accepted_at"] or f["filed_date"]),
            "items": describe_items(f["items"]) if f["form_type"].startswith("8-K") else "",
            "url": f["url"],
        }
        if f["form_type"].startswith("8-K"):
            docs, budget = [], filing_chars
            for doc in documents_for(con, f["accession_no"]):
                text = doc["text"][: min(doc_chars, budget)]
                if not text:
                    break
                cut = len(text) < len(doc["text"])
                docs.append(
                    {
                        "type": doc["doc_type"],
                        "url": doc["url"],
                        "text": text + (" [...text cut for length]" if cut else ""),
                    }
                )
                budget -= len(text)
            entry["documents"] = docs or "text not fetched"
        filings.append(entry)

    return {
        "data_as_of": str(d.as_of) if d.as_of else None,
        "regime": regime,
        "market": {
            "symbol": market.symbol,
            "change_1d_pct": _r(market.change_1d_pct, 1),
            "change_5d_pct": _r(market.change_5d_pct, 1),
        }
        if market
        else None,
        "watchlist": stocks,
        "rule_flags": [{"symbol": s, "flag": t} for s, t in d.flags],
        "filings_last_days": d.windows.get("filings"),
        "filings": filings,
        "insider_window_days": d.windows.get("insiders"),
        "insider_summary": [
            {
                "symbol": i["symbol"],
                "buyers": i["buyers"],
                "buy_value_usd": _r(i["buy_value"] or 0, 0),
                "sellers": i["sellers"],
                "sell_value_usd": _r(i["sell_value"] or 0, 0),
            }
            for i in d.insiders
        ],
        "largest_insider_trades": [
            {
                "symbol": t["symbol"],
                "insider": t["insider"],
                "title": t["title"],
                "type": "buy" if t["code"] == "P" else "sell",
                "date": str(t["date"]),
                "value_usd": _r(t["value"], 0),
            }
            for t in d.insider_trades
        ],
    }


def cache_key(model: str, payload: dict[str, Any]) -> str:
    blob = json.dumps(
        {"model": model, "prompt": PROMPT_VERSION, "data": payload}, sort_keys=True, default=str
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def cost_usd(usage: Any, prices: dict[str, Any]) -> float:
    def n(name: str) -> int:
        return getattr(usage, name, 0) or 0

    per = 1e-6
    return (
        n("input_tokens") * float(prices.get("input", 4.0)) * per
        + n("cache_creation_input_tokens") * float(prices.get("cache_write", 5.0)) * per
        + n("cache_read_input_tokens") * float(prices.get("cache_read", 0.2)) * per
        + n("output_tokens") * float(prices.get("output", 20.0)) * per
    )


def _call_claude(client: Any, model: str, payload: dict[str, Any], settings: dict) -> Any:
    llm = settings.get("llm", {})
    max_items = int(settings.get("digest", {}).get("max_items", 5))
    kwargs: dict[str, Any] = dict(
        model=model,
        max_tokens=int(llm.get("max_tokens", 16000)),
        system=SYSTEM_PROMPT.format(max_items=max_items),
        messages=[
            {
                "role": "user",
                "content": "Today's data:\n" + json.dumps(payload, indent=1, default=str),
            }
        ],
        output_config={
            "effort": llm.get("effort", "medium"),
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
    )
    if llm.get("server_fallback", True):
        # If the model declines, the API re-runs the request on a fallback model.
        return client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
    return client.messages.create(**kwargs)


def generate(
    con: duckdb.DuckDBPyConnection,
    d: Digest,
    settings: dict[str, Any],
    client: Any | None = None,
) -> tuple[Analysis | None, str]:
    """Claude's analysis for this digest, from cache when the data hasn't changed.
    Returns (analysis, "") or (None, reason it wasn't available)."""
    llm = settings.get("llm", {})
    if not llm.get("enabled", True):
        return None, "Claude briefs are turned off in settings"
    model = llm.get("model", DEFAULT_MODEL)
    payload = build_payload(con, d, settings)
    key = cache_key(model, payload)

    hit = con.execute(
        "SELECT content, model, cost_usd FROM briefs WHERE brief_id = ?", [key]
    ).fetchone()
    if hit:
        log.info("Using cached Claude brief %s", key[:12])
        return Analysis.from_dict(
            json.loads(hit[0]), source="claude", model=hit[1], cost_usd=0.0, cached=True
        ), ""

    if client is None:
        if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
            return None, "ANTHROPIC_API_KEY is not set in .env"
        import anthropic

        client = anthropic.Anthropic()

    import anthropic

    try:
        resp = _call_claude(client, model, payload, settings)
    except anthropic.AuthenticationError:
        return None, "the Claude API key was rejected"
    except anthropic.RateLimitError:
        return None, "Claude API rate limit"
    except anthropic.APIStatusError as exc:
        log.error("Claude API error %s: %s", exc.status_code, exc.message)
        return None, f"Claude API error {exc.status_code}"
    except anthropic.APIConnectionError:
        return None, "could not reach the Claude API"

    if resp.stop_reason in ("refusal", "max_tokens"):
        return None, f"Claude stopped early ({resp.stop_reason})"
    text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
    try:
        data = json.loads(text)
    except ValueError:
        return None, "Claude's answer wasn't valid JSON"

    cost = cost_usd(resp.usage, llm.get("price_per_mtok", {}))
    used_model = getattr(resp, "model", model) or model
    log.info(
        "Claude brief: %s, %s in / %s out tokens, $%.4f",
        used_model,
        getattr(resp.usage, "input_tokens", "?"),
        getattr(resp.usage, "output_tokens", "?"),
        cost,
    )
    sources = sorted({s for i in data.get("items", []) for s in i.get("sources", [])})
    con.execute(
        "INSERT OR REPLACE INTO briefs (brief_id, run_date, symbol, model, content, sources, "
        "input_tokens, output_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            key,
            d.as_of or dt.date.today(),
            "*digest*",
            used_model,
            json.dumps(data),
            json.dumps(sources),
            getattr(resp.usage, "input_tokens", None),
            getattr(resp.usage, "output_tokens", None),
            cost,
        ],
    )
    return Analysis.from_dict(data, source="claude", model=used_model, cost_usd=cost), ""
