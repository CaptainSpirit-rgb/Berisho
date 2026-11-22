"""Research packets: everything an agent may use about one ticker, built by Python.

Every number the agents are allowed to quote is computed here (returns, moving averages,
margins, growth, valuation, portfolio performance) and every fact carries a ``source_id``
that resolves in the packet's ``sources`` index. Agents cite those ids; the validator
checks the ids exist and that every number they write appears in the packet.

Layout of a run directory (``data/research/<date>/``)::

    index.json                 run manifest: tickers, rules, portfolio, file paths
    packets/<TICKER>.json      one packet per ticker
    docs/<accession>_<doc>.txt filing text, wrapped so the Read tool sees all of it
    reports/                   agents write here
"""

from __future__ import annotations

import datetime as dt
import json
import re
import textwrap
from pathlib import Path
from typing import Any

import duckdb

from market_intel.config import Security
from market_intel.delivery.digest import Digest, describe_items
from market_intel.research.portfolio import Position, portfolio_summary
from market_intel.research.select import Pick

PACKET_VERSION = 1

HELD_STANCES = ["add", "hold", "trim", "review_to_sell"]
WATCH_STANCES = ["consider_buying", "watch", "avoid"]
AGENTS = ["fundamental_analyst", "news_filings_analyst"]


def research_settings(settings: dict[str, Any]) -> dict[str, Any]:
    cfg = {
        "max_tickers": 3,
        "portfolio_file": "config/portfolio.yaml",
        "out_dir": "data/research",
        "new_report_days": 7,
        "filings_days": 120,
        "insider_days": 90,
        "news_days": 14,
        "news_per_symbol": 15,
        "max_chars_per_document": 20000,
        "fetch_fundamentals": True,
        "stance": {"min_confidence": "medium", "min_independent_sources": 2},
    }
    user = settings.get("research") or {}
    cfg.update({k: v for k, v in user.items() if k != "stance"})
    cfg["stance"] = {**cfg["stance"], **(user.get("stance") or {})}
    return cfg


def _r(v: float | None, n: int = 2) -> float | None:
    return round(v, n) if v is not None else None


def _pct(v: float | None, n: int = 1) -> float | None:
    """A fraction (0.253) as a percentage (25.3)."""
    return round(100 * v, n) if v is not None else None


def _bn(v: float | None) -> float | None:
    return round(v / 1e9, 2) if v is not None else None


class Sources:
    """The packet's citation index. ``group`` marks evidence that comes from the same
    original source (e.g. all XBRL facts of one report), for counting independent sources."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def add(
        self,
        sid: str,
        kind: str,
        label: str,
        url: str | None = None,
        date: Any = None,
        group: str | None = None,
    ) -> str:
        self.items.setdefault(
            sid,
            {
                "type": kind,
                "label": label,
                "url": url,
                "date": str(date) if date is not None else None,
                "group": group or sid,
            },
        )
        return sid


# ----------------------------------------------------------------------------- quant
def _closes(con, symbol: str, as_of: dt.date, n: int = 300) -> list[tuple[dt.date, float, int]]:
    rows = con.execute(
        """
        SELECT trade_date, close, volume FROM prices_daily
        WHERE symbol = ? AND trade_date <= ? AND close IS NOT NULL
        ORDER BY trade_date DESC LIMIT ?
        """,
        [symbol, as_of, n],
    ).fetchall()
    return list(reversed(rows))


def _ret(closes: list, n: int) -> float | None:
    if len(closes) <= n:
        return None
    return 100 * (closes[-1][1] / closes[-1 - n][1] - 1)


def price_stats(con, symbol: str, as_of: dt.date) -> dict[str, Any] | None:
    c = _closes(con, symbol, as_of)
    if not c:
        return None
    last_date, last = c[-1][0], c[-1][1]

    def sma(n: int) -> float | None:
        return sum(x[1] for x in c[-n:]) / n if len(c) >= n else None

    ytd_base = next((x[1] for x in c if x[0].year == last_date.year), None)
    prior_year = [x for x in c if x[0].year < last_date.year]
    if prior_year:
        ytd_base = prior_year[-1][1]
    year = c[-252:]
    hi, lo = max(x[1] for x in year), min(x[1] for x in year)
    s50, s200 = sma(50), sma(200)
    vols = [x[2] for x in c[-21:-1] if x[2]]
    return {
        "last_close": _r(last),
        "last_close_date": str(last_date),
        "change_1d_pct": _r(_ret(c, 1), 1),
        "change_5d_pct": _r(_ret(c, 5), 1),
        "change_1m_pct": _r(_ret(c, 21), 1),
        "change_3m_pct": _r(_ret(c, 63), 1),
        "change_6m_pct": _r(_ret(c, 126), 1),
        "change_ytd_pct": _r(100 * (last / ytd_base - 1), 1) if ytd_base else None,
        "sma_50": _r(s50),
        "sma_200": _r(s200),
        "pct_vs_sma_50": _r(100 * (last / s50 - 1), 1) if s50 else None,
        "pct_vs_sma_200": _r(100 * (last / s200 - 1), 1) if s200 else None,
        "high_52w": _r(hi),
        "low_52w": _r(lo),
        "pct_below_52w_high": _r(100 * (1 - last / hi), 1) if hi else None,
        "volume_vs_20d_avg": _r(c[-1][2] / (sum(vols) / len(vols)), 1)
        if vols and c[-1][2]
        else None,
        "days_of_history": len(c),
    }


def _quant(con, sec: Security, d: Digest, bench: str, as_of: dt.date, src: Sources) -> dict:
    sid = src.add(
        f"prices:{sec.symbol}",
        "prices",
        f"{sec.symbol} daily prices (Yahoo)",
        date=as_of,
        group=f"prices:{sec.symbol}",
    )
    stock = price_stats(con, sec.symbol, as_of)
    out: dict[str, Any] = {"source_id": sid, "stock": stock}
    bsid = src.add(
        f"prices:{bench}",
        "prices",
        f"{bench} daily prices (Yahoo)",
        date=as_of,
        group=f"prices:{bench}",
    )
    b = price_stats(con, bench, as_of)
    out["benchmark"] = {"symbol": bench, "source_id": bsid, **(b or {})}
    if (
        stock
        and b
        and stock.get("change_3m_pct") is not None
        and b.get("change_3m_pct") is not None
    ):
        out["vs_benchmark_3m_pts"] = _r(stock["change_3m_pct"] - b["change_3m_pct"], 1)
    if sec.sector_etf:
        s = price_stats(con, sec.sector_etf, as_of)
        if s:
            ssid = src.add(
                f"prices:{sec.sector_etf}",
                "prices",
                f"{sec.sector_etf} sector fund prices (Yahoo)",
                date=as_of,
                group=f"prices:{sec.sector_etf}",
            )
            out["sector_fund"] = {"symbol": sec.sector_etf, "source_id": ssid, **s}
            if (
                stock
                and stock.get("change_3m_pct") is not None
                and s.get("change_3m_pct") is not None
            ):
                out["vs_sector_3m_pts"] = _r(stock["change_3m_pct"] - s["change_3m_pct"], 1)
        else:
            out["sector_fund"] = {"symbol": sec.sector_etf, "note": "no prices collected yet"}
    r = d.regime or {}
    out["market_regime"] = (
        {
            "regime": r["regime"],
            "rule": f"{r['benchmark']} vs its {r['ma_days']}-day average",
            "benchmark_close": _r(r["close"]),
            "benchmark_moving_average": _r(r["ma"]),
            "source_id": bsid,
        }
        if "regime" in r
        else {"regime": "unknown"}
    )
    out["rule_flags"] = [t for s, t in d.flags if s == sec.symbol]
    out["news_sentiment"] = next((line for s, line in d.sentiment if s == sec.symbol), None)
    return out


# ----------------------------------------------------------------------- fundamentals
def _has_facts(con, cik: int) -> bool:
    try:
        return bool(con.execute("SELECT 1 FROM xbrl_facts WHERE cik = ? LIMIT 1", [cik]).fetchone())
    except duckdb.CatalogException:
        return False


def _fundamentals(
    con, sec: Security, as_of: dt.date, close: float | None, src: Sources
) -> dict[str, Any]:
    if not sec.cik or not _has_facts(con, sec.cik):
        return {
            "available": False,
            "reason": "No SEC financials stored for this company yet (run research-packets "
            "on the PC, which fetches them, or screen-collect).",
        }
    from market_intel.screener.metrics import company_metrics

    m = company_metrics(con, as_of, [sec.cik], max_staleness_days=400).get(sec.cik)
    if not m:
        return {"available": False, "reason": "SEC financials stored but too old or incomplete."}
    end = m["period_end"]
    filing = con.execute(
        """
        SELECT accession_no, form_type, primary_doc_url, filed_date FROM filings
        WHERE cik = ? AND form_type IN ('10-Q', '10-K', '10-Q/A', '10-K/A')
          AND period_of_report BETWEEN ? AND ?
        ORDER BY filed_date DESC LIMIT 1
        """,
        [sec.cik, end - dt.timedelta(days=12), end + dt.timedelta(days=12)],
    ).fetchone()
    label = f"{sec.symbol} SEC financials (XBRL), period ending {end}"
    url = None
    if filing:
        label += f", from {filing[1]} filed {filing[3]}"
        url = filing[2]
    sid = src.add(
        f"xbrl:{sec.symbol}:{end}",
        "financials",
        label,
        url=url,
        date=end,
        group=f"filing:{filing[0]}" if filing else f"xbrl:{sec.symbol}:{end}",
    )

    out: dict[str, Any] = {
        "available": True,
        "source_id": sid,
        "period_end": str(end),
        "data_age_days": m["data_age_days"],
        "revenue_ttm_usd_bn": _bn(m["revenue_ttm"]),
        "revenue_growth_ttm_pct": _pct(m["revenue_growth"]),
        "revenue_growth_latest_quarter_pct": _pct(m["quarter_growth"]),
        "revenue_growth_previous_quarter_pct": _pct(m["quarter_growth_prev"]),
        "growth_acceleration_pts": _pct(m["growth_acceleration"]),
        "gross_margin_pct": _pct(m["gross_margin"]),
        "gross_margin_change_pts": _pct(m["gross_margin_change"]),
        "operating_margin_pct": _pct(m["operating_margin"]),
        "operating_margin_change_pts": _pct(m["operating_margin_change"]),
        "net_income_ttm_usd_bn": _bn(m["net_income_ttm"]),
        "free_cash_flow_ttm_usd_bn": _bn(m["fcf_ttm"]),
        "fcf_margin_pct": _pct(m["fcf_margin"]),
        "cash_usd_bn": _bn(m["cash"]),
        "debt_usd_bn": _bn(m["debt"]) if m["debt_known"] else None,
        "net_debt_usd_bn": _bn(m["net_debt"]) if m["debt_known"] else None,
        "net_debt_to_ebitda": _r(m["net_debt_to_ebitda"]),
        "notes": "Margin/growth changes are vs the same period a year earlier. TTM = "
        "trailing twelve months.",
    }
    shares = m.get("shares")
    if close and shares:
        mcap = close * shares
        ev = mcap + (m["net_debt"] if m["debt_known"] else 0.0)
        ni, fcf, rev = m["net_income_ttm"], m["fcf_ttm"], m["revenue_ttm"]
        out["valuation"] = {
            "source_ids": [sid, f"prices:{sec.symbol}"],
            "market_cap_usd_bn": _bn(mcap),
            "pe_ratio": _r(mcap / ni, 1) if ni and ni > 0 else None,
            "earnings_yield_pct": _pct(ni / mcap) if ni else None,
            "fcf_yield_pct": _pct(fcf / mcap) if fcf is not None else None,
            "ev_to_sales": _r(ev / rev, 2) if rev else None,
            "note": "Peer/sector comparison not computed in Stage 1.",
        }
    return out


# --------------------------------------------------------------------------- filings
def _wrap(text: str, width: int = 100) -> str:
    return "\n".join(
        textwrap.fill(line, width) if line.strip() else "" for line in text.splitlines()
    )


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def _filings(
    con,
    sec: Security,
    now_naive: dt.datetime,
    days: int,
    doc_chars: int,
    docs_dir: Path,
    run_dir: Path,
    src: Sources,
) -> list[dict[str, Any]]:
    from market_intel.collectors.filing_text import documents_for

    rows = con.execute(
        """
        SELECT accession_no, form_type, accepted_at, filed_date, items, primary_doc_url,
               period_of_report
        FROM filings
        WHERE symbol = ? AND form_type NOT IN ('4', '4/A') AND subject_cik = cik
          AND coalesce(accepted_at, filed_date::TIMESTAMP) >= ?
        ORDER BY coalesce(accepted_at, filed_date::TIMESTAMP) DESC
        """,
        [sec.symbol, now_naive - dt.timedelta(days=days)],
    ).fetchall()
    out = []
    for acc, form, accepted, filed, items, url, period in rows:
        when = accepted or filed
        sid = src.add(
            f"filing:{acc}", "filing", f"{sec.symbol} {form} filed {filed}", url=url, date=when
        )
        entry: dict[str, Any] = {
            "source_id": sid,
            "form": form,
            "accepted_utc": str(when),
            "url": url,
        }
        if form.startswith("8-K"):
            entry["items"] = describe_items(items)
            docs = []
            for doc in documents_for(con, acc):
                text = doc["text"][:doc_chars]
                path = docs_dir / f"{_safe(acc)}_{_safe(doc['doc_name'])}.txt"
                cut = len(text) < len(doc["text"])
                path.write_text(
                    _wrap(text) + ("\n[...text cut for length]" if cut else "") + "\n",
                    encoding="utf-8",
                )
                docs.append(
                    {
                        "type": doc["doc_type"],
                        "url": doc["url"],
                        "text_file": path.relative_to(run_dir).as_posix(),
                        "chars": len(text),
                    }
                )
            entry["documents"] = docs or "text not fetched"
        elif form.startswith("10-"):
            entry["period_of_report"] = str(period) if period else None
            entry["note"] = "Full text not collected; its figures are in `fundamentals`."
        out.append(entry)
    return out


def _insiders(con, symbol: str, since: dt.date, src: Sources) -> dict[str, Any]:
    rows = con.execute(
        """
        SELECT accession_no, line_no, insider_name, insider_title, transaction_date,
               transaction_code, shares, price
        FROM insider_transactions
        WHERE symbol = ? AND transaction_date >= ? AND transaction_code IN ('P', 'S')
        ORDER BY transaction_date DESC, shares * price DESC NULLS LAST
        """,
        [symbol, since],
    ).fetchall()
    trades = []
    buy_value = sell_value = 0.0
    buyers, sellers = set(), set()
    for acc, line, name, title, day, code, shares, price in rows:
        value = (shares or 0) * (price or 0)
        if code == "P":
            buy_value += value
            buyers.add(name)
        else:
            sell_value += value
            sellers.add(name)
        if len(trades) < 15:
            sid = src.add(
                f"form4:{acc}:{line}",
                "insider_trade",
                f"{symbol} Form 4: {name} {'bought' if code == 'P' else 'sold'} on {day}",
                url=f"https://www.sec.gov/Archives/edgar/data/"
                f"{acc.split('-')[0].lstrip('0')}/{acc.replace('-', '')}/",
                date=day,
                group=f"form4:{acc}",
            )
            trades.append(
                {
                    "source_id": sid,
                    "insider": name,
                    "title": title,
                    "date": str(day),
                    "type": "buy (open market)" if code == "P" else "sale",
                    "shares": _r(shares, 0),
                    "price": _r(price),
                    "value_usd": _r(value, 0),
                }
            )
    return {
        "window_days_from": str(since),
        "open_market_buyers": len(buyers),
        "buy_value_usd": _r(buy_value, 0),
        "sellers": len(sellers),
        "sell_value_usd": _r(sell_value, 0),
        "largest_recent": trades,
        "note": "Sales are weak evidence on their own (taxes, planned programs); "
        "open-market buys are stronger.",
    }


def _news(
    con, symbol: str, since: dt.datetime, limit: int, filings: list[dict[str, Any]], src: Sources
) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT news_id, published_at, source, headline, url, sentiment_score FROM news
        WHERE symbol = ? AND published_at >= ?
        QUALIFY row_number() OVER (PARTITION BY lower(trim(headline))
                                   ORDER BY published_at DESC) = 1
        ORDER BY published_at DESC LIMIT ?
        """,
        [symbol, since, limit],
    ).fetchall()
    eight_ks = []
    for f in filings:
        if f["form"].startswith("8-K"):
            try:
                eight_ks.append((dt.datetime.fromisoformat(f["accepted_utc"]), f["source_id"]))
            except ValueError:
                continue
    out = []
    for nid, published, source, headline, url, score in rows:
        # A headline within 48 h after an 8-K probably reports that filing: same original
        # source, so it doesn't count as independent confirmation.
        repeat = next(
            (
                sid
                for when, sid in eight_ks
                if dt.timedelta(0) <= published - when <= dt.timedelta(hours=48)
            ),
            None,
        )
        sid = src.add(
            f"news:{nid}",
            "headline",
            f"{source}: {headline}",
            url=url,
            date=published,
            group=repeat or f"news:{nid}",
        )
        out.append(
            {
                "source_id": sid,
                "published_utc": str(published),
                "source": source,
                "headline": headline,
                "finbert_score": _r(score),
                "may_repeat_filing": repeat,
            }
        )
    return out


def _previous_views(con, symbol: str, before: dt.date) -> list[dict[str, Any]]:
    rows = con.execute(
        """
        SELECT run_date, report FROM agent_reports
        WHERE agent = 'cio' AND ticker = ? AND run_date < ?
        ORDER BY run_date DESC LIMIT 3
        """,
        [symbol, before],
    ).fetchall()
    out = []
    for day, report in rows:
        r = json.loads(report) if isinstance(report, str) else report
        out.append(
            {
                "run_date": str(day),
                "headline": r.get("headline"),
                "stance": r.get("stance"),
                "confidence": r.get("confidence"),
                "thesis_status": (r.get("thesis_check") or {}).get("status"),
            }
        )
    return out


# ------------------------------------------------------------------------------- run
def build_packet(
    con: duckdb.DuckDBPyConnection,
    sec: Security,
    pick: Pick,
    d: Digest,
    holding: dict[str, Any] | None,
    settings: dict[str, Any],
    now: dt.datetime,
    run_dir: Path,
) -> dict[str, Any]:
    cfg = research_settings(settings)
    bench = settings.get("benchmark", {}).get("symbol", "SPY")
    now_naive = now.astimezone(dt.UTC).replace(tzinfo=None)
    as_of = d.as_of or now_naive.date()
    docs_dir = run_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    src = Sources()

    quant = _quant(con, sec, d, bench, as_of, src)
    close = (quant.get("stock") or {}).get("last_close")
    filings = _filings(
        con,
        sec,
        now_naive,
        int(cfg["filings_days"]),
        int(cfg["max_chars_per_document"]),
        docs_dir,
        run_dir,
        src,
    )
    packet: dict[str, Any] = {
        "packet_version": PACKET_VERSION,
        "meta": {
            "ticker": sec.symbol,
            "name": sec.name,
            "cik": sec.cik,
            "sector_fund": sec.sector_etf,
            "data_as_of": str(as_of),
            "depth": pick.depth,
            "why_selected": pick.reasons,
            "held": pick.held,
            "allowed_stances": HELD_STANCES if pick.held else WATCH_STANCES,
        },
        "holding": holding,
        "quant": quant,
        "fundamentals": _fundamentals(con, sec, as_of, close, src),
        "filings": filings,
        "insiders": _insiders(
            con, sec.symbol, as_of - dt.timedelta(days=int(cfg["insider_days"])), src
        ),
        "news": _news(
            con,
            sec.symbol,
            now_naive - dt.timedelta(days=int(cfg["news_days"])),
            int(cfg["news_per_symbol"]),
            filings,
            src,
        ),
        "previous_views": _previous_views(con, sec.symbol, as_of),
    }
    if holding:
        src.add(
            f"portfolio:{sec.symbol}",
            "portfolio",
            f"Luka's {sec.symbol} position (config/portfolio.yaml) valued with Yahoo prices",
            group=f"portfolio:{sec.symbol}",
        )
        holding["source_id"] = f"portfolio:{sec.symbol}"
    packet["sources"] = src.items
    return packet


def fetch_missing_fundamentals(con, securities: list[Security], client) -> list[str]:
    """Fetch SEC companyfacts for selected companies that have none stored (on the PC)."""
    import logging

    from market_intel import screener
    from market_intel.screener import facts

    screener.init_schema(con)
    log = logging.getLogger(__name__)
    fetched = []
    for sec in securities:
        if sec.cik and not _has_facts(con, sec.cik):
            try:
                n = facts.fetch_company(con, client, sec.cik)
                fetched.append(sec.symbol)
                log.info("Fetched %d SEC financial facts for %s", n, sec.symbol)
            except Exception as exc:  # network/SEC errors: the packet says data is missing
                log.warning("Could not fetch SEC financials for %s: %s", sec.symbol, exc)
    return fetched


def build_run(
    con: duckdb.DuckDBPyConnection,
    securities: list[Security],
    positions: list[Position],
    settings: dict[str, Any],
    run_dir: Path,
    now: dt.datetime | None = None,
    sec_client: Any = None,
) -> dict[str, Any]:
    """Pick tickers, write every packet and the run manifest. Returns the manifest."""
    from market_intel.delivery.digest import build_digest
    from market_intel.research.select import select_tickers

    now = now or dt.datetime.now(dt.UTC)
    cfg = research_settings(settings)
    bench = settings.get("benchmark", {}).get("symbol", "SPY")
    by_symbol = {s.symbol: s for s in securities}
    missing = [p.symbol for p in positions if p.symbol not in by_symbol]
    # Holdings outside the watchlist still get prices and a thesis check, but no filings
    # or news (those collectors follow the watchlist).
    for sym in missing:
        by_symbol[sym] = Security(sym, sym)
    d = build_digest(con, list(by_symbol.values()), settings, now)
    picks = select_tickers(
        con,
        [s.symbol for s in securities],
        d.flags,
        positions,
        now,
        max_tickers=int(cfg["max_tickers"]),
        new_report_days=int(cfg["new_report_days"]),
    )
    as_of = d.as_of or now.date()
    portfolio = portfolio_summary(con, positions, as_of, bench) if positions else None
    holdings = {p["symbol"]: p for p in (portfolio or {}).get("positions", [])}

    if sec_client is not None and cfg.get("fetch_fundamentals", True):
        fetch_missing_fundamentals(
            con, [by_symbol[p.symbol] for p in picks if p.symbol in by_symbol], sec_client
        )

    for sub in ("packets", "reports", "docs"):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)
    tickers = []
    for pick in picks:
        packet = build_packet(
            con, by_symbol[pick.symbol], pick, d, holdings.get(pick.symbol), settings, now, run_dir
        )
        path = run_dir / "packets" / f"{pick.symbol}.json"
        path.write_text(
            json.dumps(packet, indent=1, default=str, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        tickers.append(
            {
                "symbol": pick.symbol,
                "depth": pick.depth,
                "held": pick.held,
                "reasons": pick.reasons,
                "packet": f"packets/{pick.symbol}.json",
                "reports": {a: f"reports/{a}_{pick.symbol}.json" for a in AGENTS},
            }
        )
    manifest = {
        "run_date": str(now.astimezone(dt.UTC).date()),
        "data_as_of": str(as_of),
        "generated_at_utc": now.astimezone(dt.UTC).isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
        "tickers": tickers,
        "portfolio": portfolio,
        "holdings_not_on_watchlist": missing,
        "final_report": "reports/final.json",
        "rules": {
            "held_stances": HELD_STANCES,
            "watch_stances": WATCH_STANCES,
            "stance_min_confidence": cfg["stance"]["min_confidence"],
            "stance_min_independent_sources": int(cfg["stance"]["min_independent_sources"]),
            "no_position_sizing": True,
            "no_price_targets": True,
        },
    }
    (run_dir / "index.json").write_text(
        json.dumps(manifest, indent=1, default=str, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest
