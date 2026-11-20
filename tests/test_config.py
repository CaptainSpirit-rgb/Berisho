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
