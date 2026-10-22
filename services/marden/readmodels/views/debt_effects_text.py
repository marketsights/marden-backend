"""Plain words for the debt effect of an event (views/debt_effects.py) and the drivers of a move (views/moves.py).

Pure functions over the `event_impact` model's output: which of the model's priced changes a site record
stands for, the "Effect on debt" line, and the reason when there is none. A record stands for a change when
it is one of the change's own records (same link), when it is the cushion move the change is (same day), or
when it reads as the same kind of power or permit signal (the price model's own text rules) within a week
before or three days after the day the model first counted it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from marden.core.relevance import procedural_reason
from marden.models import status_text as tx
from marden.models.status_dates import stated_slip

Doc = dict[str, Any]
MINUS = "\u2212"
ARROW = "→"
NO_CHANGE_PTS = 0.05
BEFORE_DAYS, AFTER_DAYS = 7, 3  # a report this close to the day the model counted the signal is that report
SLIP_LINK_DAYS = 45  # a developer slip reaches the price through the next cushion run
READS = ("permit denials, power delays of 3+ months, force-majeure notices citing power, the projected completion, "
         "dealer quotes and trades of the debt, the tenant's bond prices and interest rates")
NOT_READ = f"The price model does not read this kind of record. It moves on {READS}."
KIND_WORDS = {"permit_denial": "permit denial", "power_delay": "power delay of 3+ months",
              "force_majeure": "force-majeure notice", "completion": "projected completion", "quote": "dealer quote"}
_POWERISH = re.compile(r"\b(?:power|gas|pipeline|lateral|interconnect\w*|in[- ]service|substation|transmission"
                       r"|turbines?|fuel cells?|microgrid|grid)\b", re.I)


def parse_day(text: str) -> date:
    """An ISO day; a month ('2025-11') or a year ('2025') is read as its first day."""
    t = text[:10]
    return date.fromisoformat(t if len(t) == 10 else (t + "-01" if len(t) == 7 else t + "-01-01"))


def when(d: str | date) -> str:
    day = parse_day(d) if isinstance(d, str) else d
    return f"{day:%b} {day.day}"


def signed(v: float, decimals: int = 1, unit: str = "") -> str:
    text = f"{abs(v):.{decimals}f}"
    if float(text) == 0:
        return f"0{unit}" if decimals == 0 else f"{0:.{decimals}f}{unit}"
    return f"{'+' if v > 0 else MINUS}{text}{unit}"


def pct_change(v: float) -> str:
    """A yield change in percentage points as the line shows it: '+0.6%' (two decimals under 0.05)."""
    return signed(v, 2 if abs(v) < 0.05 else 1, "%")


def reading_kinds(text: str) -> set[str]:
    """The power and permit signals a record's text states, by the status model's text rules (no count rule)."""
    out: set[str] = set()
    if tx.power_denied(text) is not None:
        out.add("permit_denial")
    if tx.force_majeure(text) is not None:
        out.add("force_majeure")
    for clause in tx.clauses(text):
        months = stated_slip(clause)
        if months is not None and months >= 3 and _POWERISH.search(clause):
            out.add("power_delay")
    return out


def _within(d: date, change_day: date, before: int = BEFORE_DAYS, after: int = AFTER_DAYS) -> bool:
    return change_day - timedelta(days=after) <= d <= change_day + timedelta(days=before)


def match(row: Doc, changes: Sequence[Doc]) -> tuple[list[tuple[Doc, str | None]], str | None]:
    """The model changes a site record stands for, each with the signal kind it stands for (None: the whole
    change), else the reason it changed nothing."""
    d = parse_day(row["date"])
    url = row.get("sourceUrl") or ""
    text = row.get("text") or ""
    hits: list[tuple[Doc, str | None]] = []
    for ch in changes:
        recs = [r for r in ch.get("records") or [] if url and r.get("sourceUrl") == url]
        if recs and ch.get("kind") == "signals":
            kinds = sorted({r["kind"] for r in recs} & set(ch.get("kinds") or []))
            hits.append((ch, kinds[0] if len(kinds) == 1 else None))
        elif recs or (ch.get("kind") == "quote" and url and (ch.get("quote") or {}).get("sourceUrl") == url):
            hits.append((ch, None))
    if hits:
        return hits, None
    changed = str(row.get("changed") or "")
    if changed.startswith("Cushion"):
        same = [ch for ch in changes if ch.get("kind") == "completion" and ch["date"] == d.isoformat()]
        return ([(ch, None) for ch in same], None) if same else ([], "The projected completion the price model reads "
                                                                   "did not change on this day.")
    kinds = reading_kinds(text)
    if kinds:
        near = [(ch, k) for ch in changes if ch.get("kind") == "signals"
                for k in sorted(kinds & set(ch.get("kinds") or []))
                if _within(date.fromisoformat(ch["date"]), d)]
        if near:
            return near, None
        return [], _signal_reason(kinds, d, changes)
    rule = str(row.get("ruleHit") or "")
    if row.get("link") == "Developer" and "D1" in rule:
        nxt = [ch for ch in changes if ch.get("kind") == "completion"
               and d <= date.fromisoformat(ch["date"]) <= d + timedelta(days=SLIP_LINK_DAYS)]
        if nxt:
            return [(nxt[0], None)], None
    return [], other_reason(row)


def _signal_reason(kinds: set[str], d: date, changes: Sequence[Doc]) -> str:
    counted = sorted((ch["date"], k, ch) for ch in changes if ch.get("kind") == "signals"
                     for k in kinds & set(ch.get("kinds") or []))
    words = " or ".join(KIND_WORDS[k] for k in sorted(kinds))
    earlier = [c for c in counted if c[0] < d.isoformat()]
    later = [c for c in counted if c[0] > d.isoformat()]
    if earlier:
        day, k, ch = earlier[-1]
        src = _sources(ch, k)
        return f"Already priced in: the price model counted this {KIND_WORDS[k]} on {when(day)}{src}."
    if later:
        day, k, ch = later[0]
        return (f"Not priced on this day: the price model first counted a {KIND_WORDS[k]} on {when(day)}"
                f"{_sources(ch, k)}, from a later report.")
    return (f"No change to debt value: this record reads as a {words}, but no record the price model counts states "
            "it (the model counts a denial of a power permit by an agency, a dated power delay of 3+ months, or a "
            "force-majeure notice citing power).")


def _sources(ch: Doc, kind: str | None) -> str:
    names = list(dict.fromkeys(r["sourceName"] for r in ch.get("records") or [] if kind is None or r["kind"] == kind))
    return f" ({', '.join(names[:3])})" if names else ""


def other_reason(row: Doc) -> str:
    """Why a record that matches no priced change changed nothing."""
    link, rule, text = row.get("link"), str(row.get("ruleHit") or ""), row.get("text") or ""
    if link == "Lenders" and re.search(r"\bL[12]\b", rule):
        return "This row is the Marden price itself crossing a threshold, not a new input to it."
    if link == "End user":
        return ("Status only: the end user's funding is not an input to the price model; it changes the site's "
                "status and alerts.")
    if link == "Tenant":
        return ("Status only: the price model reads the tenant through its own bond prices (its bond-implied CDS), "
                "not through this record.")
    if (why := procedural_reason(text)) is not None:
        return f"Procedural filing ({why}): it does not change the debt's terms, schedule or power."
    return NOT_READ


def effect_of(ch: Doc, kind: str | None, iid: str) -> Doc | None:
    pool = (ch.get("byKind") or {}).get(kind) if kind else ch.get("effects")
    return next((e for e in pool or ch.get("effects") or [] if e.get("instrumentId") == iid), None)


def effect_phrase(name: str, e: Doc) -> str:
    """'Project Jupiter Construction loans -1.4 pts (87.3 -> 85.9), YTW +0.6%' (true minus and arrow)."""
    out = f"{name} {signed(e['changePts'])} pts ({e['without']:.1f} {ARROW} {e['with']:.1f})"
    if isinstance(e.get("yieldChangePct"), int | float):
        out += f", {e.get('measure') or 'YTM'} {pct_change(e['yieldChangePct'])}"
    return out


def market_phrase(name: str, m: Doc) -> str:
    b, a = m["before"], m["after"]
    return (f"{name} {signed(m['changePts'])} pts on trades ({b['price']:.1f} {when(b['date'])} {ARROW} "
            f"{a['price']:.1f} {when(a['date'])})")
