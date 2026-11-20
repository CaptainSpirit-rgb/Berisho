import json
from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest
from scenario import NOW, NVDA_8K, WATCH, build

from market_intel import config
from market_intel.delivery import digest, render
from market_intel.llm import briefs

ANSWER = {
    "summary": "Bear market. NVDA fell 14% after results.",
    "items": [
        {
            "symbol": "NVDA",
            "headline": "Sold off after results",
            "what": "Revenue $10.0 billion; outlook says revenue will decline.",
            "why": "Growth expectations are being reset.",
            "bull": "Reset may be priced in.",
            "bear": "Further cuts possible.",
            "check_next": "Read the outlook section.",
            "sources": [NVDA_8K],
        }
    ],
    "background": ["AAPL: -3.8% over 5 days, in line with the market."],
}


class FakeMessages:
    def __init__(self, owner):
        self.owner = owner

    def create(self, **kwargs):
        self.owner.calls.append(kwargs)
        if self.owner.error:
            raise self.owner.error
        return SimpleNamespace(
            stop_reason=self.owner.stop_reason,
            model="claude-opus-5-5",
            content=[
                SimpleNamespace(type="thinking"),
                SimpleNamespace(type="text", text=self.owner.text),
            ],
            usage=SimpleNamespace(input_tokens=10_000, output_tokens=2_000),
        )


class FakeClient:
    def __init__(self, text=None, stop_reason="end_turn", error=None):
        text = json.dumps(ANSWER) if text is None else text
        self.text, self.stop_reason, self.error, self.calls = text, stop_reason, error, []
        self.messages = FakeMessages(self)
        self.beta = SimpleNamespace(messages=FakeMessages(self))


@pytest.fixture
def setup(con):
    build(con)
    settings = config.load_settings()
    return con, digest.build_digest(con, WATCH, settings, now=NOW), settings


def test_payload_has_numbers_and_filing_text(setup):
    con, d, settings = setup
    p = briefs.build_payload(con, d, settings)
    assert p["regime"]["regime"] == "bear" and p["market"]["symbol"] == "SPY"
    nvda = next(s for s in p["watchlist"] if s["symbol"] == "NVDA")
    assert nvda["change_1d_pct"] == -14.0 and nvda["volume_vs_20d_avg"] == 2.4
    assert nvda["change_5d_vs_market_pts"] is not None
    f = next(f for f in p["filings"] if f["symbol"] == "NVDA")
    assert f["items"] == "2.02 results of operations" and f["url"] == NVDA_8K
    assert "revenue expected to decline" in f["documents"][0]["text"]
    assert next(f for f in p["filings"] if f["symbol"] == "JPM")["documents"] == "text not fetched"
    assert p["largest_insider_trades"][0]["value_usd"] == 20_000_000
    json.dumps(p)  # serialisable


def test_long_filing_text_is_cut_and_marked(setup):
    con, d, settings = setup
    settings["llm"]["max_chars_per_document"] = 20
    doc = briefs.build_payload(con, d, settings)["filings"][0]["documents"][0]
    assert doc["text"].endswith("[...text cut for length]") and len(doc["text"]) < 60


def test_generate_calls_claude_once_then_uses_cache(setup):
    con, d, settings = setup
    client = FakeClient()
    a, reason = briefs.generate(con, d, settings, client=client)
    assert reason == "" and a.source == "claude" and not a.cached
    assert a.items[0].symbol == "NVDA" and a.items[0].sources == [NVDA_8K]
    assert a.cost_usd == pytest.approx(10_000 * 4e-6 + 2_000 * 20e-6)
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == "default"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "Use only numbers that appear in the data" in call["system"]
    row = con.execute("SELECT model, input_tokens, output_tokens, cost_usd FROM briefs").fetchone()
    assert row[:3] == ("claude-opus-5-5", 10_000, 2_000)

    a2, _ = briefs.generate(con, d, settings, client=client)
    assert len(client.calls) == 1 and a2.cached and a2.summary == a.summary
    text = render.render_text(d, a2)
    assert "Bull case: Reset may be priced in." in text and "cached, no cost" in text


def test_new_data_misses_the_cache(setup):
    con, d, settings = setup
    client = FakeClient()
    briefs.generate(con, d, settings, client=client)
    d.rows[0].close = 1.0
    briefs.generate(con, d, settings, client=client)
    assert len(client.calls) == 2


def test_missing_key_and_disabled(setup):
    con, d, settings = setup
    assert briefs.generate(con, d, settings) == (None, "ANTHROPIC_API_KEY is not set in .env")
    settings["llm"]["enabled"] = False
    assert briefs.generate(con, d, settings, client=FakeClient())[0] is None


def _status_error(cls, code):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("err", response=httpx.Response(code, request=req), body=None)


@pytest.mark.parametrize(
    "client, reason",
    [
        (FakeClient(stop_reason="refusal"), "Claude stopped early (refusal)"),
        (FakeClient(text="not json"), "Claude's answer wasn't valid JSON"),
        (
            FakeClient(error=_status_error(anthropic.InternalServerError, 500)),
            "Claude API error 500",
        ),
        (
            FakeClient(error=_status_error(anthropic.AuthenticationError, 401)),
            "the Claude API key was rejected",
        ),
    ],
)
def test_failures_return_reason_and_store_nothing(setup, client, reason):
    con, d, settings = setup
    assert briefs.generate(con, d, settings, client=client) == (None, reason)
    assert con.execute("SELECT count(*) FROM briefs").fetchone()[0] == 0


def test_server_fallback_can_be_turned_off(setup):
    con, d, settings = setup
    settings["llm"]["server_fallback"] = False
    client = FakeClient()
    briefs.generate(con, d, settings, client=client)
    assert "fallbacks" not in client.calls[0] and "betas" not in client.calls[0]
