"""The "Research desk" message: the validated CIO report, laid out for Telegram (HTML)
and for the console (plain text).

Order: summary, portfolio, then one block per ticker: stance (or "No stance:
monitoring" and why), headline, what happened, why (reasons), trend, bull, bear,
thesis check for holdings, what would change the view, what to check next,
disagreements, missing data and numbered sources.
"""

from __future__ import annotations

import datetime as dt
import html
from typing import Any

STANCE_LABELS = {
    "add": "ADD",
    "hold": "HOLD",
    "trim": "TRIM",
    "review_to_sell": "REVIEW TO SELL",
    "consider_buying": "CONSIDER BUYING",
    "watch": "WATCH",
    "avoid": "AVOID",
}
FOOTER = "Decision support, not financial advice. Position sizing is your call."


def _day(s: str | None) -> str:
    if not s:
        return "?"
    try:
        return dt.date.fromisoformat(s[:10]).strftime("%d %b %Y")
    except ValueError:
        return s


def _money(v: Any) -> str:
    return f"${v:,.2f}" if isinstance(v, int | float) else "-"


def _pct(v: Any) -> str:
    return f"{v:+.1f}%" if isinstance(v, int | float) else "-"


class _Out:
    def __init__(self, as_html: bool) -> None:
        self.html = as_html
        self.lines: list[str] = []

    def esc(self, s: Any) -> str:
        return html.escape(str(s), quote=False) if self.html else str(s)

    def bold(self, s: Any) -> str:
        return f"<b>{self.esc(s)}</b>" if self.html else str(s)

    def link(self, text: str, url: str | None) -> str:
        if self.html and url:
            return f'<a href="{html.escape(url)}">{self.esc(text)}</a>'
        return f"{text} {url}" if url else text

    def add(self, line: str = "") -> None:
        self.lines.append(line)


def _portfolio(out: _Out, portfolio: dict[str, Any] | None, note: str | None) -> None:
    if not portfolio or not portfolio.get("positions"):
        return
    out.add(out.bold("💼 Your portfolio"))
    for p in portfolio["positions"]:
        if "value_usd" not in p:
            out.add(out.esc(f"{p['symbol']}: {p.get('note', 'no price data')}"))
            continue
        line = (
            f"{p['symbol']} {_money(p['value_usd'])} ({_pct(p.get('return_pct'))} since "
            f"{_day(p['buy_date'])}"
        )
        if p.get("benchmark_return_pct") is not None:
            line += f"; S&P 500 {_pct(p['benchmark_return_pct'])}"
        line += ")"
        if p.get("share_of_portfolio_pct") is not None:
            line += f" · {p['share_of_portfolio_pct']:.0f}% of total"
        out.add(out.esc(line))
    out.add(
        out.esc(
            f"Total {_money(portfolio.get('total_value_usd'))}, "
            f"{_pct(portfolio.get('total_return_pct'))} on "
            f"{_money(portfolio.get('total_invested_usd'))} invested (price only)"
        )
    )
    if note:
        out.add(out.esc(note))
    out.add()


def _cite(ids: list[str], numbers: dict[str, int]) -> str:
    refs = []
    for sid in ids:
        if sid not in numbers:
            numbers[sid] = len(numbers) + 1
        refs.append(f"[{numbers[sid]}]")
    return " " + "".join(refs) if refs else ""


def _item(out: _Out, it: dict[str, Any]) -> None:
    numbers: dict[str, int] = {}
    stance = it.get("stance")
    head = f"{it['ticker']} · {STANCE_LABELS.get(stance, 'NO STANCE: MONITORING')}"
    detail = f"confidence {it.get('confidence', '?')}"
    if it.get("horizon"):
        detail += f", {it['horizon']}"
    if it.get("held"):
        detail += ", you own it"
    out.add(f"{out.bold(head)} ({out.esc(detail)})")
    if not stance and it.get("stance_withheld_reason"):
        out.add(out.esc(f"Why no stance: {it['stance_withheld_reason']}"))
    if it.get("headline"):
        out.add(out.bold(it["headline"]))
    if it.get("what_happened"):
        out.add(out.esc(it["what_happened"]))

    def bullets(title: str, claims: list[dict[str, Any]]) -> None:
        if not claims:
            return
        out.add(out.bold(title))
        for c in claims:
            out.add(out.esc(f"• {c['text']}") + _cite(c["source_ids"], numbers))

    bullets("Why", it.get("reasons") or [])
    trend = it.get("trend")
    if trend:
        out.add(
            out.bold("Trend") + " " + out.esc(trend["text"]) + _cite(trend["source_ids"], numbers)
        )
    bullets("Bull case", it.get("bull") or [])
    bullets("Bear case", it.get("bear") or [])
    tc = it.get("thesis_check")
    if tc:
        status = (tc.get("status") or "unclear").replace("_", " ").upper()
        label = (
            f'Your reason: "{tc["thesis"]}" → {status}'
            if tc.get("thesis")
            else f"Your reason → {status}"
        )
        out.add(out.bold(label))
        for c in tc.get("evidence") or []:
            out.add(out.esc(f"• {c['text']}") + _cite(c["source_ids"], numbers))
    if it.get("change_view_if"):
        out.add(out.bold("Would change the view") + " " + out.esc(it["change_view_if"]))
    if it.get("check_next"):
        out.add(out.bold("Check next") + " " + out.esc(it["check_next"]))
    for d in it.get("disagreements") or []:
        state = "resolved" if d.get("resolved") else "unresolved"
        out.add(out.esc(f"⚖ Disagreement ({state}): {d.get('topic', '')}: {d.get('views', '')}"))
    if it.get("missing_data"):
        out.add(out.esc("Missing data: " + "; ".join(it["missing_data"])))
    srcs = it.get("_sources") or {}
    if numbers:
        refs = []
        for sid, n in numbers.items():
            s = srcs.get(sid) or {}
            refs.append(f"[{n}] " + out.link(s.get("label", sid)[:80], s.get("url")))
        prefix = "<i>Sources:</i> " if out.html else "Sources: "
        out.add(prefix + " · ".join(refs))
    out.add()


def render(final: dict[str, Any], portfolio: dict[str, Any] | None, as_html: bool) -> str:
    out = _Out(as_html)
    out.add(
        out.bold(f"🔎 Research desk · {_day(final.get('run_date'))}")
        + out.esc(f" (data as of {_day(final.get('data_as_of'))})")
    )
    if final.get("summary"):
        out.add(out.esc(final["summary"]))
    out.add()
    _portfolio(out, portfolio, final.get("portfolio_note"))
    items = sorted(
        final.get("items") or [], key=lambda i: (i.get("depth") != "full", not i.get("held"))
    )
    for it in items:
        _item(out, it)
    v = final.get("validation") or {}
    if v:
        out.add(
            out.esc(
                f"Checked: {v.get('claims_total', 0)} claims, {v.get('claims_removed', 0)} removed "
                f"as unsupported; {v.get('stances_withheld', 0)} stance(s) withheld."
            )
        )
    out.add(out.esc(FOOTER))
    return "\n".join(out.lines).strip() + "\n"


def render_skipped(day: str, reason: str) -> str:
    return (
        f"🔎 Research desk {_day(day)}: skipped today ({reason}). The digest above is unaffected."
    )
