# Berisho: project summary

*Last updated: 22 November 2026*

Berisho (`market-intel`) is a personal market intelligence app. It watches a handful of US
stocks, flags events that may matter, explains them in plain language and sends a daily
Telegram digest. It is **decision support only**: no automated trading, not financial advice.

The idea comes from Luka's bachelor thesis: headline sentiment alone is a weak price
predictor, so the app works as an *attention filter and research assistant*, not a price
prediction machine. The full original plan is in [CLAUDE.md](../CLAUDE.md).

---

## 1. Goal and ground rules

| Topic | Decision |
|---|---|
| Purpose | Surface the few events worth attention, explain why, leave the decision to Luka |
| Holding horizon | Months (fundamentals-driven), with alerts only for strong events |
| Market | US-listed stocks only (SEC EDGAR is free and official) |
| Focus | **S&P 500 and the major indexes** (changed on 21 Nov 2026 from ~6,000 small/mid caps) |
| Cost | **Free to run.** No paid APIs, including the Claude API (it's optional and off) |
| Delivery | Telegram bot, weekdays |
| Where it runs | Luka's Windows PC, `C:\Users\lukar\Berisho` (the cloud can't reach Yahoo, SEC or Telegram) |
| Stack | Python, DuckDB (one local file), yfinance, SEC EDGAR, FinBERT |

**Watchlist:** KO, AAPL, NVDA (Luka's picks), MSFT, JPM (placeholders, swap freely in
`config/watchlist.yaml`).

---

## 2. What has been built

All of the following is merged into `main` unless noted.

| # | What | PR |
|---|---|---|
| 1 | Repo scaffold: package layout, DuckDB schema, config, tests | #1 |
| 2 | Price collector (yfinance) and SEC EDGAR collector (filings, Form 4 insider trades), dry-run console digest | #2 |
| 3 | EDGAR fixes: correct acceptance times, drop filings *about other companies* (e.g. JPM's stake filings) | #3, #4 |
| 4 | Telegram delivery + watchlist set to KO, AAPL, NVDA, MSFT, JPM | #5 |
| 5 | Digest layout with analysis; **free by default** (rule-based notes quoting 8-K press releases), optional paid Claude path | #6 |
| 6 | FinBERT news sentiment (thesis pipeline): baseline-shift and news-volume signals | #7 |
| 7 | Weekly screener with a point-in-time backtest | #8 |
| 8 | Yahoo headlines collector; `run-daily` pushes a daily data file for the Claude analysis | #9 |
| 9 | Digest polish: no filing boilerplate, no duplicate headlines, clear sentiment status | #10 |
| 10 | Screener switched to the **S&P 500** + weekly index/sector market overview | #11 |
| 11 | One combined daily Telegram message: digest + sentiment + screener candidates | **#12, open, not merged yet** |

### How the pieces fit

```
Collectors ─► DuckDB ─► Signals ─► Digest ─► Telegram
 prices        raw tables   gaps, volume,    daily message    + daily data file
 SEC filings                8-Ks, insiders,                    (digest-data branch)
 Form 4                     sentiment shift,                        │
 news                       regime (SPY vs 200-day)                 ▼
                                                        Scheduled Claude analysis
Weekly: S&P 500 screener + market overview ─► Telegram     (16:33 Tbilisi, weekdays)
```

### Components in a sentence each

- **Prices:** daily bars for the watchlist and SPY from yfinance (~400 days so the 200-day average works).
- **SEC EDGAR:** new filings (8-K, 10-Q/10-K, 13D/13G), Form 4 insider trades, and the text of recent 8-Ks and press releases.
- **News:** latest Yahoo Finance headlines per stock (free, about 10 per ticker).
- **Market regime:** bull or bear from SPY above or below its 200-day average (the thesis rule).
- **Signals:** price gaps, volume spikes, fresh 8-Ks, 13D filings, insider cluster buys. Thresholds live in `config/settings.yaml`.
- **Sentiment (FinBERT):** scores each headline on Luka's PC. Signals are a *shift from the stock's own 60-day baseline* (z ≥ 2) and a *news volume spike* (≥ 3× normal), halved in bear markets. Each stock needs 20 days of history before it can raise a signal.
- **Digest:** Today in brief → What matters (what happened, why, bull/bear case, what to check) → Background → numbers, filings, insiders. Times shown in Tbilisi time.
- **Weekly screener:** S&P 500 companies ranked on growth, quality, balance sheet and price (vs their sector), with reasons and warnings. Financials and Real Estate are excluded. Opens with an overview of SPY, QQQ, DIA, IWM and the 11 sector funds.
- **Backtest:** replays the screen quarterly since 2015 using only data *filed* by each date. Caveat: survivorship bias (only today's S&P 500 members are known).
- **Scheduled Claude analysis:** `run-daily` pushes `latest.json` to the `digest-data` branch; a scheduled Claude run at **16:33 Tbilisi on weekdays** (first run Mon 23 Nov 2026) reads it and writes the analysis in the Claude app, using Luka's subscription (free).

---

## 3. Status and results so far

- **First real digest sent (20 Nov 2026)** from Luka's PC to Telegram. Regime was BEAR (SPY 689.81 vs 200-day 730.29), nothing flagged, NVDA earnings 8-K and a JPM officer-change 8-K listed, insider activity only sales (AAPL $35.1M, KO $16.5M, MSFT $6.6M).
- **Not yet tested on real data:** the S&P 500 screener and backtest, FinBERT with the real model, the combined daily message (#12), the daily data-file push.
- No CI on the repo; tests run locally with `pytest` (139 pass on the #12 branch).

---

## 4. Open items

1. **Review and merge PR #12** (combined daily message).
2. **Run the S&P 500 screener on the PC** and report the output (the "Find new undervalued stocks" thread is waiting on this):
   `market-intel screen-collect`, `market-intel screen-prices`, `market-intel screen --dry-run`.
3. **Set up Windows Task Scheduler** so `run-daily` runs at 16:00 on weekdays (needed before the 16:33 Claude analysis has fresh data). Command below.
4. Install FinBERT on the PC (`pip install -e .[sentiment]`) so sentiment baselines start building.
5. Optional later: run the backtest; swap MSFT/JPM for tickers Luka actually follows; sentiment for screener candidates; a decision journal and outcome tracking (Phase 4).
6. Declined for now: a same-day "big move" flag (e.g. NVDA −14% wasn't flagged because only gaps and volume spikes count).

---

## 5. How to run it (on the Windows PC)

**One-time setup**

```bash
python -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"
pip install -e ".[sentiment]"          # optional: FinBERT (~440 MB model)
copy .env.example .env                 # set SEC_USER_AGENT, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
market-intel init-db
market-intel telegram-chat-id          # then telegram-test
```

**Daily**

```bash
market-intel run-daily --dry-run       # collect everything and print the digest
market-intel run-daily                 # collect, send to Telegram, push the data file
```

**Weekly**

```bash
market-intel run-weekly                # market overview + S&P 500 candidates to Telegram
```

**Automatic daily run at 16:00 (PowerShell)**

```powershell
schtasks /Create /TN "Berisho daily" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 16:00 `
  /TR "C:\Users\lukar\Berisho\.venv\Scripts\market-intel.exe run-daily"
```

More detail on every command is in the [README](../README.md).
