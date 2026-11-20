# Personal Market Intelligence App — Project Context

> Handoff document from a planning conversation in Claude chat. Put it in the repo root
> (or rename it to `CLAUDE.md`) so Claude Code starts each session with this context.

---

## 1. Who I am

- **Luka Berishvili**, Data Analyst at TBC Bank (Retail Lending), based in Georgia.
- Studying Management with a Computer Science minor at Kutaisi International University.
- Day-to-day stack: **SQL (Dremio), PySpark/Databricks, Delta Lake, Power BI**. Comfortable with Python, data modelling (SCD2, semantic layers) and pipeline engineering (backfills, smoke tests, safety switches).
- I'm interested in investing in stocks but **don't have time to follow every news item and market move**.
- I'm also using this project to get good at Claude Code and agentic workflows.

## 2. Where this comes from: my bachelor thesis

**Title:** *Does Market Regime Influence Predictability? News Sentiment and Short-Term Stock Return Forecasting in Bull and Bear Markets* (defended, graded 95/100).

**Setup**
- FinBERT (`ProsusAI/finbert`) sentiment on headlines from the **FNSPID** dataset (~40,900 headlines scored).
- 7 S&P 500 tickers (AAPL, TSLA, NVDA, DIS, GOOG, GS, XOM), 2022–2023.
- Regime: **S&P 500 above or below its 200-day moving average** (Chau, Deesomsak & Koutmos, 2016).
- Target: 5-day forward return direction (binary).
- Walk-forward expanding window (150-day initial window, 20-day step), ~2,200 out-of-sample predictions.
- Models: LR_combined, RF_M1 (traditional technical indicators), RF_M2 (sentiment only), RF_M3 (combined).

**Findings that shape this app**
- Sentiment predicted better in **bull markets** for 5 of 7 tickers (NVDA bull−bear F1 gap +0.161, GS +0.112; XOM −0.115 and TSLA went the other way; DIS 0.000).
- The sentiment-only model was competitive on its own, **but adding sentiment to traditional indicators barely helped** (M3 vs M1: max +0.025 F1, range −0.040 to +0.025).

**Design lesson:** headline sentiment alone is a weak price predictor. This app should **not** be a "predict tomorrow's price" machine. It's an **attention filter and research assistant**: it surfaces the few events worth my attention, explains why they matter, and leaves the decision to me. Sentiment is one input among several, and it's more useful as a *change from a stock's own baseline* or as *news volume / novelty* than as a raw level.

## 3. Goal

A personal app that:
1. **Tracks the current market** for my holdings and a wider candidate universe.
2. **Detects events that might start meaningful moves**, using news, financial statements, filings, insider activity, price/volume anomalies and other relevant sources.
3. **Explains** each flagged item in plain language: what happened, why it may matter, bull case, bear case, what to check next.
4. **Delivers** a short ranked digest (daily or weekly) and only alerts instantly on high-scoring events.
5. **Learns**: it logs my ideas and decisions, tracks outcomes, and shows over time which signals actually work.
6. Helps me **find lesser-known companies** with high return potential, through fundamentals screening and not only mega-caps.

**This is decision support, not automated trading.** No order execution.

## 4. Decisions made

| Decision | Choice |
|---|---|
| Where to build | **Claude Code, local repo** (not a claude.ai artifact: artifacts can't run scheduled jobs or Python, and can't safely hold API keys) |
| Language | **Python** |
| Storage | **DuckDB** to start (one local file, SQL-friendly); move to Postgres only if needed |
| Scheduling | Local cron while developing → **GitHub Actions or a small VPS** so the digest runs when my laptop is off |
| LLM | **Claude API** for reading filings/news and writing the briefs |
| Delivery | **Telegram bot** (preferred) or email digest |
| Dashboard | Optional and later: **Streamlit** (an artifact reading exported JSON is also possible) |
| Earlier intention | Run a **historical fundamentals screen (backtest)** early, to check that a screen has edge before trusting it |

## 5. Open decisions (settle these first)

1. **Holding horizon:** days to weeks (event-driven, needs fresh data) or months (fundamentals-driven, slower and cheaper)? *Leaning: medium/long-term with event alerts on top. Confirm.*
2. **Market:** US-listed stocks only to start? *Recommended: yes, because SEC EDGAR gives free, official, structured data.*
3. **Universe size:** watchlist of ~20–50 plus a screener over a broader universe (e.g. Russell 2000 / small and mid caps for "lesser-known" names)?
4. **Data budget:** free tiers only at first, or willing to pay ~$10–30/month for a better price/news API?
5. **Delivery time:** when should the daily digest arrive (Tbilisi time, UTC+4)? Before the US open (17:30 Tbilisi) makes sense.

## 6. Architecture

```
            ┌────────────────────── scheduled jobs ──────────────────────┐
            │                                                            │
 Collectors ──► Raw store (DuckDB) ──► Signal detectors ──► Scoring/rank ──► LLM briefs ──► Digest (Telegram/email)
  prices          prices, filings,      event triggers        combine,         Claude API       + instant alerts
  EDGAR           news, fundamentals    fundamental screen    regime-weight                     │
  news                                  sentiment shift                                         ▼
  macro                                 regime filter                                     Journal (my decisions
                                                                                          + outcome tracking)
                                                                                                │
                                                                       Evaluation ◄─────────────┘
                                                                  (which signals actually work)
```

### 6.1 Data sources (verify current terms, limits and pricing before relying on them)

| Need | Source | Notes |
|---|---|---|
| Prices / volume | `yfinance` (start), later Polygon / Tiingo / Alpha Vantage | yfinance is unofficial and may break |
| Fundamentals | **SEC EDGAR XBRL** (`companyfacts`, `frames` APIs) | Free and official; requires a descriptive User-Agent with an email |
| Filings | **SEC EDGAR** submissions API | 8-K (material events), 10-Q/10-K, **Form 4** (insider trades), 13D/13G (activist stakes) |
| News | Finnhub (free tier), RSS feeds, GDELT | Store headline, source, timestamp, ticker mapping |
| Earnings calendar | Finnhub / similar | For upcoming catalysts |
| Macro | **FRED** API | Rates, yields, etc. |
| Market regime | S&P 500 (SPY / ^GSPC) vs 200-day MA | Reuse the thesis rule |

### 6.2 Signal detectors (each writes rows to a `signals` table)

**Event triggers**
- Earnings surprise (actual vs estimate) and guidance changes
- **Insider cluster buys** (several Form 4 open-market purchases within N days)
- Unusual volume (e.g. > 3× 20-day average) and price gaps (> X% overnight)
- Specific 8-K items (material agreements, executive changes, etc.)
- Analyst rating/estimate revisions (if a data source allows)
- New 13D filings

**Fundamental screen (slow pipeline, runs weekly)**
- Quality: margin trend, ROIC, revenue growth acceleration
- Balance sheet: leverage, liquidity
- Valuation vs peers / own history
- Focus on small and mid caps for the lesser-known angle
- **Backtest the screen historically before trusting it** (point-in-time data, to avoid look-ahead bias)

**Sentiment (reuses the thesis pipeline)**
- FinBERT scores per headline → daily ticker aggregate
- Signal = **deviation from the ticker's rolling baseline** plus **news volume spike / novelty**, not the raw level

**Regime filter**
- Bull/bear flag from the 200-day MA rule; adjusts signal weights (the thesis suggests sentiment deserves more weight in bull regimes)

### 6.3 Scoring
- Start simple and transparent: weighted sum of normalised signal strengths × regime multiplier.
- Every score should be **explainable** (store which signals contributed).
- Later: learn the weights from journal outcomes.

### 6.4 LLM brief (Claude API)
For each top-ranked item, send the relevant filing excerpt / articles / numbers and ask for:
- **What happened** (1–2 sentences)
- **Why it may matter**
- **Bull case / bear case**
- **What to check next**
- **Confidence + key uncertainty**

Rules: cite the source documents, never invent numbers, say clearly when data is missing. Cache results to control cost.

### 6.5 Journal and evaluation
- Table of ideas: date, ticker, triggering signals, score, my action (ignored / watch / bought / sold), notes.
- Automatically compute forward returns at **+5, +20, +60 trading days**, against SPY and the sector ETF.
- Periodic report: hit rate and average excess return **per signal type**. This applies the thesis's walk-forward evaluation to live decisions.

## 7. Phased roadmap

**Phase 0: setup**
- Repo, virtualenv (or `uv`), `.env` for keys (never committed), DuckDB schema, logging, basic tests.

**Phase 1: MVP (target: 2–3 weekends)**
- Watchlist config (YAML).
- Collectors: daily prices, EDGAR filings (8-K, Form 4), news for the watchlist.
- Detectors: price gap, volume spike, insider cluster buy, new 8-K.
- Claude briefs for top items.
- Daily Telegram/email digest.
- ✅ Done when: I get a useful digest every weekday without touching anything.

**Phase 2: screener**
- EDGAR XBRL fundamentals → fundamental screen over a broader universe.
- Historical backtest of the screen (point-in-time).
- Weekly "new candidates" section in the digest.

**Phase 3: sentiment and scoring**
- Port the FinBERT pipeline from the thesis; baseline-deviation and novelty signals.
- Regime filter; unified scoring with explanations.

**Phase 4: journal and learning**
- Decision journal (Telegram buttons or a Streamlit form).
- Outcome tracking and per-signal performance report.
- Optional: Streamlit dashboard.

## 8. Suggested repo structure

```
market-intel/
├── CLAUDE.md                # this file (or a trimmed version)
├── README.md
├── pyproject.toml
├── .env.example             # API keys template (real .env is gitignored)
├── config/
│   ├── watchlist.yaml
│   └── settings.yaml        # thresholds, weights, schedule
├── src/market_intel/
│   ├── collectors/          # prices.py, edgar.py, news.py, macro.py
│   ├── storage/             # db.py, schema.sql
│   ├── signals/             # events.py, fundamentals.py, sentiment.py, regime.py
│   ├── scoring/             # rank.py
│   ├── llm/                 # briefs.py, prompts/
│   ├── delivery/            # telegram.py, email.py, digest.py
│   ├── journal/             # journal.py, evaluation.py
│   └── cli.py               # e.g. `market-intel run-daily`
├── backtests/
├── notebooks/               # exploration only
├── tests/
└── .github/workflows/daily.yml
```

## 9. Engineering conventions

- Every collector is **idempotent** (safe to re-run; upsert by natural key). Same habits as Delta backfills at work.
- Store **raw data first**, derive signals from raw tables. Keep timestamps in UTC; display in Tbilisi time.
- **Point-in-time correctness** for anything backtested: use filing/accepted dates, not period-end dates.
- Respect API rate limits and terms; SEC needs a proper User-Agent and ≤10 requests/sec.
- Secrets only in `.env` / GitHub Actions secrets.
- Thresholds and weights live in config, not hard-coded.
- Small, testable modules; a dry-run mode that prints the digest instead of sending it.
- Track LLM token usage and cost per run.

## 10. Non-goals and guardrails

- **No automated trading or order execution.**
- No promises of prediction accuracy. The thesis showed modest, regime-dependent signal.
- The app supports my decisions and doesn't make them. Not financial advice.
- Don't overbuild: ship the Phase 1 digest before adding ML.

## 11. First task for Claude Code

1. Ask me the open decisions in §5 (or confirm the leanings).
2. Scaffold the repo structure from §8 with `pyproject.toml`, `.env.example` and the DuckDB schema.
3. Implement the price and EDGAR collectors for a 5-ticker test watchlist, with a dry-run digest printed to the console.
