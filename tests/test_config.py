from market_intel import cli, config


def test_watchlist_has_five_unique_tickers():
    securities = config.load_watchlist()
    symbols = [s.symbol for s in securities]
    assert len(symbols) == 5
    assert len(set(symbols)) == 5
    assert all(s.cik for s in securities), "every test ticker needs a CIK for EDGAR"


def test_duplicate_symbols_rejected(tmp_path):
    path = tmp_path / "wl.yaml"
    path.write_text("tickers:\n  - {symbol: aapl}\n  - {symbol: AAPL}\n")
    try:
        config.load_watchlist(path)
    except ValueError:
        return
    raise AssertionError("expected ValueError for duplicate symbols")


def test_settings_load():
    settings = config.load_settings()
    assert settings["benchmark"]["regime_ma_days"] == 200
    assert settings["signals"]["volume_spike_multiple"] > 0


def test_env_example_has_no_secret_values():
    text = (config.REPO_ROOT / ".env.example").read_text()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key.endswith(("_KEY", "_TOKEN", "_ID")):
            assert value == "", f"{key} must be empty in .env.example"


def test_db_path_relative_to_repo(monkeypatch):
    monkeypatch.setenv("MARKET_INTEL_DB_PATH", "data/x.duckdb")
    assert config.db_path() == config.REPO_ROOT / "data" / "x.duckdb"


def test_cli_init_db(tmp_path, capsys):
    path = tmp_path / "cli.duckdb"
    assert cli.main(["init-db", "--db", str(path)]) == 0
    assert path.exists()
    assert cli.main(["watchlist"]) == 0
    assert "AAPL" in capsys.readouterr().out


def test_cli_run_daily_dry_run_offline(tmp_path, monkeypatch, capsys):
    import datetime as dt
    import json
    from pathlib import Path

    from market_intel.collectors import edgar, prices

    today = dt.datetime.now(dt.UTC).date()
    real_collect = prices.collect_prices

    def fake_fetch(symbol, start, end):
        return [prices.PriceBar(symbol, today, 10.0, 10.0, 10.0, 10.0, 10.0, 1000)]

    monkeypatch.setattr(
        prices, "collect_prices", lambda con, symbols, **kw: real_collect(con, symbols, fake_fetch)
    )
    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "submissions_sample.json").read_text()
    )

    class FakeSec:
        def __init__(self, **kwargs):
            pass

        def get_json(self, url):
            return payload if url.endswith("CIK0000320193.json") else {}

        def get_text(self, url):
            return (Path(__file__).parent / "fixtures" / "form4_sample.xml").read_text()

    monkeypatch.setattr(edgar, "SecClient", FakeSec)
    path = tmp_path / "daily.duckdb"
    assert cli.main(["run-daily", "--dry-run", "--db", str(path)]) == 0
    out = capsys.readouterr().out
    assert "MARKET DIGEST" in out and "TODAY IN BRIEF" in out and "Notes from fixed rules" in out
    for symbol in ("KO", "AAPL", "NVDA", "MSFT", "JPM"):
        assert symbol in out
