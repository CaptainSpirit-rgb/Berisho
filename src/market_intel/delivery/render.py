"""Lay out the digest for reading: plain text for the console and HTML for Telegram.

Order: a short summary, the items worth attention (with what / why / bull / bear /
check next and source links), background lines for the rest of the watchlist, then
the numbers (a narrow table that fits a phone), filings and insider totals as
reference. The footer says who wrote the analysis (Claude or the rule-based fallback)
and what it cost.
"""

from __future__ import annotations

import html
import textwrap

from market_intel.delivery.analysis import Analysis, Item
from market_intel.delivery.digest import (
    Candidates,
    Digest,
    TickerRow,
    _local,
    _money,
    _pct,
    describe_items,
)
from market_intel.screener.screen import fmt_money

DISCLAIMER = "Decision support only, not financial advice."


def _stats(row: TickerRow | None) -> str:
    if row is None or row.close is None:
        return "no price data"
    parts = [
        f"{row.close:.2f}",
        f"{_pct(row.change_1d_pct)} today",
        f"{_pct(row.change_5d_pct)} 5d",
    ]
    if row.volume_ratio is not None:
        parts.append(f"vol {row.volume_ratio:.1f}x")
    return " · ".join(parts)


def _table(d: Digest) -> list[str]:
    lines = [f"{'':<5}{'CLOSE':>8}{'1D':>7}{'5D':>7}{'VOL':>6}"]
    rows = list(d.rows) + ([d.market] if d.market else [])
    for t in rows:
        if t.close is None:
            lines.append(f"{t.symbol:<5}  no data yet")
            continue
        vol = f"{t.volume_ratio:.1f}x" if t.volume_ratio is not None else "-"
        lines.append(
            f"{t.symbol:<5}{t.close:>8.2f}{_pct(t.change_1d_pct):>7}"
            f"{_pct(t.change_5d_pct):>7}{vol:>6}"
        )
    return lines


def _regime_line(d: Digest) -> str:
    r = d.regime
    if r and "regime" in r:
        return (
            f"Regime: {r['regime'].upper()} ({r['benchmark']} {r['close']:.2f} vs "
            f"{r['ma_days']}-day avg {r['ma']:.2f})"
        )
    if r:
        return f"Regime: unknown ({r['benchmark']} has {r['history']} of {r['needed']} days)"
    return "Regime: unknown"


def _filing_lines(d: Digest) -> list[tuple[str, str | None]]:
    out = []
    for f in d.filings:
        when = _local(f["accepted_at"], d.display_tz) if f["accepted_at"] else str(f["filed_date"])
        desc = describe_items(f["items"]) if f["form_type"].startswith("8-K") else ""
        out.append((f"{when} {f['symbol']} {f['form_type']} {desc}".rstrip(), f["url"]))
    return out


def _insider_lines(d: Digest) -> list[str]:
    return [
        f"{i['symbol']}: bought {_money(i['buy_value'])} ({i['buyers']}), "
        f"sold {_money(i['sell_value'])} ({i['sellers']})"
        for i in d.insiders
    ]


REFRESH_HINT = "market-intel run-weekly"


def _candidates_heading(c: Candidates) -> str:
    return f"from the weekly S&P 500 screen of {c.run_date.day} {c.run_date:%b}" + (
        f", top {len(c.picks)} of {c.total_saved}" if c.total_saved > len(c.picks) else ""
    )


def _candidate_title(p: dict) -> str:
    bits = [fmt_money(p["market_cap"])] if p.get("market_cap") else []
    if p.get("sector"):
        bits.append(p["sector"])
    tags = [
        t for t, on in (("new this week", p["new"]), ("on your watchlist", p["on_watchlist"])) if on
    ]
    return (
        f"{p['rank']}. {p['ticker']} {p['name']}"
        + (f" ({', '.join(bits)})" if bits else "")
        + f" · score {p['score']:.2f}"
        + (f" [{', '.join(tags)}]" if tags else "")
    )


def _candidate_notes(p: dict) -> list[str]:
    """The top reason and the top warning: enough to decide whether to read more."""
    notes = [f"+ {p['reasons'][0]}"] if p["reasons"] else []
    if p["warnings"]:
        notes.append(f"! {p['warnings'][0]}")
    return notes


def _candidates_footnote(c: Candidates) -> str:
    if c.stale:
        return (
            f"This list is {c.age_days} days old. Run {REFRESH_HINT} to refresh it. "
            "A screen ranks how companies look on paper; it doesn't predict prices."
        )
    return (
        "Full reasons and warnings are in the weekly screen message. A screen ranks how "
        "companies look on paper; it doesn't predict prices."
    )


def _no_candidates_text(c: Candidates) -> str:
    return (
        f"No screen saved yet. Run {REFRESH_HINT} once (it fetches the S&P 500 "
        "financials from SEC and takes a few minutes) and the top picks will show here every day."
    )


def _footer(a: Analysis) -> str:
    if a.source == "claude":
        cost = "cached, no cost" if a.cached else f"cost ${a.cost_usd or 0:.3f}"
        return f"Analysis written by Claude ({a.model}, {cost}). Numbers come from the data."
    reason = f" Claude not used: {a.note}." if a.note else ""
    return f"Notes from fixed rules; quotes are verbatim from the filings.{reason}"


def _row(d: Digest, symbol: str) -> TickerRow | None:
    return next((r for r in d.rows if r.symbol == symbol), None)


def _item_fields(item: Item) -> list[tuple[str, str]]:
    fields = [
        ("What", item.what),
        ("Why it may matter", item.why),
        ("Bull case", item.bull),
        ("Bear case", item.bear),
        ("Check next", item.check_next),
    ]
    return [(k, v) for k, v in fields if v]


# ------------------------------------------------------------------ plain text


def render_text(d: Digest, a: Analysis, width: int = 72) -> str:
    tz = d.display_tz
    local_now = d.generated_at.astimezone(tz)
    wrap = textwrap.TextWrapper(width=width)
    sub = textwrap.TextWrapper(width=width, initial_indent="   ", subsequent_indent="   ")
    out = [f"MARKET DIGEST · {local_now:%a %d %b %Y, %H:%M} {tz.key}", ""]

    out += ["TODAY IN BRIEF", *wrap.wrap(a.summary), ""]

    out.append("WHAT MATTERS")
    if not a.items:
        out.append("Nothing stands out today.")
    for n, item in enumerate(a.items, 1):
        out.append(f"{n}. {item.symbol}  {_stats(_row(d, item.symbol))}")
        out += sub.wrap(item.headline)
        for label, text in _item_fields(item):
            out += sub.wrap(f"{label}: {text}")
        for url in item.sources:
            out.append(f"   Source: {url}")
        for n_ in d.headlines(item.symbol):
            out += sub.wrap(
                f"News ({_local(n_['published_at'], tz)}, {n_['source']}): {n_['headline']}"
            )
        out.append("")
    if a.items:
        out.pop()

    if a.background:
        out += ["", "BACKGROUND"]
        for line in a.background:
            out += textwrap.wrap(line, width, initial_indent="· ", subsequent_indent="  ")

    out += ["", f"NUMBERS (last close {d.as_of or '-'})", *_table(d), _regime_line(d)]
    if d.sentiment:
        out += ["", "NEWS SENTIMENT (FinBERT, vs each stock's own baseline)"]
        out += [f"{sym:<6}{text}" for sym, text in d.sentiment]
        if d.sentiment_note:
            out += textwrap.wrap(d.sentiment_note, width)

    filings = _filing_lines(d)
    out += ["", f"FILINGS (last {d.windows.get('filings')} days, excluding Form 4)"]
    for text, url in filings or [("None.", None)]:
        out.append(text)
        if url:
            out.append(f"  {url}")
    insiders = _insider_lines(d)
    if insiders:
        out += ["", f"INSIDER OPEN-MARKET TRADES (last {d.windows.get('insiders')} days)"]
        out += insiders

    c = d.candidates
    if c is not None:
        out += ["", "NEW CANDIDATES" + (f" ({_candidates_heading(c)})" if c.run_date else "")]
        if c.run_date is None:
            out += wrap.wrap(_no_candidates_text(c))
        elif not c.picks:
            out.append("The last screen saved no candidates.")
        for p in c.picks:
            out += textwrap.wrap(_candidate_title(p), width, subsequent_indent="   ")
            for note in _candidate_notes(p):
                out += sub.wrap(note)
        if c.picks:
            out += wrap.wrap(_candidates_footnote(c))

    out += ["", *wrap.wrap(_footer(a)), DISCLAIMER]
    return "\n".join(out)


# ------------------------------------------------------------------ Telegram HTML


def _e(text: str) -> str:
    return html.escape(text, quote=False)


def _link(label: str, url: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{_e(label)}</a>'


def render_html(d: Digest, a: Analysis) -> str:
    """Telegram HTML (parse_mode=HTML). Blocks are separated by blank lines, which is
    where long digests get split into several messages."""
    tz = d.display_tz
    local_now = d.generated_at.astimezone(tz)
    blocks = [
        f"<b>Market digest</b> · {local_now:%a %d %b, %H:%M} Tbilisi"
        if tz.key == "Asia/Tbilisi"
        else f"<b>Market digest</b> · {local_now:%a %d %b, %H:%M} {_e(tz.key)}",
        f"<b>Today in brief</b>\n{_e(a.summary)}",
    ]

    if a.items:
        blocks.append("<b>What matters</b>")
        for n, item in enumerate(a.items, 1):
            lines = [
                f"<b>{n}. {_e(item.symbol)}</b>  {_e(_stats(_row(d, item.symbol)))}",
                f"<i>{_e(item.headline)}</i>",
            ]
            lines += [f"<b>{_e(k)}:</b> {_e(v)}" for k, v in _item_fields(item)]
            if item.sources:
                links = ", ".join(_link(f"filing {i}", u) for i, u in enumerate(item.sources, 1))
                lines.append(f"<b>Source:</b> {links}")
            for n_ in d.headlines(item.symbol):
                label = f"{n_['headline']} ({n_['source']})"
                lines.append("📰 " + (_link(label, n_["url"]) if n_["url"] else _e(label)))
            blocks.append("\n".join(lines))
    else:
        blocks.append("<b>What matters</b>\nNothing stands out today.")

    if a.background:
        blocks.append("<b>Background</b>\n" + "\n".join(f"• {_e(b)}" for b in a.background))

    blocks.append(
        f"<b>Numbers</b> (last close {d.as_of or '-'})\n<pre>"
        + _e("\n".join(_table(d)))
        + "</pre>\n"
        + _e(_regime_line(d))
    )
    if d.sentiment:
        blocks.append(
            "<b>News sentiment</b> (vs each stock's usual)\n"
            + "\n".join(f"<b>{_e(sym)}</b> {_e(text)}" for sym, text in d.sentiment)
            + (f"\n<i>{_e(d.sentiment_note)}</i>" if d.sentiment_note else "")
        )

    filings = _filing_lines(d)
    if filings:
        blocks.append(
            f"<b>Filings</b> (last {d.windows.get('filings')} days)\n"
            + "\n".join(_link(text, url) if url else _e(text) for text, url in filings)
        )
    insiders = _insider_lines(d)
    if insiders:
        blocks.append(
            f"<b>Insider trades</b> (last {d.windows.get('insiders')} days)\n"
            + _e("\n".join(insiders))
        )
    c = d.candidates
    if c is not None:
        if c.run_date is None:
            blocks.append(f"<b>New candidates</b>\n{_e(_no_candidates_text(c))}")
        else:
            lines = [f"<b>New candidates</b> ({_e(_candidates_heading(c))})"]
            if not c.picks:
                lines.append("The last screen saved no candidates.")
            for p in c.picks:
                title = _e(_candidate_title(p)).replace(
                    f"{p['rank']}. {_e(p['ticker'])}", f"{p['rank']}. <b>{_e(p['ticker'])}</b>", 1
                )
                lines.append(title)
                lines += [f"   {_e(n)}" for n in _candidate_notes(p)]
            if c.picks:
                lines.append(f"<i>{_e(_candidates_footnote(c))}</i>")
            blocks.append("\n".join(lines))
    # Keep the footer with the last section, so a split never sends it on its own.
    blocks[-1] += f"\n\n<i>{_e(_footer(a))}\n{_e(DISCLAIMER)}</i>".replace("\n\n", "\n", 1)
    return "\n\n".join(blocks)
