"""Research desk Stage 1: portfolio, ticker selection, packets, validator, rendering,
storage and the Claude Code runner (with a fake `claude`, never the real one)."""

import datetime as dt
import json
import os
import stat
import sys

import pytest
import scenario
from scenario import NOW, WATCH, trading_days

from market_intel.config import Security
from market_intel.delivery import research as render
from market_intel.delivery.digest import build_digest
from market_intel.research import packet, runner, validate
from market_intel.research.portfolio import Position, load_portfolio, value_position
from market_intel.research.select import select_tickers
from market_intel.research.store import save_run

KO = Security("KO", "Coca-Cola", 21344, "XLP")
SECURITIES = [KO, *WATCH]
SETTINGS = {
    "benchmark": {"symbol": "SPY", "regime_ma_days": 200},
    "sentiment": {"enabled": False},
    "research": {"max_tickers": 3},
}


def load_series(con, symbol, closes):
    days = trading_days(len(closes))
    con.executemany(
        "INSERT OR REPLACE INTO prices_daily (symbol, trade_date, open, high, low, close, "
        "adj_close, volume, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'test')",
        [(symbol, d, c, c, c, c, c, 1_000_000) for d, c in zip(days, closes, strict=True)],
    )
    return days


@pytest.fixture
def db(con):
    scenario.build(con)
    # KO: 260 days from 100 rising linearly to 138.2 (+38.2%).
    n = 260
    load_series(con, "KO", [100 + 38.2 * i / (n - 1) for i in range(n)])
    return con


@pytest.fixture
def ko_position():
    days = trading_days(260)
    return Position("KO", 5.0, days[0], "bought", "Expected the stock to grow.")


# ------------------------------------------------------------------------- portfolio
def test_load_portfolio(tmp_path):
    p = tmp_path / "portfolio.yaml"
    p.write_text(
        "positions:\n"
        "  - {symbol: ko, invested_usd: 5, buy_date: 2026-01-12, thesis: 'grow'}\n"
        "  - {symbol: AAPL, invested_usd: 2.0, buy_date: '2026-09-30', source: gift}\n"
    )
    ko, aapl = load_portfolio(p)
    assert ko == Position("KO", 5.0, dt.date(2026, 1, 12), "bought", "grow")
    assert aapl.source == "gift" and aapl.thesis is None
    assert load_portfolio(tmp_path / "missing.yaml") == []


def test_value_position_vs_benchmark(db, ko_position):
    v = value_position(db, ko_position, dt.date(2026, 11, 19))
    assert v["return_pct"] == 38.2
    assert v["value_usd"] == 6.91
    assert v["benchmark"] == "SPY"
    # SPY over the same dates: 730 -> 690
    assert v["benchmark_return_pct"] == -5.5
    assert v["vs_benchmark_pts"] == 43.7


def test_value_position_without_prices(db):
    v = value_position(db, Position("XYZ", 1.0, dt.date(2026, 1, 1)), dt.date(2026, 11, 19))
    assert "value_usd" not in v and "collect-prices" in v["note"]


# ------------------------------------------------------------------------- selection
def test_selection_prefers_events_and_checks_every_holding(db):
    d = build_digest(db, SECURITIES, SETTINGS, NOW)
    picks = select_tickers(
        db,
        [s.symbol for s in SECURITIES],
        d.flags,
        [Position("AAPL", 2, dt.date(2026, 9, 30))],
        NOW,
        max_tickers=1,
    )
    assert [(p.symbol, p.depth) for p in picks] == [("NVDA", "full"), ("AAPL", "light")]
    assert any("8-K" in r for r in picks[0].reasons)
    assert picks[1].held and picks[1].reasons == ["holding: daily thesis check"]


def test_selection_rotates_on_a_quiet_day(db):
    picks = select_tickers(db, ["KO", "AAPL"], [], [], NOW, max_tickers=3)
    assert [(p.symbol, p.depth) for p in picks] == [("KO", "full")]
    db.execute(
        "INSERT INTO agent_reports (run_date, agent, ticker, report) "
        "VALUES ('2026-11-18', 'cio', 'KO', '{}')"
    )
    picks = select_tickers(db, ["KO", "AAPL"], [], [], NOW, max_tickers=3)
    assert picks[0].symbol == "AAPL"


# ---------------------------------------------------------------------------- packets
def _facts(con, cik):
    from screener_helpers import companyfacts

    from market_intel import screener
    from market_intel.screener import facts

    screener.init_schema(con)
    data = companyfacts(cik, [2024, 2025, 2026], lambda y, q: 1e9 * (1 + 0.1 * (y - 2024)))
    facts.upsert_facts(con, facts.parse_companyfacts(data))


def test_build_run_writes_packets_docs_and_manifest(db, tmp_path, ko_position):
    _facts(db, 1045810)
    run_dir = tmp_path / "2026-11-20"
    m = packet.build_run(db, SECURITIES, [ko_position], SETTINGS, run_dir, now=NOW)

    assert [(t["symbol"], t["depth"]) for t in m["tickers"]] == [
        ("NVDA", "full"),
        ("JPM", "full"),
        ("KO", "light"),
    ]
    assert m["portfolio"]["total_value_usd"] == 6.91
    assert m["rules"]["held_stances"] == packet.HELD_STANCES
    assert (run_dir / "index.json").exists()

    nvda = json.loads((run_dir / "packets" / "NVDA.json").read_text())
    assert nvda["meta"]["allowed_stances"] == packet.WATCH_STANCES
    stock = nvda["quant"]["stock"]
    assert stock["change_1d_pct"] == -14.0 and stock["last_close"] == 125.5
    assert nvda["quant"]["market_regime"]["regime"] == "bear"
    assert any("8-K" in f for f in nvda["quant"]["rule_flags"])
    filing = nvda["filings"][0]
    assert filing["source_id"] in nvda["sources"]
    doc = run_dir / filing["documents"][0]["text_file"]
    assert "revenue of $10.0 billion" in doc.read_text()
    f = nvda["fundamentals"]
    assert f["available"] and f["source_id"] in nvda["sources"]
    assert f["revenue_growth_ttm_pct"] == 9.3  # (1.1 + 3 x 1.2) / (1.0 + 3 x 1.1) - 1
    assert f["valuation"]["market_cap_usd_bn"] is not None

    ko = json.loads((run_dir / "packets" / "KO.json").read_text())
    assert ko["holding"]["thesis"] == "Expected the stock to grow."
    assert ko["holding"]["source_id"] == "portfolio:KO" and "portfolio:KO" in ko["sources"]
    assert ko["meta"]["allowed_stances"] == packet.HELD_STANCES
    assert ko["fundamentals"]["available"] is False
    assert ko["quant"]["stock"]["pct_vs_sma_200"] is not None


def test_headline_after_8k_shares_its_source_group(db, tmp_path):
    db.execute(
        "INSERT INTO news (news_id, symbol, published_at, source, headline, url) VALUES "
        "('n1', 'NVDA', '2026-11-18 08:00:00', 'Reuters', 'Nvidia revenue tops $10 billion', "
        "'https://x.test/1'), "
        "('n2', 'NVDA', '2026-11-12 08:00:00', 'Bloomberg', 'Nvidia old story', 'https://x.test/2')"
    )
    m = packet.build_run(db, SECURITIES, [], SETTINGS, tmp_path / "r", now=NOW)
    assert m["tickers"][0]["symbol"] == "NVDA"
    p = json.loads((tmp_path / "r" / "packets" / "NVDA.json").read_text())
    news = {n["source_id"]: n for n in p["news"]}
    assert news["news:n1"]["may_repeat_filing"] == "filing:0001045810-26-000088"
    assert p["sources"]["news:n1"]["group"] == "filing:0001045810-26-000088"
    assert news["news:n2"]["may_repeat_filing"] is None


# --------------------------------------------------------------------------- numbers
def test_extract_numbers_skips_dates_forms_and_counts():
    text = (
        "On 2026-11-17 (Nov 17, 2026) the 10-Q and 8-K Item 2.02 showed revenue of $10.0 "
        "billion, 38.2% above the 200-day average; 3 insiders sold in Q3 2026 and 1,250 shares."
    )
    nums = [(n.text, n.value) for n in validate.extract_numbers(text)]
    assert nums == [("$10.0 billion", 10e9), ("38.2%", 38.2), ("1,250", 1250.0)]


@pytest.mark.parametrize(
    "text,known,found",
    [
        ("38%", [38.2], True),
        ("38.5%", [38.2], False),
        ("$10 billion", [10e9], True),
        ("$47.1 billion", [47.06], True),  # the packet's *_usd_bn field
        ("25%", [0.25], True),  # a fraction in filing data
        ("down 14.0%", [-14.0], True),
        ("$999", [10e9, 38.2], False),
    ],
)
def test_number_found(text, known, found):
    (n,) = validate.extract_numbers(text)
    assert validate.number_found(n, known) is found


# --------------------------------------------------------------------------- validator
@pytest.fixture
def run(db, tmp_path, ko_position):
    run_dir = tmp_path / "2026-11-20"
    m = packet.build_run(db, SECURITIES, [ko_position], SETTINGS, run_dir, now=NOW)
    reports = run_dir / "reports"
    (reports / "fundamental_analyst_NVDA.json").write_text(
        json.dumps(
            {
                "agent": "fundamental_analyst",
                "ticker": "NVDA",
                "assessment": "unclear",
                "findings": [
                    {
                        "claim": "No financials in the packet",
                        "source_ids": ["prices:NVDA"],
                        "confidence": "high",
                        "materiality": "low",
                    }
                ],
                "risks": [],
                "missing_data": ["fundamentals"],
                "open_questions": [],
                "research_priority": "medium",
            }
        )
    )
    (reports / "news_filings_analyst_NVDA.json").write_text("not json")
    final = {
        "summary": "Bear market: SPY trades below its 200-day average.",
        "portfolio_note": "KO is up 38.2% since purchase.",
        "items": [
            {
                "ticker": "NVDA",
                "headline": "Fell 14.0% the day after results",
                "what_happened": "Shares closed at 125.5 after the 8-K.",
                "reasons": [
                    {
                        "text": "The press release reported revenue of $10.0 billion",
                        "source_ids": ["filing:0001045810-26-000088"],
                    },
                    {"text": "Closed 125.5, down 14.0% in a day", "source_ids": ["prices:NVDA"]},
                    {"text": "Revenue was $12.3 billion", "source_ids": ["prices:NVDA"]},
                    {"text": "Made up source", "source_ids": ["filing:nope"]},
                    {"text": "Allocate 5% of your portfolio here", "source_ids": ["prices:NVDA"]},
                ],
                "trend": {"text": "Below its moving averages", "source_ids": ["prices:NVDA"]},
                "bull": [],
                "bear": [
                    {
                        "text": "Outlook: revenue expected to decline",
                        "source_ids": ["filing:0001045810-26-000088"],
                    }
                ],
                "stance": "watch",
                "confidence": "medium",
                "horizon": "3-6 months",
                "change_view_if": "Guidance improves",
                "check_next": "Read the outlook",
                "thesis_check": {"status": "intact"},
            },
            {
                "ticker": "KO",
                "headline": "Up 38.2% since you bought it",
                "reasons": [
                    {"text": "Return of 38.2% since buying", "source_ids": ["portfolio:KO"]}
                ],
                "stance": "hold",
                "confidence": "high",
                "horizon": "6 months",
                "thesis_check": {
                    "thesis": "grow",
                    "status": "intact",
                    "evidence": [
                        {"text": "Beat SPY by 43.7 points", "source_ids": ["portfolio:KO"]}
                    ],
                },
            },
            {"ticker": "TSLA", "headline": "not in this run"},
        ],
    }
    (reports / "final.json").write_text(json.dumps(final))
    return run_dir, m


def test_validator_keeps_supported_claims_and_removes_the_rest(run):
    run_dir, _ = run
    final, validation = validate.validate_run(run_dir)
    s = validation["summary"]
    nvda, ko = final["items"]
    assert [c["text"] for c in nvda["reasons"]] == [
        "The press release reported revenue of $10.0 billion",
        "Closed 125.5, down 14.0% in a day",
    ]
    problems = " | ".join(i["problem"] for i in validation["issues"])
    assert "number(s) not in the data: $12.3 billion" in problems
    assert "no valid source" in problems
    assert "position sizing" in problems
    assert "report is not valid JSON" in problems
    assert "ticker not in this run" in problems
    # Two independent sources (the 8-K and prices), medium confidence: stance stands.
    assert nvda["stance"] == "watch"
    assert nvda["thesis_check"] is None  # not held
    # KO's reasons rest on one source only: stance withheld, thesis check kept.
    assert ko["stance"] is None and "1 independent source" in ko["stance_withheld_reason"]
    assert ko["thesis_check"]["status"] == "intact"
    assert ko["thesis_check"]["evidence"][0]["text"] == "Beat SPY by 43.7 points"
    assert ko["_sources"]["portfolio:KO"]["type"] == "portfolio"
    assert s["stances_given"] == 1 and s["stances_withheld"] == 1
    assert s["claims_removed"] == 3 and s["publishable"]
    assert final["portfolio_note"] == "KO is up 38.2% since purchase."
    assert (run_dir / "reports" / "final.validated.json").exists()


def test_validator_rejects_wrong_stance_vocabulary(run):
    run_dir, _ = run
    final = json.loads((run_dir / "reports" / "final.json").read_text())
    final["items"][0]["stance"] = "hold"  # NVDA isn't held
    (run_dir / "reports" / "final.json").write_text(json.dumps(final))
    out, validation = validate.validate_run(run_dir)
    assert out["items"][0]["stance"] is None
    assert "not allowed here" in out["items"][0]["stance_withheld_reason"]


def test_prose_with_unknown_number_is_marked(run):
    run_dir, _ = run
    final = json.loads((run_dir / "reports" / "final.json").read_text())
    final["summary"] = "The market fell 47.5% this week."
    (run_dir / "reports" / "final.json").write_text(json.dumps(final))
    out, _ = validate.validate_run(run_dir)
    assert out["summary"].endswith("[unverified: 47.5%]")


def test_missing_final_report_is_not_publishable(run):
    run_dir, _ = run
    (run_dir / "reports" / "final.json").unlink()
    final, validation = validate.validate_run(run_dir)
    assert final is None and not validation["summary"]["publishable"]


# ---------------------------------------------------------------- render and storage
def test_render_and_store(run, db):
    run_dir, m = run
    final, validation = validate.validate_run(run_dir)
    text = render.render(final, m["portfolio"], as_html=False)
    assert "NVDA · WATCH" in text and "KO · NO STANCE: MONITORING" in text
    assert 'Your reason: "grow" → INTACT' in text
    assert "KO $6.91 (+38.2% since" in text
    assert "Position sizing is your call" in text
    assert "[1]" in text and "https://www.sec.gov" in text
    html_text = render.render(final, m["portfolio"], as_html=True)
    assert "<b>NVDA · WATCH</b>" in html_text and '<a href="https://www.sec.gov' in html_text

    save_run(db, m, final, validation, "sent")
    assert db.execute("SELECT status FROM research_runs").fetchone()[0] == "sent"
    agents = db.execute("SELECT agent, ticker FROM agent_reports ORDER BY 1, 2").fetchall()
    assert ("cio", "KO") in agents and ("fundamental_analyst", "NVDA") in agents
    assert db.execute("SELECT ticker, stance, close_at_stance FROM stances").fetchall() == [
        ("NVDA", "watch", 125.5)
    ]
    # Re-running the day replaces it, and the CIO view feeds the next packet's history.
    save_run(db, m, final, validation, "sent")
    assert db.execute("SELECT count(*) FROM stances").fetchone()[0] == 1
    assert packet._previous_views(db, "NVDA", dt.date(2026, 11, 21))[0]["stance"] == "watch"


# ---------------------------------------------------------------------------- runner
def test_command_limits_tools_and_strips_the_api_key():
    cmd = runner.build_command("claude", "data/research/2026-11-20", {"max_turns": 5})
    tools = cmd[cmd.index("--allowedTools") + 1]
    assert "Write(./data/research/2026-11-20/reports/**)" in tools
    assert "Bash" not in tools
    assert cmd[cmd.index("--max-turns") + 1] == "5"
    env = runner.clean_env({"ANTHROPIC_API_KEY": "sk-test", "PATH": "/bin"})
    assert "ANTHROPIC_API_KEY" not in env and env["PATH"] == "/bin"


def test_run_agents_without_claude_installed(tmp_path):
    res = runner.run_agents(tmp_path, tmp_path, {"claude_command": "no-such-claude-xyz"})
    assert not res.ok and "isn't installed" in res.reason


@pytest.mark.skipif(sys.platform == "win32", reason="fake CLI is a shell script")
def test_run_agents_with_fake_claude(tmp_path, monkeypatch):
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text(
        "#!/bin/sh\n"
        'if [ -n "$ANTHROPIC_API_KEY" ]; then echo \'{"is_error": true, "result": "key"}\'; '
        "exit 1; fi\n"
        'echo \'{"is_error": false, "num_turns": 7, "duration_ms": 60000, '
        '"total_cost_usd": 0.5}\'\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-removed")
    run_dir = tmp_path / "data" / "research" / "2026-11-20"
    run_dir.mkdir(parents=True)
    res = runner.run_agents(run_dir, tmp_path, {"timeout_minutes": 1})
    assert res.ok, res.reason
    assert res.meta["num_turns"] == 7
    assert (run_dir / "claude_output.json").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="fake CLI is a shell script")
def test_research_run_end_to_end_dry_run(tmp_path, monkeypatch, capsys):
    """research-run: packets -> (fake) Claude Code writes final.json -> checks -> print."""
    from market_intel import cli, config
    from market_intel.storage import db as dbmod

    path = tmp_path / "data" / "test.duckdb"
    c = dbmod.connect(path)
    dbmod.init_schema(c)
    scenario.build(c)
    c.close()

    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    final = {
        "summary": "Bear market.",
        "items": [
            {
                "ticker": "NVDA",
                "headline": "Fell after results",
                "confidence": "low",
                "reasons": [{"text": "Down 14.0% in a day", "source_ids": ["prices:NVDA"]}],
                "stance": None,
                "stance_withheld_reason": "Only price data so far.",
            }
        ],
    }
    fake.write_text(
        "#!/bin/sh\n"
        "dir=$(echo \"$2\" | sed -n 's/.*run directory \\(.*\\)\\.$/\\1/p')\n"
        f"cat > \"$dir/reports/final.json\" <<'JSON'\n{json.dumps(final)}\nJSON\n"
        'echo \'{"is_error": false, "num_turns": 3}\'\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config, "load_watchlist", lambda path=None: list(WATCH))
    monkeypatch.setattr(config, "load_settings", lambda path=None: dict(SETTINGS))

    rc = cli.main(["research-run", "--dry-run", "--no-fetch", "--db", str(path)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "Research desk" in out and "NVDA · NO STANCE: MONITORING" in out
    assert "Down 14.0% in a day [1]" in out
    c = dbmod.connect(path)
    assert c.execute("SELECT status FROM research_runs").fetchone()[0] == "dry_run"
    c.close()
