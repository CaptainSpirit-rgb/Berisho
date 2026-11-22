# Berisho: multi-agent blueprint and redefined roadmap

*22 November 2026. Answers §13 ("Immediate next step") of the proposal
`Berisho_Multi_Agent_Architecture.md`, checked against the code on `main`.*

**In one paragraph.** Berisho changes from a pipeline that ends in a digest into a small
research desk: Python still collects, stores and calculates everything, and a few
narrowly scoped Claude agents read a **research packet** that Python builds,
write **structured reports with citations**, and a Chief Intelligence Officer (CIO)
combines them into a report for Telegram. To stay **free**, the agents run as **Claude
Code subagents in a headless Claude Code session on Luka's PC**, which uses his Claude
subscription, not the paid API. LangGraph and the Agent SDK are left for later, because both
need the paid API or a local model. Stage 1 is three roles, at most three tickers a day, run
for two weeks next to the current digest to see whether the reports are actually better.

---

## 1. What I'd keep, change or push back on in the proposal

| Proposal | Verdict | Why |
|---|---|---|
| Research desk, not a trading floor; quality over agent count | **Keep** | Fits the thesis lesson (attention filter, not a predictor) |
| Hub and spoke, no free agent chatter | **Keep** | Also the only shape Claude Code subagents support |
| Python calculates, LLMs interpret | **Keep, enforce in code** | The packet holds every number; the validator rejects numbers not in it (§4.6) |
| Structured JSON reports; confidence, materiality and priority kept separate | **Keep** | Schema in §4.5 |
| Stage 1 = Fundamental + News/Filings + CIO | **Keep** | News alone is too thin today (about 10 Yahoo headlines per stock, no article text), so merging it with filings is right |
| Quantitative Analyst as an LLM agent | **Change: deterministic "Quant desk", no LLM** | `signals/`, `regime.py`, sentiment and the screener already do this. A section of the packet, not an agent. The CIO interprets it |
| Research Auditor as an LLM agent | **Split** | Most auditing can be code: schema, citation IDs exist, every number matches the packet. This is free and doesn't share the model's blind spots. An LLM auditor is added in Stage 2 only for what code can't check |
| Option C (Python + LangGraph + Claude) as the long-term direction | **Defer** | It needs the Claude API (paid) or a local model. Luka's rule is free to run. Revisit only if Stage 1–2 show value *and* Luka decides on a budget |
| Agent Teams | **Not now** | Experimental, built for interactive sessions, and uses several times the tokens of subagents. Not suited to an unattended daily run |
| Six agents on every watchlist stock every day | **Change: research only what the signals flag** | Subscription limits, and it's the whole point of an attention filter. Max 3 tickers a day, plus one rotating stock on quiet days |

---

## 2. Where agents fit in the code that exists

```
 EXISTING (unchanged)                         NEW
 ───────────────────                          ───
 collectors/ prices, edgar, filing_text, news
        │
 DuckDB  market_intel.duckdb (+ screener db)
        │
 signals/ regime, sentiment · digest flags ─► research/packet.py   builds one packet per ticker
 screener/ metrics (xbrl_facts)                    │                (all numbers + source IDs)
        │                                          ▼
 delivery/digest ─► Telegram (as today)      .claude/agents/*.md   specialists (subagents)
                                                   │
                                             CIO (headless claude -p)  synthesis, 1 follow-up round
                                                   │
                                             research/validate.py  schema + citation + number check
                                                   │
                                             DuckDB agent_reports  (start of company memory)
                                                   │
                                             delivery/research.py ─► Telegram "Research desk" message
```

| Existing component | Role in the new design | Change needed |
|---|---|---|
| `collectors/prices.py`, `edgar.py`, `filing_text.py`, `news.py` | Data layer | None |
| `storage/schema.sql` (prices, filings, insider_transactions, news, signals, briefs, journal…) | Shared data foundation | Add `research_runs`, `agent_reports` (append-only) |
| `signals/regime.py`, `signals/sentiment.py`, digest flags | **Quant desk** (deterministic) | Expose as a packet section, not only as digest text |
| `screener/facts.py`, `metrics.py` (`xbrl_facts`) | Fundamentals input for the Fundamental Analyst | Compute the same metrics for watchlist tickers (JPM: banks are excluded from the screen, so it needs a banks-specific set or "data missing") |
| `llm/briefs.py` `build_payload` | Starting point for the packet builder | Split per ticker, add source IDs |
| `delivery/export.py` (digest-data branch) | Stays as the cloud fallback path | None in Stage 1 |
| `delivery/analysis.py` rule-based notes | Remains the fallback; digest is always sent | None |
| `delivery/telegram.py`, `render.py` | Delivery | New renderer for the research message |
| `journal/` (empty), `journal`/`journal_outcomes` tables | Stage 4 | — |
| Scheduled cloud analysis (16:33, reads `latest.json`) | Superseded by the local run once Stage 1 works; kept as a fallback until then | Retire after the trial |

---

## 3. Running it for free

### 3.1 Recommended runtime: Claude Code headless on the PC

The daily run already needs the PC (the cloud can't reach Yahoo, SEC or Telegram). Running
the agents there too means they can read DuckDB and the full filing text directly, and
Telegram is reachable. Claude Code's `claude -p "<prompt>"` runs a session without a
window. Subagents are Markdown files in `.claude/agents/` with their own instructions and
allowed tools. The main session plays the CIO and calls them.

```
16:00  market-intel run-daily            (existing Task Scheduler job: collect, digest, Telegram)
16:15  scripts\research.ps1
         market-intel research-packets   (Python: choose tickers, write data\research\<date>\*.json)
         claude -p "/research <date>"    (CIO + subagents write reports\*.json)
         market-intel research-validate  (Python: reject or flag unsupported claims)
         market-intel research-send      (Python: Telegram "Research desk" message, store in DuckDB)
```

### 3.2 What costs money and what doesn't

| Option | Cost | Notes |
|---|---|---|
| Claude Code headless (`claude -p`) logged in with Luka's subscription | **Free** (inside plan usage limits) | Subagent work counts toward the same limits as interactive use. Keep the daily run small (§4.8) |
| Same, but with `ANTHROPIC_API_KEY` set in the environment | **Paid** | Claude Code prefers the API key when it is set and bills it. `research.ps1` must clear that variable. Today it lives only in `.env`, which Claude Code doesn't read, but check |
| Python Agent SDK / LangGraph / CrewAI calling Claude | **Paid** (API billing) | This is Option C. A subscription doesn't cover API calls |
| Existing `llm.enabled: true` path in `briefs.py` | **Paid** (~$0.10–3/month) | Stays off |
| Local model via Ollama (e.g. an 8B–14B model) | **Free**, uses the PC's GPU/CPU | Weaker at synthesis and citing. A possible later option for cheap triage (news de-duplication, novelty). Depends on the PC's hardware |
| Agent Teams | Free on the subscription, but heavy | Not for an unattended run |
| Cloud scheduled session (today's 16:33 analysis) | Free | Can't reach DuckDB or Telegram; works only from `latest.json`. Fallback only |

> Before relying on it: check that the current Claude plan allows headless/scheduled
> `claude -p` use and how much of the usage limit a run consumes. Measure it in week 1.

---

## 4. Organizational blueprint (the eight items from §13)

### 4.1 Job descriptions

| Role | Kind | Central question | Stage |
|---|---|---|---|
| **Quant desk** | Python, no LLM | What is the market doing, independent of the news? | 1 (exists) |
| **Fundamental Analyst** | Subagent | Is the business getting better or worse? | 1 |
| **News & Filings Analyst** | Subagent | What happened, what did the company officially say, and what's new vs repeated? | 1 |
| **Chief Intelligence Officer** | Main headless session | What deserves Luka's attention today, and how sure are we? | 1 |
| **Validator** | Python, no LLM | Is every claim cited, and does every number match the packet? | 1 |
| **Risk Analyst** | Subagent | What are the others missing; what is the downside? | 2 |
| **Research Auditor** | Subagent | Do the cited sources actually say what is claimed? | 2 |

### 4.2 Responsibilities and boundaries

- **Fundamental Analyst** reads the packet's `fundamentals` (TTM revenue, growth and
  acceleration, operating and FCF margins and trend, net debt/EBITDA, valuation vs sector,
  all computed by `screener/metrics.py`) and the 10-Q/10-K entries in `filings`. It may
  not compute new ratios. If a metric is missing it says so. It doesn't comment on news.
- **News & Filings Analyst** reads `filings` (8-K items, EX-99 press-release excerpts,
  Form 4, 13D/G) and `news` (headlines). Filings outrank headlines on what was disclosed.
  It must mark each headline as *new information*, *repeat of a filing* or *commentary*. It
  doesn't judge valuation.
- **CIO** reads the specialists' reports and the `quant` section. It doesn't do primary
  research: if it needs a fact, it asks a specialist (§4.4). It never gives buy or sell
  advice and doesn't predict prices (guardrail from CLAUDE.md §10).
- **Risk Analyst** (Stage 2) reads the same packet plus the specialists' reports and must
  name at least one concrete downside per ticker, each with a source, or say "none found".
- **Research Auditor** (Stage 2) receives only the CIO's draft claims and the **raw
  sources** (not the other agents' summaries), so it checks the evidence, not the prose.

### 4.3 Data access permissions

Enforced by each subagent's `tools:` line and by what the packet contains.

| Role | Packet sections | Raw files | Can write | Network |
|---|---|---|---|---|
| Fundamental | `meta`, `quant`, `fundamentals`, `filings` (10-Q/10-K only) | — | its own report file | No |
| News & Filings | `meta`, `filings`, `news`, `insiders` | filing text under `data\research\<date>\docs\` | its own report file | No |
| Risk (S2) | whole packet + specialist reports | as News & Filings | its own report file | No |
| Auditor (S2) | `sources` index + CIO draft | all of `docs\` | audit file | No |
| CIO | `meta`, `quant`, all reports | — | `final.json` | No |

No agent gets web access in Stage 1. Every fact must come from data Berisho collected,
which keeps sources traceable and the run reproducible. No agent can run arbitrary shell
commands. The `claude -p` call gets an explicit `--allowedTools` list (Read, Write
inside `data\research\`, Task).

### 4.4 Communication protocol

- **Hub and spoke only.** Specialists never message each other; the CIO is the only one
  who calls them.
- **Round 1 (independent):** each specialist gets the same packet and works without
  seeing the others' reports, so their first views stay independent.
- **Round 2 (targeted, optional):** the CIO may send **one** follow-up per ticker as a
  structured question: `{to, ticker, question, why, evidence_needed}`. Example: the
  Fundamental Analyst says "improving" and the filings show inventory up sharply, so the CIO asks
  the Fundamental Analyst about inventory and cash conversion, citing the filing.
- **Stopping rule:** at most one follow-up round, then publish with the disagreement left
  visible. A hard time cap per run (e.g. 20 min) is set in `research.ps1`.

### 4.5 Report formats

Specialist report (`reports/<agent>_<TICKER>.json`):

```json
{
  "agent": "fundamental_analyst",
  "ticker": "NVDA",
  "as_of": "2026-11-23",
  "assessment": "improving | stable | deteriorating | unclear",
  "findings": [
    {
      "claim": "TTM revenue growth accelerated for the third quarter in a row",
      "source_ids": ["xbrl:revenue:2026Q3", "filing:0001045810-26-000123"],
      "confidence": "high | medium | low",
      "materiality": "high | medium | low"
    }
  ],
  "risks": [{"claim": "...", "source_ids": ["..."]}],
  "missing_data": ["No segment data in the packet"],
  "open_questions": ["Is the margin gain from mix or pricing?"],
  "research_priority": "high | medium | low"
}
```

Confidence, materiality and priority are separate fields, as the proposal asks. Labels are
used instead of 0.92-style decimals, because an LLM's decimals are not calibrated.

CIO final report (`final.json`): per ticker, a one-line headline, what happened, why it
may matter, the bull and bear points (each with `source_ids`), **disagreements** (who said
what, resolved or not), what to check next, and missing data. Plus a "today in brief" line
across tickers. JSON Schemas live in `src/market_intel/research/schemas/`.

### 4.6 Cross-validation rules

1. **Every factual claim needs at least one `source_id`** that exists in the packet's
   `sources` index (a filing accession, an XBRL fact key, a headline ID, a price row).
2. **Numbers are copied, not computed.** The validator extracts every number in the report
   and checks it against the packet values for the cited sources (with rounding tolerance).
   An unmatched number is removed from the claim and the claim is marked "unverified".
3. **Independence is counted by original source.** Five headlines that repeat one press
   release count as one source. The packet gives each headline a `derived_from` link when
   it matches a filing's date and topic. The CIO may not call a claim "confirmed" unless it
   has two independent sources.
4. **Disagreements are kept.** If two reports conflict on the same topic, the CIO must
   either resolve it with a cited follow-up or list it under `disagreements`.
5. **Stage 2:** the Auditor re-reads the raw document for every *high-materiality* claim.

### 4.7 CIO decision authority

The CIO may: choose which tickers and findings make the report, set the overall priority,
ask one follow-up per ticker, and write the plain-language synthesis.

The CIO may not: change or add numbers, drop a disagreement or an Auditor flag, cite
anything that isn't in the packet, give buy or sell advice or price targets, or override
the validator.

### 4.8 When to research and when to publish

**Which tickers get researched** (chosen by Python, not by the LLM):
1. Watchlist stocks with a digest flag today (gap, volume spike, fresh 8-K, 13D, insider
   cluster buy, sentiment shift), ranked by the existing score.
2. A 10-Q/10-K filed since the last run.
3. If none of the above, one watchlist stock in rotation so each gets a fresh review about
   weekly.
4. Max 3 tickers per day (`research.max_tickers` in `settings.yaml`).

**Publish when** the validator passes or has removed every failing claim, and at least one
ticker has a report. **Don't publish** (log it, and send a one-line "research desk skipped:
reason" to Telegram) when the run times out, the subscription limit is hit, or every claim
fails validation. **The normal digest is always sent first and doesn't depend on the agents.**

---

## 5. Redefined roadmap

What's already built (Phases 0–3 of CLAUDE.md, PRs #1–#12) becomes the **data layer and
Quant desk**. The old Phase 4 (journal) moves to Stage 4.

### Stage 0: finish the base (open items, no new features)
- Merge PR #12 and #13, set up the 16:00 Task Scheduler job, install FinBERT, run the
  S&P 500 screener once on the PC. The agents need these tables filled.

### Stage 1: three-role prototype (2 weekends + a 2-week trial)

Concretely:

| # | Deliverable | Where |
|---|---|---|
| 1 | Packet builder: per-ticker JSON with `meta`, `quant`, `fundamentals`, `filings`, `insiders`, `news`, and a `sources` index; filing texts written next to it | `src/market_intel/research/packet.py`, `market-intel research-packets` |
| 2 | Watchlist fundamentals: reuse `screener/metrics.py` for the 5 watchlist tickers (screener DB) | `research/packet.py` |
| 3 | Ticker selection rule (§4.8) | `research/select.py`, `settings.yaml: research:` |
| 4 | Subagents: `fundamental-analyst.md`, `news-filings-analyst.md` | `.claude/agents/` |
| 5 | CIO instructions as a slash command | `.claude/commands/research.md` |
| 6 | JSON Schemas + validator (schema, citations, number matching) | `research/schemas/`, `research/validate.py` |
| 7 | Storage: `research_runs`, `agent_reports` (append-only, never updated in place) | `storage/schema.sql` |
| 8 | Telegram "Research desk" message | `delivery/research.py`, `market-intel research-send` |
| 9 | Runner script with time cap, `ANTHROPIC_API_KEY` cleared, allowed tools list; Task Scheduler at 16:15 | `scripts/research.ps1`, README |
| 10 | Tests: packet building, selection, validator (fixtures, no LLM in tests) | `tests/test_research_*.py` |

**Trial (10 trading days):** the digest and the Research desk message both go out. Luka
rates each research item 👍/👎 (Telegram reply or a line in a file). We also track the
validator's rejection rate, minutes per run and how much of the usage limit is used.
**Stage 1 succeeds if** most items are rated useful, under ~10% of claims fail
validation, and the run fits comfortably in the plan's limits. Otherwise we stop and keep
the digest.

### Stage 2: independent challenge
- Risk Analyst and Research Auditor subagents. The Auditor reads raw documents only.
- Follow-up round enabled; `disagreements` shown in Telegram.
- `derived_from` de-duplication of headlines vs filings.

### Stage 3: quant and screener integration
- Screener candidates (weekly) get one Fundamental review each before they're shown.
- Regime and sentiment shift feed the CIO's priority explicitly, with weights from
  `settings.yaml` (thesis: sentiment counts more in bull markets).

### Stage 4: memory and evaluation
- **Company research memory:** each ticker's past `agent_reports` (latest 3, plus every
  high-priority one) go into the packet as `history`, so agents compare with what they said
  before. Append-only with timestamps, so past conclusions are never rewritten.
- Decision journal (`journal` table already exists) and +5/+20/+60-day outcomes vs SPY and
  sector ETF; per-agent and per-signal hit rates.
- Only now consider LangGraph, and only with a local model or an agreed API budget.

---

## 6. Risks and open questions

- **Subscription limits** are the main constraint. Measure them in week 1; reduce
  `max_tickers` if needed.
- **Plan terms for unattended runs:** to confirm before relying on a daily headless run.
- **Same-model correlation:** every agent is Claude. Mitigated by different data per role,
  the code validator, and the Auditor reading raw sources. It can't be removed without a second model.
- **Thin news:** headlines only. Article text would need a new free source (RSS, GDELT);
  out of scope for Stage 1.
- **PC must be on** at 16:00–16:40 on weekdays.
- **Not financial advice:** the CIO prompt and the validator both block buy/sell language.
