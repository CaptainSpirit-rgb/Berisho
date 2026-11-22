"""Which tickers get researched today. Chosen by rules, not by the LLM (blueprint §4.8).

1. Every holding gets a thesis check; ``full`` research when something happened to it,
   otherwise ``light``.
2. Watchlist stocks with a digest flag today, most flags first.
3. Stocks with a 10-Q/10-K filed in the last ``new_report_days``.
4. On a quiet day, one watchlist stock in rotation (the one researched longest ago).

At most ``max_tickers`` get full research; light thesis checks for holdings don't count.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field

import duckdb

from market_intel.research.portfolio import Position


@dataclass
class Pick:
    symbol: str
    depth: str  # full | light
    reasons: list[str] = field(default_factory=list)
    held: bool = False


def _new_reports(
    con: duckdb.DuckDBPyConnection, symbols: list[str], since: dt.datetime
) -> dict[str, str]:
    rows = con.execute(
        """
        SELECT symbol, form_type FROM filings
        WHERE symbol IN (SELECT unnest(?)) AND subject_cik = cik
          AND form_type IN ('10-Q', '10-K', '10-Q/A', '10-K/A')
          AND coalesce(accepted_at, filed_date::TIMESTAMP) >= ?
        ORDER BY coalesce(accepted_at, filed_date::TIMESTAMP) DESC
        """,
        [symbols, since],
    ).fetchall()
    out: dict[str, str] = {}
    for sym, form in rows:
        out.setdefault(sym, form)
    return out


def _last_researched(con: duckdb.DuckDBPyConnection) -> dict[str, dt.date]:
    rows = con.execute("SELECT ticker, max(run_date) FROM agent_reports GROUP BY ticker").fetchall()
    return {t: d for t, d in rows}


def select_tickers(
    con: duckdb.DuckDBPyConnection,
    watchlist: list[str],
    flags: list[tuple[str, str]],
    positions: list[Position],
    now: dt.datetime,
    max_tickers: int = 3,
    new_report_days: int = 7,
) -> list[Pick]:
    now_naive = now.astimezone(dt.UTC).replace(tzinfo=None)
    held = [p.symbol for p in positions]
    universe = list(dict.fromkeys(held + watchlist))
    flag_count = Counter(sym for sym, _ in flags)
    flag_text: dict[str, list[str]] = {}
    for sym, text in flags:
        flag_text.setdefault(sym, []).append(text)
    reports = _new_reports(con, universe, now_naive - dt.timedelta(days=new_report_days))

    def reasons_for(sym: str) -> list[str]:
        r = list(flag_text.get(sym, []))
        if sym in reports:
            r.append(f"new {reports[sym]} filed")
        return r

    # Candidates for full research, in priority order: holdings with news, then flagged
    # watchlist names (most flags first), then fresh quarterly/annual reports.
    eventful = [s for s in universe if reasons_for(s)]
    eventful.sort(
        key=lambda s: (s not in held, -flag_count.get(s, 0), s not in reports, universe.index(s))
    )
    full = eventful[:max_tickers]
    if not full and watchlist and max_tickers > 0:
        last = _last_researched(con)
        never = [s for s in watchlist if s not in last]
        rotation = (
            never[0] if never else min(watchlist, key=lambda s: (last[s], watchlist.index(s)))
        )
        full = [rotation]

    picks: list[Pick] = []
    for sym in full:
        reasons = reasons_for(sym) or [
            "weekly rotation: no fresh review yet"
            if sym not in _last_researched(con)
            else "weekly rotation: researched longest ago"
        ]
        picks.append(Pick(sym, "full", reasons, held=sym in held))
    for sym in held:
        if sym not in full:
            picks.append(Pick(sym, "light", ["holding: daily thesis check"], held=True))
    return picks
