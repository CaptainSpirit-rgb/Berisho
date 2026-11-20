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
market-intel digest --dry-run --no-llm   # force the free notes even if Claude is enabled
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

**By default the digest is free.** Sections 1–3 come from fixed rules: big or
market-beating moves, what each new 8-K is about, and key sentences quoted word for word
from the filing and its press release (fetched from SEC), e.g. the revenue and outlook
lines of an earnings release or who was appointed in an officer change.

**Optional, paid: Claude-written analysis.** Set `llm.enabled: true` in
`config/settings.yaml` and `ANTHROPIC_API_KEY` in `.env`, and Claude writes sections 1–3
instead (why it may matter, bull/bear case, what to check). It uses only the numbers and
filing text the app sends and cites its sources. One call per digest, cached so re-runs
are free; cost is logged and shown in the footer. Roughly $3/month on Opus, $1.50 on
Sonnet, $0.10 on Haiku (change `llm.model`). If a call fails, the free notes are used.

Not covered yet: news headlines. A move with no filing behind it is reported as such.

## News sentiment (Phase 3, thesis pipeline)

FinBERT (`ProsusAI/finbert`, as in the thesis) scores every collected headline on your
PC, for free: `sentiment_score` = P(positive) − P(negative), from −1 to +1.

```bash
pip install -e .[sentiment]              # torch + transformers; the model (~440 MB)
                                         # downloads from Hugging Face on first use
market-intel score-sentiment             # score new headlines, rebuild daily summary,
                                         # write sentiment signals
```

`run-daily` runs this step automatically after the collectors (and skips it, with a log
line, if FinBERT isn't installed). Following the thesis, the signal is not the raw level:

* **Sentiment shift**: mean score of the last 24 h vs the stock's own previous 60 days,
  as a z-score of the mean; flagged at |z| ≥ 2.
* **News volume spike**: headlines in the last 24 h at ≥ 3× the stock's usual daily count.
* In a bear market (SPY below its 200-day MA) both signals get half weight.

A stock needs 20 days of collection and 30 scored headlines before it can raise a signal;
until then the digest shows "building baseline (n/20 days)". Headlines published before
collection started are scored but don't count as history. All thresholds are under
`sentiment:` in `config/settings.yaml`. Per-day counts and means are in the
`sentiment_daily` table, signals in `signals`.

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
