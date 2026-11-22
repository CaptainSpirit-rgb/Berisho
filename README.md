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
market-intel collect-news                # Yahoo Finance headlines for the watchlist (free)
market-intel digest --dry-run            # print the digest from stored data
market-intel run-daily --dry-run         # collect everything, print the digest
market-intel run-daily                   # collect, send to Telegram, push the data file
market-intel export-data                 # only push today's data file
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

Headlines come from Yahoo Finance via yfinance (free, no key): the latest few per stock
are shown under each item and as "Latest headline" in the background lines.

## Daily Claude analysis (free, through the Claude subscription)

`run-daily` also writes the day's data (numbers, filing quotes, headlines, insider
trades and the digest text) as JSON and pushes it to the **`digest-data`** branch of this
repo (`daily/<date>.json` and `latest.json`). It uses git plumbing, so your working tree
and current branch are never touched; it needs only the git access you already have.
A scheduled Claude run in the project reads `latest.json` at 16:30 Tbilisi on weekdays
and writes the analysis in the Claude app. Turn the push off with `export.push: false`.

### Run it automatically at 16:00 on weekdays (Windows)

In PowerShell, adjusting the path to your virtualenv if it differs:

```powershell
schtasks /Create /TN "Berisho daily" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 16:00 `
  /TR "C:\Users\lukar\Berisho\.venv\Scripts\market-intel.exe run-daily"
```

The PC must be on (awake) at 16:00. Check a run with `schtasks /Run /TN "Berisho daily"`.

## Research desk: multi-agent analysis (Stage 1, free on the Claude subscription)

A second daily message, **"Research desk"**, written by a small team of Claude agents and
checked by Python. Design: [docs/MULTI_AGENT_BLUEPRINT.md](docs/MULTI_AGENT_BLUEPRINT.md).

```
market-intel research-run            packets -> agents -> checks -> Telegram
market-intel research-run --dry-run  same, but print instead of sending
```

What happens, step by step:

1. **Python picks the stocks** (`research-packets`): every holding in
   `config/portfolio.yaml` gets a thesis check, plus up to 3 watchlist stocks with
   something new (a flag in the digest, a new 10-Q/10-K), or one stock in rotation on a
   quiet day.
2. **Python builds a research packet per stock** in `data/research/<date>/`: price trend
   (vs 50/200-day averages, S&P 500 and sector fund), SEC financials (growth, margins,
   cash flow, debt, valuation), recent filings with their text, insider trades,
   headlines, your position and its return vs the S&P 500. Every fact has a source id.
3. **The agents** run in Claude Code, headless, logged in with your Claude subscription:
   the *Fundamental Analyst* and the *News & Filings Analyst* (`.claude/agents/`) each
   report on every stock independently; the *Chief Intelligence Officer*
   (`.claude/commands/research.md`) asks at most one follow-up per stock and writes the
   final report.
4. **Python checks it** (`research-validate`): every claim must cite a source from the
   packet and every number must appear in the data, or the claim is removed. A stance
   (Add / Hold / Trim / Review to sell, or Consider buying / Watch / Avoid) is kept only
   with at least medium confidence and two independent sources; otherwise it says
   "No stance: monitoring". No position sizes or price targets, ever.
5. **Telegram** (`research-send`), and everything is stored in DuckDB (`research_runs`,
   `agent_reports`, and `stances` for the future scorecard).

If the agents fail (Claude Code missing, usage limit, timeout), Telegram gets a one-line
"Research desk skipped" note; the normal digest never depends on the agents.

**Cost: free, inside your plan's usage limits.** Nothing calls the paid API. Claude Code
would bill the API if `ANTHROPIC_API_KEY` were set, so `research-run` removes it from
the agents' environment. Claude Code prints an "API-equivalent cost" in the log; on a
subscription that isn't charged. Watch how much of your usage limit a run takes in the
first week and lower `research.max_tickers` if needed.

### One-time setup on the PC

```powershell
git pull
pip install -e ".[dev]"
copy config\portfolio.example.yaml config\portfolio.yaml   # then put your positions in it
# Install Claude Code (https://claude.com/claude-code), run `claude` once and log in
# with your Claude subscription (not an API key).
market-intel run-daily --dry-run      # also collects the sector-fund prices now
market-intel research-run --dry-run   # first full run, printed only (takes a few minutes)
```

Then schedule it 15 minutes after the daily run:

```powershell
schtasks /Create /TN "Berisho research" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 16:15 `
  /TR "C:\Users\lukar\Berisho\.venv\Scripts\market-intel.exe research-run"
```

Run the steps one at a time when you want to look inside: `research-packets`, then
`claude` and `/research data/research/<date>` interactively, then `research-validate`
and `research-send --dry-run`. Settings are under `research:` in `config/settings.yaml`.

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

## Weekly screener: market overview + new candidates (Phase 2)

Once a week: a **market overview** (S&P 500, Nasdaq 100, Dow and Russell 2000 via SPY, QQQ,
DIA and IWM, plus the 11 sector funds: 1-week, 1-month, 3-month and year-to-date change,
and whether each is above its 200-day average), then the **S&P 500 companies** that look
best on four things at once: **growth** (12-month sales growth, and whether it is speeding
up), **quality** (operating and free-cash-flow margins, margin trend), **balance sheet**
(net debt vs EBITDA) and **price** (earnings yield, FCF yield, EV/sales vs its GICS sector).
Each pick comes with plain-language reasons and warnings. Everything is free: the member
list and sectors from Wikipedia's S&P 500 page, financials from SEC, prices from Yahoo.
Financials and Real Estate are left out because bank/insurer/REIT accounts differ.

```bash
market-intel screen-collect              # S&P 500 list + SEC financials (~500 calls, a few minutes)
market-intel screen-prices               # ~400 days of prices for members, index and sector funds
market-intel screen --dry-run            # print the market overview and ranked candidates
market-intel run-weekly                  # all of it, sent to Telegram
```

Thresholds, weights and the funds in the overview live under `screener:` in
`config/settings.yaml`. `screener.universe: all_listed` switches back to screening every
NYSE/Nasdaq stock (uses SEC's 1 GB bulk file; add `min_market_cap`/`max_market_cap`).

### Backtest before trusting it

```bash
market-intel screen-prices --start 2014-06-01   # ~10 years of prices for the universe (slow)
market-intel screen-backtest                    # report in data/backtests/
```

Every quarter since 2015 the screen is replayed using only financials **filed** by that
date (not period end), and the top 10 are compared with the average of every company that
passed the filters, and with SPY. Each pillar is also tested alone. Known bias: only
today's S&P 500 members are known (past lists aren't free), so companies that shrank out of
the index, went bust or were taken over are missing and absolute returns look better than
reality. The excess over the (equally biased) universe is the fairer number.

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
| `src/market_intel/screener/` | Weekly fundamentals screen and its point-in-time backtest |
| `src/market_intel/journal/` | Decision journal and outcome evaluation |
| `backtests/`, `notebooks/` | Screen backtests, exploration |
