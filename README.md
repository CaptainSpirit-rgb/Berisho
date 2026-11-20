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

## Daily run (Phase 1)

Set `SEC_USER_AGENT` in `.env` first (e.g. `market-intel you@example.com`); SEC rejects
requests without a contact email.

```bash
market-intel collect-prices              # yfinance daily bars for the watchlist + SPY
market-intel collect-filings             # EDGAR filings, Form 4 trades, recent 8-K text
market-intel digest --dry-run            # print the digest from stored data
market-intel run-daily --dry-run         # all three in one go, printed to the console
market-intel run-daily                   # all three, digest sent to Telegram
market-intel digest --dry-run --no-llm   # skip Claude: rule-based notes, no API cost
```

Collectors are incremental and idempotent: re-running upserts by natural key. The first
price run pulls ~400 days so the 200-day regime MA has history. The digest flags price gaps,
volume spikes, fresh 8-Ks, 13D filings and insider cluster buys using thresholds from
`config/settings.yaml`, and shows times in Tbilisi time.

## What the digest contains

1. **Today in brief**: 2–4 sentences on what matters today, starting with the market regime.
2. **What matters**: the few stocks where something happened (a big or market-beating move,
   a new 8-K, insider buying), each with what happened, why it may matter, bull and bear
   case, what to check next, and links to the filings used.
3. **Background**: one line for every other watchlist stock, and insider selling.
4. **Numbers, filings, insider totals** as reference, in a table narrow enough for a phone.

Sections 1–3 are written by Claude when `ANTHROPIC_API_KEY` is set in `.env`. The app
sends Claude the numbers it computed plus the text of recent 8-Ks and their press releases
(fetched from SEC), and Claude must use only those numbers and cite its sources. It is one
call per digest (model and effort in `config/settings.yaml`, section `llm`), cached in the
`briefs` table so re-running on the same data is free; tokens and cost are logged and
stored per call. Expect roughly $0.10–0.15 per digest; the footer shows the actual cost.
If the key is missing or the call fails, the digest still goes out with plainer
rule-based notes, and the footer says why Claude wasn't used.

Not covered yet: news headlines. A move with no filing behind it is reported as such.

## Telegram setup

The digest is sent by your own Telegram bot to your own chat. One-time setup:

1. In Telegram, open a chat with **@BotFather** (the official one, with the blue check).
2. Send `/newbot`, pick a display name (e.g. `Berisho Digest`) and a username ending in `bot`
   (e.g. `berisho_digest_bot`). BotFather replies with a token like `123456789:AAH...`.
3. Put it in `.env` as `TELEGRAM_BOT_TOKEN=...`. Treat it like a password: anyone with it can
   post as your bot. If it leaks, send `/revoke` to BotFather for a new one.
4. Open your new bot in Telegram (BotFather links it), press **Start**, and send it any message.
5. Run `market-intel telegram-chat-id`. It prints a line like `TELEGRAM_CHAT_ID=123456789`;
   copy it into `.env`.
6. Run `market-intel telegram-test`. A test message should arrive in Telegram.

After that, `market-intel run-daily` sends the digest. If sending fails (missing settings,
network error), the digest is printed to the console instead and the command exits with 1.
The digest uses Telegram formatting (bold headings, links); long digests are split into
several messages at section boundaries to fit Telegram's 4,096-character limit.

## Layout

| Path | Purpose |
|---|---|
| `config/watchlist.yaml` | Tickers to track (KO, AAPL, NVDA, MSFT, JPM for now) |
| `config/settings.yaml` | Thresholds, weights, schedule |
| `src/market_intel/storage/schema.sql` | DuckDB schema (raw, derived, journal tables) |
| `src/market_intel/collectors/` | Prices, EDGAR, news, macro (Phase 1) |
| `src/market_intel/signals/` | Event, fundamental, sentiment, regime detectors |
| `src/market_intel/scoring/` | Ranking with explainable contributions |
| `src/market_intel/llm/` | Claude briefs and prompts |
| `src/market_intel/delivery/` | Digest, Telegram, email |
| `src/market_intel/journal/` | Decision journal and outcome evaluation |
| `backtests/`, `notebooks/` | Screen backtests, exploration |
