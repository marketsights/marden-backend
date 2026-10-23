"""Plain words for the capacity model (spec 27): each method's evidence as a sentence built from the fact's
fields (never the raw value or the field name), the one-line reason behind the confidence, and the hover
notes that say which GPU platform each end of the GPU and GPU value ranges assumes.

- Evidence: 'MISO large-load request: 280 MW for Building 2 and remaining capacity (Oct 20, 2026, under
  construction)'; '8 backup generators counted on site (Aug 5, 2026)'; 'Crusoe Abilene generation: 360 MW
  stated capacity (May 1, 2026)'. A field without its own sentence reads as its label and its plain value.
- Confidence reason: what the answer rests on and whether a source states the GPU platform ('only a
  utility large-load request, no stated GPU platform').
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from marden.core.db import Row
from marden.models.sourced import num

DASH = "\u2013"
TIMES = "\u00d7"
_LIMIT = 90
_WORDS = {"mw": "MW", "it": "IT", "gpu": "GPU", "sqft": "sq ft", "iso": "ISO", "cod": "COD", "pue": "PUE"}


def short_date(d: date | str | None) -> str:
    """'Oct 20, 2026'."""
    if isinstance(d, str):
        try:
            d = date.fromisoformat(d[:10])
        except ValueError:
            return d
    return f"{d:%b} {d.day}, {d.year}" if isinstance(d, date) else ""


def _n(v: Any) -> str:
    x = num(v)
    if x is None:
        return str(v)
    return f"{x:,.0f}" if x == int(x) else f"{x:,.1f}"


def _clip(text: str, limit: int = _LIMIT) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:-\u2013 ")
    return f"{cut}\u2026"


def _label(field: str) -> str:
    words = [_WORDS.get(w, w) for w in field.split("_")]
    return " ".join(words).capitalize() if words[0] == words[0].lower() else " ".join(words)


def plain(value: Any) -> str:
    """A fact value in words: numbers with separators, lists joined, objects as their scalar parts."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int | float):
        return _n(value)
    if isinstance(value, list):
        return ", ".join(plain(v) for v in value if v not in (None, ""))
    if isinstance(value, dict):
        named = value.get("name") or value.get("value")
        if named is not None:
            return plain(named)
        return "; ".join(f"{_label(str(k)).lower()} {plain(v)}" for k, v in value.items()
                         if isinstance(v, str | int | float) and not isinstance(v, bool) and v != "")
    return str(value or "").strip()


def _what(name: str) -> str:
    """The part of a request's name after the project's own name: 'Polaris Forge 2 (Harwood AI Factory),
    Building 2 and remaining capacity' -> 'Building 2 and remaining capacity'."""
    head, sep, rest = name.partition(", ")
    return _clip(rest if sep and rest.strip() else head)


def _when(r: Row, *extra: str) -> str:
    parts = [short_date(r.get("valid_from")), *[e for e in extra if e]]
    return f" ({', '.join(p for p in parts if p)})" if any(parts) else ""


def _mw(v: Any) -> float | None:
    if isinstance(v, dict):
        return num(v.get("mw") if v.get("mw") is not None else v.get("value"))
    return num(v)


def _chips(v: Any) -> str:
    if isinstance(v, dict):
        items = sorted(((k, num(n)) for k, n in v.items() if num(n)), key=lambda kv: -(kv[1] or 0))
        parts = [f"{n:,.0f} {k}" for k, n in items]
        return " and ".join(parts) if len(parts) <= 2 else ", ".join(parts)
    return plain(v)


def evidence_text(r: Row, names: dict[str, str] | None = None) -> str:
    """One fact in a sentence, from its fields."""
    field, v = r["field"], r["value"]
    who = (names or {}).get(r["subject_id"]) or ""
    if len(who) > 48 and " (" in who:  # 'Entergy Louisiana Hyperion CCGTs (first 3 plants, ...)'
        who = who.split(" (")[0]
    who = _clip(who, 60)
    if field == "large_load_request" and isinstance(v, dict):
        iso = str(v.get("iso") or v.get("utility") or "Utility").strip()
        mw, what, stage = _mw(v), _what(str(v.get("name") or v.get("serves_text") or "")), str(v.get("stage") or "")
        size = f"{_n(mw)} MW" if mw else "MW not stated"
        return f"{iso} large-load request: {size}{f' for {what}' if what else ''}{_when(r, stage)}"
    if field == "grid_mw":
        mw = _mw(v)
        util = v.get("utility") if isinstance(v, dict) else None
        return f"Grid service: {_n(mw) if mw else plain(v)} MW{f' from {util}' if util else ''}{_when(r)}"
    if field in ("facility_mw", "it_mw"):
        what = "Facility" if field == "facility_mw" else "IT"
        mw = _mw(v)
        return f"{what} capacity stated: {_n(mw) + ' MW' if mw else _clip(plain(v))}{_when(r)}"
    if field == "backup_generator_count":
        return f"{_n(v)} backup generator{'' if num(v) == 1 else 's'} counted on site{_when(r)}"
    if field in ("sqft_total", "sqft"):
        whose = f"{who} floor area" if who else "Floor area"
        return f"{whose}: {_n(v)} sq ft{_when(r)}"
    if field == "capacity_mw":
        return f"{who or 'Serving plant'}: {_n(_mw(v))} MW stated capacity{_when(r)}"
    if field == "count":
        return f"{who or 'Serving plant'}: {_n(v)} generating unit{'' if num(v) == 1 else 's'}{_when(r)}"
    if field == "rating_mw_each":
        return f"{who or 'Serving plant'}: {_n(v)} MW per unit{_when(r)}"
    if field == "fuel":
        return f"{who or 'Serving plant'}: units run on {plain(v).lower()}{_when(r)}"
    if field in ("chips", "chips_planned"):
        lead = "Disclosed chips" if field == "chips" else "Planned chips"
        return f"{lead}: {_chips(v)}{_when(r)}"
    if field in ("chip_types", "gpu_type"):
        return f"Stated GPU platform: {plain(v)}{_when(r)}"
    return f"{_label(field)}: {_clip(plain(v))}{_when(r)}"


def evidence(rows: list[Row], names: dict[str, str] | None = None, limit: int = 6) -> list[dict[str, Any]]:
    return [
        {
            "date": r["valid_from"].isoformat(),
            "text": evidence_text(r, names),
            "tag": str(r["tag"]).strip(),
            "sourceName": r.get("source_name") or "",
            "sourceUrl": r.get("source_url") or "",
        }
        for r in rows[:limit]
    ]


# ---------------------------------------------------------------- confidence and facility basis
def method_phrase(method: str, rows: list[Row]) -> str:
    """What a method's figure rests on, as a noun phrase."""
    n = len(rows)
    if method == "Filed":
        filed = bool(rows) and str(rows[0]["tag"]).strip() == "F"
        return "a filed facility MW" if filed else "a reported facility MW"
    if method == "Utility load":
        kinds = {r["field"] for r in rows}
        if kinds == {"large_load_request"} and n == 1:
            return "a utility large-load request"
        return "a utility grid service figure" if n == 1 else f"{n} utility load records"
    return {
        "Prime generation": "the serving plants' generation",
        "Disclosed GPU count": "a disclosed chip count",
        "Campus share": "the campus's stated MW split by building footprint",
        "Footprint": "floor area at peer campuses' power density",
        "Backup generators": "a generator count without a rating",
    }.get(method, method.lower())


def confidence_reason(confidence: str, known: list[tuple[str, list[Row]]], gpu: str) -> str:
    """'only a utility large-load request, no stated GPU platform'. `known`: the methods with a figure in
    rank order (method, rows); `gpu`: the GPU half of the reason."""
    phrases = [method_phrase(m, rows) for m, rows in known]
    if confidence == "High" and phrases:
        facility = phrases[0]
    elif not phrases:
        facility = "no method has a facility MW figure"
    elif len(phrases) == 1:
        facility = f"only {phrases[0]}"
    elif confidence == "Medium":
        facility = f"{phrases[0]} and {phrases[1]} agree within 25%"
    else:
        facility = f"{phrases[0]} and {phrases[1]} differ by more than 25%"
    return f"{facility}, {gpu}" if gpu else facility


def facility_basis(best_method: str | None, known: list[tuple[str, list[Row]]]) -> str:
    """Under the facility MW figure when it is one number: where it comes from."""
    if best_method is None or not known:
        return ""
    if best_method == "combined" and len(known) >= 2:
        return f"overlap of {known[0][0].lower()} and {known[1][0].lower()}"
    rows = next((r for m, r in known if m == best_method), [])
    return f"from {method_phrase(best_method, rows)}"


# ---------------------------------------------------------------- GPU platform notes
def _kw(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".")


def _usd(x: float) -> str:
    return f"${x / 1e9:,.1f}B" if x >= 1e9 else f"${x / 1e6:,.0f}M" if x >= 1e6 else f"${x:,.0f}"


def _k(x: float) -> str:
    return f"{x / 1000:,.0f}k" if x >= 10000 else f"{x:,.0f}"


def gpu_note(basis: dict[str, Any], share: tuple[float, float, float], it: dict[str, Any]) -> str:
    """Hover on the GPU count: the platform (stated or which one each end assumes) and the arithmetic."""
    plats = {p["chip"]: p for p in basis["platforms"]}
    ends = basis["gpus"]
    lo, base, hi = plats[ends["low"]], plats[ends["base"]], plats[ends["high"]]
    arithmetic = (f"GPUs = IT MW {TIMES} GPU share of IT load ({share[0]:.0%}{DASH}{share[2]:.0%}, "
                  f"base {share[1]:.0%}) "
                  f"\u00f7 chip power per GPU.")
    if basis["stated"]:
        return (f"{base['chip']} as stated{f' ({basis['statedBy']})' if basis.get('statedBy') else ''}: "
                f"{_kw(base['kwPerGpu'])} kW per GPU, {_kw(base['itKwPerGpu'])} kW of IT load per GPU at the base "
                f"share. {arithmetic} Low end: low IT MW and share; high end: high IT MW and share.")
    why = basis.get("why") or "No source states the GPU platform."
    if lo["chip"] == hi["chip"]:
        ends_text = f"Both ends assume {lo['chip']} ({_kw(lo['kwPerGpu'])} kW per GPU)."
    else:
        ends_text = (f"Low end ({_k(it['low'] * share[0] * 1000 / lo['kwPerGpu'])}) assumes {lo['chip']} at "
                     f"{_kw(lo['kwPerGpu'])} kW per GPU ({_kw(lo['itKwPerGpu'])} kW of IT load each) on the low IT MW "
                     f"and share; high end ({_k(it['high'] * share[2] * 1000 / hi['kwPerGpu'])}) assumes {hi['chip']} "
                     f"at {_kw(hi['kwPerGpu'])} kW ({_kw(hi['itKwPerGpu'])} kW of IT load) on the high.")
    return f"{why} {ends_text} Base assumes {base['chip']}. {arithmetic}"


def value_note(basis: dict[str, Any]) -> str:
    """Hover on the implied GPU value: the price per GPU each end uses."""
    plats = {p["chip"]: p for p in basis["platforms"]}
    ends = basis["value"]
    prices = "; ".join(f"{p['chip']} ${p['priceUsd']['low']:,.0f}{DASH}${p['priceUsd']['high']:,.0f}"
                       for p in basis["platforms"] if p.get("priceUsd"))
    lo, hi, base = plats[ends["low"]], plats[ends["high"]], plats[ends["base"]]
    if lo["chip"] == hi["chip"]:
        ends_text = f"Both ends use {lo['chip']}."
    else:
        ends_text = (f"Low end: {lo['chip']}'s GPU count at its low price; high end: {hi['chip']}'s at its "
                     f"high price.")
    return (f"GPUs {TIMES} new price per GPU (published ranges: {prices}). {ends_text} Base: {base['chip']} at the "
            "midpoint of its range.")
