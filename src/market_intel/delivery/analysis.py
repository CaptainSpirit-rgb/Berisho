"""The "analysis" part of the digest: a short summary, the few items worth attention and
background notes for the rest.

Two sources fill the same structure:

* ``llm.briefs`` asks Claude to write it from the digest's numbers and filing text;
* ``rule_based`` below writes plainer notes from fixed rules. It is the fallback when the
  Claude API key is missing or a call fails, so a digest always goes out.

Neither invents numbers: every figure comes from the stored data.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from market_intel.delivery.digest import Digest, TickerRow, describe_items


@dataclass
class Item:
    symbol: str
    headline: str
    what: str
    why: str
    bull: str = ""
    bear: str = ""
    check_next: str = ""
    sources: list[str] = field(default_factory=list)


@dataclass
class Analysis:
    summary: str
    items: list[Item]
    background: list[str]
    source: str  # "claude" or "rules"
    model: str | None = None
    cost_usd: float | None = None
    cached: bool = False
    note: str = ""  # why Claude wasn't used, shown in the footer

    @classmethod
    def from_dict(cls, data: dict[str, Any], **kwargs: Any) -> Analysis:
        items = [
            Item(
                symbol=i.get("symbol", ""),
                headline=i.get("headline", ""),
                what=i.get("what", ""),
                why=i.get("why", ""),
                bull=i.get("bull", ""),
                bear=i.get("bear", ""),
                check_next=i.get("check_next", ""),
                sources=list(i.get("sources") or []),
            )
            for i in data.get("items") or []
        ]
        return cls(
            summary=data.get("summary", ""),
            items=items,
            background=list(data.get("background") or []),
            **kwargs,
        )


def _pct(v: float | None) -> str:
    return f"{v:+.1f}%" if v is not None else "n/a"


def relative_5d(row: TickerRow, market: TickerRow | None) -> float | None:
    if row.change_5d_pct is None or market is None or market.change_5d_pct is None:
        return None
    return row.change_5d_pct - market.change_5d_pct


def regime_sentence(d: Digest) -> str:
    r = d.regime
    if not r or "regime" not in r:
        return "The market regime is unknown (not enough benchmark history yet)."
    gap = 100 * (r["close"] / r["ma"] - 1)
    side = "above" if gap >= 0 else "below"
    return (
        f"The market is in a {r['regime'].upper()} regime: {r['benchmark']} closed "
        f"{abs(gap):.1f}% {side} its {r['ma_days']}-day average."
    )


def _money(v: float | None) -> str:
    v = v or 0
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v / div:.1f}{suffix}"
    return f"${v:.0f}"


# Words that mark the sentences worth quoting from a filing, by 8-K item.
HIGHLIGHT_WORDS = {
    "2.02": (
        "revenue",
        "net income",
        "net loss",
        "earnings per share",
        "per diluted share",
        "gross margin",
        "outlook",
        "guidance",
        "expects",
        "expected to",
    ),
    "5.02": ("resign", "retire", "appoint", "elected", "named", "succeed", "depart", "terminat"),
    "1.01": ("agreement", "acquire", "merger"),
    "2.01": ("acquisition", "completed", "disposition"),
    "2.05": ("restructuring", "workforce", "charges"),
    "2.06": ("impairment",),
}
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"])")


def highlights(
    text: str, items: str | None, max_sentences: int = 3, max_len: int = 300
) -> list[str]:
    """The first few sentences of a filing that mention the item's key words (e.g. revenue
    and outlook for results). Quoted verbatim, so nothing is invented."""
    words: tuple[str, ...] = ()
    for code in (c.strip() for c in (items or "").split(",")):
        words += HIGHLIGHT_WORDS.get(code, ())
    if not words or not text:
        return []
    picked = []
    for line in text.splitlines():
        for sentence in _SENTENCE.split(line):
            sentence = sentence.strip()
            low = sentence.lower()
            if 25 <= len(sentence) <= max_len and any(w in low for w in words):
                if sentence not in picked:
                    picked.append(sentence)
            if len(picked) >= max_sentences:
                return picked
    return picked


def _move_text(row: TickerRow, market: TickerRow | None) -> str:
    parts = [f"{_pct(row.change_1d_pct)} on the day", f"{_pct(row.change_5d_pct)} over 5 days"]
    text = ", ".join(parts)
    if row.volume_ratio is not None and row.volume_ratio >= 1.5:
        text += f", on {row.volume_ratio:.1f}x normal volume"
    if market and market.change_5d_pct is not None:
        text += f" ({market.symbol} {_pct(market.change_5d_pct)} over 5 days)"
    return text


def rule_based(
    d: Digest,
    settings: dict[str, Any],
    note: str = "",
    filing_text: Callable[[str], str] | None = None,
) -> Analysis:
    """Notes from fixed rules. ``filing_text(accession_no)`` returns a filing's stored
    text (press release first); when given, key sentences are quoted from it."""
    dig = settings.get("digest", {})
    big_move = float(dig.get("big_move_pct", 5.0))
    rel_move = float(dig.get("relative_move_pct", 8.0))
    max_items = int(dig.get("max_items", 5))

    filings_by = {}
    for f in d.filings:
        filings_by.setdefault(f["symbol"], []).append(f)
    flags_by = {}
    for sym, text in d.flags:
        flags_by.setdefault(sym, []).append(text)
    insiders_by = {i["symbol"]: i for i in d.insiders}

    candidates = []
    for row in d.rows:
        if row.close is None:
            continue
        rel = relative_5d(row, d.market)
        big = (row.change_1d_pct is not None and abs(row.change_1d_pct) >= big_move) or (
            rel is not None and abs(rel) >= rel_move
        )
        filings = filings_by.get(row.symbol, [])
        eight_ks = [f for f in filings if f["form_type"].startswith("8-K")]
        buys = (insiders_by.get(row.symbol) or {}).get("buyers") or 0
        other_flags = [t for t in flags_by.get(row.symbol, []) if not t.startswith("new 8-K")]
        reasons = int(big) + len(eight_ks) + int(buys > 0) + len(other_flags)
        if reasons:
            candidates.append((reasons, abs(rel or 0), row, big, eight_ks, buys, other_flags))
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)

    items = []
    for _, _, row, big, eight_ks, buys, other_flags in candidates[:max_items]:
        what, why, check, sources = [], [], [], []
        what.append(f"{row.symbol} moved {_move_text(row, d.market)}.")
        for f in eight_ks:
            desc = describe_items(f["items"]) or "no item numbers"
            what.append(f"Filed an {f['form_type']} ({desc}).")
            quotes = highlights(filing_text(f["accession_no"]), f["items"]) if filing_text else []
            if quotes:
                what.append("From the filing: " + " ".join(f'"{q}"' for q in quotes))
            if f["url"]:
                sources.append(f["url"])
        if buys:
            ins = insiders_by[row.symbol]
            what.append(
                f"{buys} insider(s) bought {_money(ins['buy_value'])} on the open market "
                f"in the last {d.windows.get('insiders', 30)} days."
            )
        for t in other_flags:
            what.append(f"Flag: {t}.")

        items_codes = {c.strip() for f in eight_ks for c in (f["items"] or "").split(",")}
        if big and "2.02" in items_codes:
            why.append(
                "The move came right after an earnings filing, so the results or guidance are "
                "the likely cause."
            )
        elif big and eight_ks:
            why.append("A large move next to a fresh company filing; the filing may explain it.")
        elif big and d.headlines(row.symbol):
            why.append("No company filing explains this move; the recent headlines below may.")
        elif big:
            why.append(
                "Neither a filing nor a recent headline explains this move. It may come "
                "from the sector or analyst actions."
            )
        if "5.02" in items_codes:
            why.append("Leadership changes can signal a shift in strategy or a problem.")
        if items_codes - {"2.02", "5.02", "9.01", ""}:
            why.append("A material-event filing; check whether it changes the investment case.")
        if buys:
            why.append(
                "Insiders buying with their own money is one of the stronger insider signals."
            )
        if eight_ks:
            check.append("Read the full filing (and its press release, exhibit 99.1, if any).")
        if big and not eight_ks:
            check.append("Read the headlines to see what drove the move.")

        headline = what[0].rstrip(".")
        items.append(
            Item(
                symbol=row.symbol,
                headline=headline,
                what=" ".join(what[1:]) or what[0],
                why=" ".join(why) or "Unusual activity relative to its own history.",
                check_next=" ".join(check),
                sources=sources,
            )
        )

    featured = {i.symbol for i in items}
    background = []
    for row in d.rows:
        if row.close is None:
            background.append(f"{row.symbol}: no price data yet.")
            continue
        if row.symbol in featured:
            continue
        rel = relative_5d(row, d.market)
        if rel is None:
            how = ""
        elif abs(rel) < 3:
            how = ", roughly in line with the market"
        else:
            how = f", {abs(rel):.1f} points {'better' if rel > 0 else 'worse'} than the market"
        line = (
            f"{row.symbol}: {_pct(row.change_5d_pct)} over 5 days{how}. Nothing new filed."
            if not filings_by.get(row.symbol)
            else f"{row.symbol}: {_pct(row.change_5d_pct)} over 5 days{how}."
        )
        news = d.headlines(row.symbol)
        if news:
            line += f' Latest headline: "{news[0]["headline"]}" ({news[0]["source"]}).'
        background.append(line)
    sellers = [i for i in d.insiders if i["sellers"] and not i["buyers"]]
    if sellers:
        listing = ", ".join(
            f"{i['symbol']} {_money(i['sell_value'])} ({i['sellers']} people)" for i in sellers
        )
        background.append(
            f"Insider selling, last {d.windows.get('insiders', 30)} days, no buying: {listing}. "
            "Selling alone is weak evidence: insiders sell for taxes, diversification and "
            "pre-planned programs."
        )

    summary = [regime_sentence(d)]
    if d.market and d.market.change_5d_pct is not None:
        chg = d.market.change_5d_pct
        summary.append(
            f"{d.market.symbol} is {'up' if chg >= 0 else 'down'} {abs(chg):.1f}% over 5 days."
        )
    if items:
        summary.append("Worth a look today: " + ", ".join(i.symbol for i in items) + ".")
    else:
        summary.append("Nothing on the watchlist stands out today.")
    return Analysis(
        summary=" ".join(summary), items=items, background=background, source="rules", note=note
    )
