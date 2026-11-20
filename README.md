# market-intel (Berisho)

A personal market intelligence app: it watches a set of US stocks, flags events that may start
meaningful moves (filings, insider buys, price/volume anomalies, news), explains them with Claude,
and sends a short daily digest. **Decision support only: no automated trading, not financial advice.**

The full plan, background and conventions are in [CLAUDE.md](CLAUDE.md).

## Setup

```bash
uv venv && source .venv/bin/activate     # or: python -m venv .venv
uv pip install -e ".[dev]"               # or: pip install -e ".[dev]"
cp .env.example .env                     # then fill in keys (never commit .env)
market-intel init-db                     # creates data/market_intel.duckdb
market-intel watchlist
pytest
```

## Layout

| Path | Purpose |
|---|---|
| `config/watchlist.yaml` | Tickers to track (5-ticker test list for now) |
| `config/settings.yaml` | Thresholds, weights, schedule |
| `src/market_intel/storage/schema.sql` | DuckDB schema (raw, derived, journal tables) |
| `src/market_intel/collectors/` | Prices, EDGAR, news, macro (Phase 1) |
| `src/market_intel/signals/` | Event, fundamental, sentiment, regime detectors |
| `src/market_intel/scoring/` | Ranking with explainable contributions |
| `src/market_intel/llm/` | Claude briefs and prompts |
| `src/market_intel/delivery/` | Digest, Telegram, email |
| `src/market_intel/journal/` | Decision journal and outcome evaluation |
| `backtests/`, `notebooks/` | Screen backtests, exploration |
