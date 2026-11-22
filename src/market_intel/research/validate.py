"""Deterministic checks on what the agents wrote (blueprint §4.6). No LLM involved, so
it doesn't share the agents' blind spots.

* Every claim cites ``source_ids`` that exist in that ticker's packet; unknown ids are
  dropped, and a claim left with none is removed.
* Numbers are copied, not computed: every number in the text must appear in the packet
  (packet values, headlines or filing text), allowing for rounding and units. A claim
  with a number that isn't there is removed; in prose fields the number is marked
  unverified.
* No position sizing or price targets: sizing is Luka's call (blueprint §0).
* Stance rule: allowed stance for held/not held, confidence at least the minimum, and the
  reasons resting on at least N independent sources (sources sharing an original, e.g.
  a headline repeating an 8-K, count once). Otherwise the stance is withheld.

Writes ``reports/final.validated.json`` and ``validation.json`` in the run directory.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIDENCE = ["low", "medium", "high"]
THESIS_STATUS = ["intact", "weakening", "broken", "no_thesis"]

_MONTHS = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
_NOISE = [
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b",  # ISO dates/times
    r"\b\d{1,2}:\d{2}\b",  # clock times
    rf"\b{_MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?\b",  # Nov 20, 2026
    rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTHS}(?:\s+\d{{4}})?\b",  # 20 Nov 2026
    rf"\b{_MONTHS}\s+\d{{4}}\b",  # Jan 2026
    r"\b(?:10-Q|10-K|8-K|20-F|6-K|S-1|DEF 14A)(?:/A)?\b",
    r"\b(?:SC |SCHEDULE )?13[DG](?:/A)?\b",
    r"\bForm\s+\d+\b",
    r"\bS&P\s*\d+\b|\bRussell\s+\d+\b|\bNasdaq[- ]\d+\b|\bDow\s+\d+\b",
    r"\bItems?\s+\d+\.\d+\b",
    r"\b\d+[- ](?:day|week|month|year|quarter)s?(?:[- ](?:average|moving|high|low|old))?\b",
    r"\b(?:Q[1-4]|H[12]|FY|CY)\s?'?\d{0,4}\b",
    r"\b\d{1,2}[- ]\d{1,2} months\b",
]
_NOISE_RE = re.compile("|".join(_NOISE), re.IGNORECASE)
_NUM_RE = re.compile(
    r"(?<![\w.])[-+−]?\$?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?:\s*(%|percent|pts\b|points\b|percentage points\b|bn\b|billion\b|b\b|million\b|mn\b"
    r"|m\b|thousand\b|k\b|x\b|times\b))?",
    re.IGNORECASE,
)
_SCALE = {
    "bn": 1e9,
    "billion": 1e9,
    "b": 1e9,
    "million": 1e6,
    "mn": 1e6,
    "m": 1e6,
    "thousand": 1e3,
    "k": 1e3,
}
_FORBIDDEN = re.compile(
    r"\b(price target|target price|fair value of \$|position siz\w*|portfolio weight\w*|"
    r"allocate|allocation|rebalanc\w*|(?:put|invest|move) \d+\s?% of|"
    r"\d+\s?% of (?:your|the) portfolio)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Num:
    value: float  # absolute value, unit scale applied
    raw: float  # absolute value as written
    decimals: int
    unit: str
    text: str


def extract_numbers(text: str) -> list[Num]:
    """Numbers worth checking in a piece of text (dates, form names, '200-day' etc. skipped)."""
    cleaned = _NOISE_RE.sub(" ", text or "")
    out = []
    for m in _NUM_RE.finditer(cleaned):
        digits = m.group(1)
        unit = (m.group(2) or "").lower()
        raw = abs(float(digits.replace(",", "")))
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        if not unit and decimals == 0 and (raw < 32 or 1990 <= raw <= 2100):
            continue  # small counts ("3 insiders") and years
        out.append(Num(raw * _SCALE.get(unit, 1.0), raw, decimals, unit, m.group(0).strip()))
    return out


def _collect_values(obj: Any, out: list[float]) -> None:
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, int | float):
        out.append(abs(float(obj)))
    elif isinstance(obj, str):
        for n in extract_numbers(obj):
            out.extend((n.value, n.raw))
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_values(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_values(v, out)


def number_found(n: Num, known: list[float]) -> bool:
    half_step = 0.5 * 10 ** (-n.decimals)
    targets = [(n.value, half_step * _SCALE.get(n.unit, 1.0)), (n.raw, half_step)]
    if n.unit in ("%", "percent", "pts", "points", "percentage points"):
        targets.append((n.raw / 100, half_step / 100))  # 25% vs a 0.25 fraction
    for v, tol in targets:
        tol = max(tol, 0.005 * v) + 1e-9  # rounding as written, or 0.5% either way
        if any(abs(abs(k) - v) <= tol for k in known):
            return True
    return False


@dataclass
class Context:
    """What one ticker's claims may use."""

    sources: dict[str, dict[str, Any]]
    numbers: list[float]


@dataclass
class Result:
    issues: list[dict[str, Any]] = field(default_factory=list)
    claims_total: int = 0
    claims_removed: int = 0
    prose_flagged: int = 0
    stances_given: int = 0
    stances_withheld: int = 0

    def add(self, where: str, problem: str, **extra: Any) -> None:
        self.issues.append({"where": where, "problem": problem, **extra})

    def summary(self) -> dict[str, Any]:
        return {
            "claims_total": self.claims_total,
            "claims_removed": self.claims_removed,
            "prose_flagged": self.prose_flagged,
            "stances_given": self.stances_given,
            "stances_withheld": self.stances_withheld,
            "issues": len(self.issues),
        }


def load_context(run_dir: Path, packet_path: str) -> Context:
    packet = json.loads((run_dir / packet_path).read_text(encoding="utf-8"))
    nums: list[float] = []
    _collect_values({k: v for k, v in packet.items() if k != "sources"}, nums)
    for f in packet.get("filings", []):
        for doc in f.get("documents") or []:
            if isinstance(doc, dict) and doc.get("text_file"):
                p = run_dir / doc["text_file"]
                if p.exists():
                    _collect_values(p.read_text(encoding="utf-8"), nums)
    return Context(packet.get("sources", {}), nums)


def _check_claim(
    claim: dict[str, Any], text_key: str, ctx: Context, res: Result, where: str
) -> dict[str, Any] | None:
    """The cleaned claim, or None when it must be removed."""
    res.claims_total += 1
    text = str(claim.get(text_key) or "").strip()
    ids = claim.get("source_ids") or []
    if isinstance(ids, str):
        ids = [ids]
    good = [i for i in ids if i in ctx.sources]
    bad = [i for i in ids if i not in ctx.sources]
    if bad:
        res.add(where, "unknown source ids dropped", ids=bad, text=text)
    reason = None
    if not text:
        reason = "empty claim"
    elif not good:
        reason = "no valid source"
    elif _FORBIDDEN.search(text):
        reason = "position sizing / price target language"
    else:
        missing = [n.text for n in extract_numbers(text) if not number_found(n, ctx.numbers)]
        if missing:
            reason = f"number(s) not in the data: {', '.join(missing)}"
    if reason:
        res.claims_removed += 1
        res.add(where, f"claim removed: {reason}", text=text)
        return None
    out = dict(claim)
    out[text_key] = text
    out["source_ids"] = good
    return out


def _check_prose(text: Any, ctx: Context, res: Result, where: str) -> str | None:
    if text is None:
        return None
    text = str(text).strip()
    if _FORBIDDEN.search(text):
        res.prose_flagged += 1
        res.add(where, "removed: position sizing / price target language", text=text)
        return "[removed by validator: position sizing or price target]"
    missing = [n.text for n in extract_numbers(text) if not number_found(n, ctx.numbers)]
    if missing:
        res.prose_flagged += 1
        res.add(where, "unverified number(s) in prose", numbers=missing, text=text)
        return f"{text} [unverified: {', '.join(missing)}]"
    return text


def _claims(items: Any, text_key: str, ctx: Context, res: Result, where: str) -> list[dict]:
    out = []
    for i, c in enumerate(items or []):
        if isinstance(c, str):
            c = {text_key: c, "source_ids": []}
        if not isinstance(c, dict):
            res.add(f"{where}[{i}]", "not an object; removed")
            continue
        cleaned = _check_claim(c, text_key, ctx, res, f"{where}[{i}]")
        if cleaned is not None:
            out.append(cleaned)
    return out


def independent_sources(claims: list[dict[str, Any]], ctx: Context) -> int:
    groups = {
        ctx.sources[i].get("group", i)
        for c in claims
        for i in c.get("source_ids", [])
        if i in ctx.sources
    }
    return len(groups)


def validate_specialist(
    report: dict[str, Any], ctx: Context, res: Result, where: str
) -> dict[str, Any]:
    out = copy.deepcopy(report)
    out["findings"] = _claims(report.get("findings"), "claim", ctx, res, f"{where}.findings")
    out["risks"] = _claims(report.get("risks"), "claim", ctx, res, f"{where}.risks")
    for key in ("confidence", "materiality"):
        for f in out["findings"]:
            if f.get(key) not in CONFIDENCE:
                res.add(where, f"finding {key} not one of {CONFIDENCE}", value=f.get(key))
    return out


def validate_item(
    item: dict[str, Any], ctx: Context, held: bool, rules: dict[str, Any], res: Result, where: str
) -> dict[str, Any]:
    out = copy.deepcopy(item)
    out["held"] = held
    for key in ("headline", "what_happened", "change_view_if", "check_next", "horizon"):
        out[key] = _check_prose(item.get(key), ctx, res, f"{where}.{key}")
    out["reasons"] = _claims(item.get("reasons"), "text", ctx, res, f"{where}.reasons")
    out["bull"] = _claims(item.get("bull"), "text", ctx, res, f"{where}.bull")
    out["bear"] = _claims(item.get("bear"), "text", ctx, res, f"{where}.bear")
    trend = item.get("trend")
    if isinstance(trend, dict):
        cleaned = _claims([trend], "text", ctx, res, f"{where}.trend")
        out["trend"] = cleaned[0] if cleaned else None
    elif trend:
        out["trend"] = None
        res.add(f"{where}.trend", "trend must be {text, source_ids}; removed")

    tc = item.get("thesis_check")
    if held:
        if not isinstance(tc, dict):
            res.add(where, "holding without a thesis_check")
            out["thesis_check"] = None
        else:
            status = tc.get("status")
            if status not in THESIS_STATUS:
                res.add(f"{where}.thesis_check", "unknown status", value=status)
                status = None
            out["thesis_check"] = {
                "thesis": tc.get("thesis"),
                "status": status,
                "evidence": _claims(
                    tc.get("evidence"), "text", ctx, res, f"{where}.thesis_check.evidence"
                ),
            }
    else:
        out["thesis_check"] = None

    allowed = rules["held_stances"] if held else rules["watch_stances"]
    stance = item.get("stance")
    conf = item.get("confidence")
    if conf not in CONFIDENCE:
        res.add(where, f"confidence not one of {CONFIDENCE}", value=conf)
        conf = "low"
    out["confidence"] = conf
    if stance:
        why = None
        if stance not in allowed:
            why = f"'{stance}' is not allowed here (allowed: {', '.join(allowed)})"
        elif CONFIDENCE.index(conf) < CONFIDENCE.index(rules["stance_min_confidence"]):
            why = f"confidence {conf} is below {rules['stance_min_confidence']}"
        else:
            n = independent_sources(out["reasons"], ctx)
            need = int(rules["stance_min_independent_sources"])
            if n < need:
                why = f"the reasons rest on {n} independent source(s), {need} needed"
        if why:
            res.stances_withheld += 1
            res.add(where, f"stance withheld: {why}", stance=stance)
            out["stance"] = None
            out["stance_withheld_reason"] = f"Evidence below the bar: {why}."
        else:
            res.stances_given += 1
            out["stance"] = stance
    else:
        out["stance"] = None
        if not out.get("stance_withheld_reason"):
            out["stance_withheld_reason"] = "No stance given."
    out["missing_data"] = [str(x) for x in item.get("missing_data") or []]
    out["disagreements"] = [d for d in item.get("disagreements") or [] if isinstance(d, dict)]
    return out


def validate_run(run_dir: Path) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Validate the specialists' reports and the final report of one run."""
    manifest = json.loads((run_dir / "index.json").read_text(encoding="utf-8"))
    rules = manifest["rules"]
    res = Result()
    contexts: dict[str, Context] = {}
    specialists: dict[str, dict[str, Any]] = {}
    for t in manifest["tickers"]:
        ctx = load_context(run_dir, t["packet"])
        contexts[t["symbol"]] = ctx
        for agent, rel in t["reports"].items():
            p = run_dir / rel
            if not p.exists():
                res.add(f"{agent}/{t['symbol']}", "report missing")
                continue
            try:
                report = json.loads(p.read_text(encoding="utf-8"))
            except ValueError as exc:
                res.add(f"{agent}/{t['symbol']}", f"report is not valid JSON: {exc}")
                continue
            specialists[f"{agent}/{t['symbol']}"] = validate_specialist(
                report, ctx, res, f"{agent}/{t['symbol']}"
            )

    final_path = run_dir / manifest["final_report"]
    final: dict[str, Any] | None = None
    if not final_path.exists():
        res.add("final", "final report missing")
    else:
        try:
            raw = json.loads(final_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raw = None
            res.add("final", f"final report is not valid JSON: {exc}")
        if raw is not None:
            # Prose across tickers may quote any packet or the portfolio summary.
            portfolio_nums: list[float] = []
            _collect_values(manifest.get("portfolio"), portfolio_nums)
            everything = Context(
                {}, [n for c in contexts.values() for n in c.numbers] + portfolio_nums
            )
            held = {t["symbol"]: t["held"] for t in manifest["tickers"]}
            depth = {t["symbol"]: t["depth"] for t in manifest["tickers"]}
            final = {
                "run_date": manifest["run_date"],
                "data_as_of": manifest["data_as_of"],
                "summary": _check_prose(raw.get("summary"), everything, res, "final.summary"),
                "portfolio_note": _check_prose(
                    raw.get("portfolio_note"), everything, res, "final.portfolio_note"
                ),
                "items": [],
            }
            for i, item in enumerate(raw.get("items") or []):
                sym = str(item.get("ticker", "")).upper()
                if sym not in contexts:
                    res.add(f"final.items[{i}]", "ticker not in this run; removed", ticker=sym)
                    continue
                ctx = Context(contexts[sym].sources, contexts[sym].numbers + portfolio_nums)
                cleaned = validate_item(item, ctx, held[sym], rules, res, f"final.{sym}")
                cleaned["ticker"] = sym
                cleaned["depth"] = depth[sym]
                cleaned["_sources"] = {
                    sid: contexts[sym].sources[sid]
                    for c in (
                        cleaned["reasons"]
                        + cleaned["bull"]
                        + cleaned["bear"]
                        + ([cleaned["trend"]] if cleaned.get("trend") else [])
                        + ((cleaned.get("thesis_check") or {}).get("evidence") or [])
                    )
                    for sid in c["source_ids"]
                }
                final["items"].append(cleaned)
            covered = {it["ticker"] for it in final["items"]}
            for sym, is_held in held.items():
                if is_held and sym not in covered:
                    res.add("final", "holding has no item (no thesis check)", ticker=sym)

    summary = res.summary()
    removed_share = (
        summary["claims_removed"] / summary["claims_total"] if summary["claims_total"] else 0.0
    )
    summary["claims_removed_share"] = round(removed_share, 3)
    summary["publishable"] = bool(final and final["items"])
    validation = {"summary": summary, "issues": res.issues}
    (run_dir / "validation.json").write_text(
        json.dumps(validation, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    if final is not None:
        final["validation"] = summary
        final["specialists"] = specialists
        (run_dir / "reports" / "final.validated.json").write_text(
            json.dumps(final, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return final, validation
