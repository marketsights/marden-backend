"""Display text for instrument documents: names, type labels, the facts line, terms and recovery text.

Text only: every number shown here is a sourced fact or model value formatted for the screen, and a
value no fact carries is left out (terms) or null (n/d). The V1 segment of an instrument comes from
the V1 universe (marden.seed.instruments_v1, specs/00a "V1 coverage").
"""

from __future__ import annotations

import re
from typing import Any

from marden.core.db import Row
from marden.readmodels.views.instrument_inputs import fact_sourced, num
from marden.seed.instruments_v1 import CONSTRUCTION, GPU_DDTL, GPU_LEASE, NEOCLOUD, TENANT, V1_PICKS

SEGMENT_OF: dict[str, str] = {p.id: p.segment for p in V1_PICKS}
SEGMENT_ORDER = (CONSTRUCTION, GPU_LEASE, GPU_DDTL, NEOCLOUD, TENANT)
KIND_LABELS = {
    "abs": "ABS",
    "cmbs": "CMBS",
    "muni": "Municipal bonds",
    "facility": "Credit facility",
    "other": "Other debt",
}
DASH = "\u2013"  # en dash for ranges: "45c (32\u201358c)"
_SUFFIX = re.compile(r",?\s+(Inc\.?|Corp\.?|Corporation|Ltd\.?|LLC|L\.P\.|LP|plc)$", re.I)


def short_name(name: str | None) -> str | None:
    """'Applied Digital Corp.' -> 'Applied Digital'; 'Project Jupiter (Stargate New Mexico)' ->
    'Project Jupiter'."""
    if not name:
        return None
    text = re.sub(r"\s*\([^)]*\)\s*$", "", name.strip())
    while True:
        trimmed = _SUFFIX.sub("", text).strip()
        if trimmed == text:
            return text or None
        text = trimmed


def display_name(inst: Row) -> str:
    """Who + what: 'CoreWeave 9.00% Sr Notes 2031', 'Project Jupiter Construction loans'. The who is
    the issuer's parent, else a non-SPV issuer, else the financed site, else the issuer."""
    who = None
    if inst.get("parent_name"):
        who = short_name(inst["parent_name"])
    elif inst.get("issuer_entity") and inst.get("issuer_kind") not in ("spv",):
        who = short_name(inst["issuer_entity"])
    elif inst.get("site_name"):
        who = short_name(inst["site_name"])
    elif inst.get("issuer_entity"):
        who = short_name(inst["issuer_entity"])
    name = inst["name"]
    if not who or name.lower().startswith(who.lower().split()[0]):
        return name
    return f"{who} {name}"


def type_label(inst: Row, seniority: str | None) -> str:
    """Free-text type the Instruments table shows and `?type=` matches (spec 06 chips group them:
    construction loans, chip leases, GPU facilities, bonds, tenant credit). No commas: the API reads
    `?type=a,b` as a list of types."""
    kind = inst["kind"]
    segment = SEGMENT_OF.get(inst["id"])
    sen = (seniority or "").lower()
    secured = "secured" in sen and "unsecured" not in sen
    if segment == TENANT:
        what = (
            "convertible notes" if kind == "convert" else ("senior unsecured notes" if "unsecured" in sen else "notes")
        )
        return f"Tenant credit · {what}"
    if kind == "construction_loan":
        return "Construction loan"
    if kind == "ddtl":
        return "GPU delayed-draw facility"
    if kind == "lease":
        return "Chip lease"
    if kind == "convert":
        return "Convertible notes"
    if segment == GPU_LEASE:
        return "Lease vehicle loan · senior secured" if kind == "term_loan" else "Lease vehicle notes · senior secured"
    if segment == GPU_DDTL:
        return "GPU term loan · senior secured" if kind == "term_loan" else "GPU-backed notes · senior secured"
    if segment == CONSTRUCTION and kind == "term_loan":
        return "Project loan · senior secured"
    if kind == "bond":
        if segment == CONSTRUCTION:
            return "Project bonds · senior secured" if secured else "Project bonds"
        return "Senior secured notes" if secured else ("Senior unsecured notes" if "unsecured" in sen else "Notes")
    if kind == "term_loan":
        return "Term loan · senior secured" if secured else "Term loan"
    return KIND_LABELS.get(kind, kind.replace("_", " ").capitalize())


def segment_rank(instrument_id: str) -> int:
    segment = SEGMENT_OF.get(instrument_id)
    return SEGMENT_ORDER.index(segment) if segment in SEGMENT_ORDER else len(SEGMENT_ORDER)


# ---------------------------------------------------------------- numbers as text
def billions(usd: Any) -> float | None:
    value = num(usd)
    return None if value is None else round(value / 1e9, 4)


def money_text(usd: Any) -> str | None:
    value = num(usd)
    if value is None:
        return None
    if abs(value) >= 10e9:
        return f"${value / 1e9:.1f}B"
    if abs(value) >= 1e9:
        return f"${value / 1e9:.2f}B"
    return f"${value / 1e6:,.0f}M"


def price_text(value: Any) -> str | None:
    """A price per 100 of par, one decimal (spec 00 Formats: '88.6')."""
    v = num(value)
    return None if v is None else f"{v:.1f}"


def yield_text(value: Any) -> str | None:
    """A yield in percent, one decimal with % (spec 00 Formats: '15.6%')."""
    v = num(value)
    return None if v is None else f"{v:.1f}%"


def text_of(value: Any) -> str | None:
    """A fact value as display text (lists joined, objects skipped)."""
    if value is None or isinstance(value, dict):
        return None
    if isinstance(value, list):
        parts = [text_of(v) for v in value]
        joined = "; ".join(p for p in parts if p)
        return joined or None
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value).strip() or None


def maturity_text(facts: dict[str, Row]) -> str | None:
    row = facts.get("maturity") or facts.get("maturity_text")
    return text_of(row["value"]) if row else None


# ---------------------------------------------------------------- recovery
def _pct(v: Any) -> int | None:
    n = num(v)
    return None if n is None else round(n)


def _band(base: int | None, low: int | None, high: int | None) -> str | None:
    if base is None and low is None and high is None:
        return None
    if base is None:
        return f"{low}{DASH}{high}c" if low is not None and high is not None else f"{low if low is not None else high}c"
    if low is not None and high is not None and (low, high) != (base, base):
        return f"{base}c ({low}{DASH}{high}c)"
    return f"{base}c"


def recovery_from_facts(facts: dict[str, Row]) -> dict[str, Any] | None:
    """Recovery if default, cents per dollar, from the recovery facts (waterfall, project, GPU)."""
    row = facts.get("recovery_waterfall")
    if row and isinstance(row["value"], dict):
        v = row["value"]
        pick = {
            k: _pct((v.get(k) or {}).get("total_recovery_pct", (v.get(k) or {}).get("recovery_pct")))
            for k in ("low", "base", "high")
        }
        text = _band(pick["base"], pick["low"], pick["high"])
        if text:
            return fact_sourced(row, text)
    row = facts.get("recovery_project")
    if row and isinstance(row["value"], dict):
        v = row["value"]
        text = _band(_pct(v.get("base_pct")), _pct(v.get("low_pct")), _pct(v.get("high_pct")))
        if text:
            return fact_sourced(row, text)
    row = facts.get("recovery_gpu")
    if row and isinstance(row["value"], dict):
        text = gpu_recovery_text(row["value"])
        if text:
            return fact_sourced(row, text)
    return None


def gpu_recovery_text(value: dict[str, Any]) -> str | None:
    """'91-100c' (en dash) from the conservative and moderate GPU-only recovery."""
    low, high = _pct(value.get("recovery_conservative_pct")), _pct(value.get("recovery_moderate_pct"))
    if low is None and high is None:
        return None
    if low is None or high is None or low == high:
        return f"{low if low is not None else high}c"
    return f"{min(low, high)}{DASH}{max(low, high)}c"


def recovery_from_output(value: Any) -> str | None:
    """A recovery model value as text: a number (cents or a 0..1 share), a string, or {base, low, high}."""
    if isinstance(value, str):
        return value
    n = num(value)
    if n is not None:
        return f"{round(n * 100 if n <= 1 else n)}c"
    if isinstance(value, dict):

        def cents(k: str) -> int | None:
            x = num(value.get(k))
            return None if x is None else round(x * 100 if x <= 1 else x)

        return _band(cents("base") if value.get("base") is not None else cents("value"), cents("low"), cents("high"))
    return None


# ---------------------------------------------------------------- terms and the facts line
def _covenant(value: Any) -> str | None:
    if not isinstance(value, dict):
        return text_of(value)
    parts = [value.get("metric"), value.get("threshold")]
    head = ": ".join(str(p) for p in parts if p)
    tail = value.get("consequence")
    return f"{head} ({tail})" if head and tail else (head or None)


def terms(inst: Row, facts: dict[str, Row]) -> dict[str, dict[str, Any]]:
    """Terms card (spec 07): issuer, coupon, maturity, size, ranking, guarantees, covenants and the
    other filed terms, each Sourced<string> from its fact. Absent terms are left out."""
    out: dict[str, dict[str, Any]] = {}

    def put(label: str, row: Row | None, text: str | None) -> None:
        if row is not None and text:
            out[label] = fact_sourced(row, text)  # type: ignore[assignment]

    put(
        "Issuer",
        facts.get("issuer_name") or facts.get("source_urls"),
        text_of(facts["issuer_name"]["value"]) if facts.get("issuer_name") else inst.get("issuer_name"),
    )
    put("Coupon", facts.get("coupon"), coupon_text(facts))
    if facts.get("rate_text"):
        put("Rate", facts["rate_text"], text_of(facts["rate_text"]["value"]))
    mat = facts.get("maturity") or facts.get("maturity_text")
    put("Maturity", mat, date_words(maturity_text(facts) or "") or None)
    size = facts.get("amount_usd")
    if size is not None:
        basis = facts.get("amount_basis")
        suffix = (
            " drawn"
            if basis and basis["value"] == "drawn"
            else (" held by funds" if basis and basis["value"] == "holder_par" else "")
        )
        put("Size", size, (money_text(size["value"]) or "") + suffix if money_text(size["value"]) else None)
    if facts.get("commitment_usd"):
        put("Commitment", facts["commitment_usd"], money_text(facts["commitment_usd"]["value"]))
    if facts.get("drawn_usd"):
        put("Drawn", facts["drawn_usd"], money_text(facts["drawn_usd"]["value"]))
    rank = facts.get("ranking_text") or facts.get("seniority")
    put("Ranking", rank, text_of(rank["value"]) if rank else None)
    if facts.get("collateral_text"):
        put("Collateral", facts["collateral_text"], text_of(facts["collateral_text"]["value"]))
    if facts.get("backstop"):
        put("Guarantees", facts["backstop"], text_of(facts["backstop"]["value"]))
    if facts.get("covenant_test"):
        put("Covenants", facts["covenant_test"], _covenant(facts["covenant_test"]["value"]))
    if facts.get("amortization"):
        put("Amortization", facts["amortization"], text_of(facts["amortization"]["value"]))
    if facts.get("rating"):
        put("Rating", facts["rating"], text_of(facts["rating"]["value"]))
    if facts.get("status"):
        put("Status", facts["status"], text_of(facts["status"]["value"]))
    ids = facts.get("identifiers")
    if ids and isinstance(ids["value"], list):
        cusips = [i.get("value") for i in ids["value"] if isinstance(i, dict) and i.get("type") == "cusip"]
        put("CUSIP", ids, ", ".join(c for c in cusips if c) or None)
    return out


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_ISO_DAY = re.compile(r"^(\d{4})-(\d{2})(?:-(\d{2}))?$")


def date_words(text: str) -> str:
    """'2031-02' -> 'Feb 2031', '2030-04-15' -> 'Apr 15, 2030'; other text unchanged."""
    m = _ISO_DAY.match(text.strip())
    if not m or not 1 <= int(m.group(2)) <= 12:
        return text
    month = _MONTHS[int(m.group(2)) - 1]
    return f"{month} {int(m.group(3))}, {m.group(1)}" if m.group(3) else f"{month} {m.group(1)}"


def coupon_text(facts: dict[str, Row]) -> str | None:
    """'9.00% fixed': the filed coupon with its filed type (fixed, floating) when the text does not
    already say it ('SOFR+4.00%' is floating as written)."""
    coupon = text_of(facts["coupon"]["value"]) if facts.get("coupon") else None
    kind = (text_of(facts["coupon_type"]["value"]) or "").lower() if facts.get("coupon_type") else ""
    if not coupon or kind not in ("fixed", "floating") or kind in coupon.lower():
        return coupon
    if kind == "floating" and re.search(r"sofr|libor|euribor|base rate|\+", coupon, re.I):
        return coupon
    return f"{coupon} {kind}"


def facts_line(facts: dict[str, Row]) -> str:
    """'Senior unsecured · 144A · 9.00% fixed · $1.75B · matures Feb 2031' from the instrument's facts
    (spec 07 header: seniority, format, size, ...)."""
    parts: list[str] = []
    if facts.get("seniority") and text_of(facts["seniority"]["value"]):
        parts.append(text_of(facts["seniority"]["value"]))  # type: ignore[arg-type]
    if facts.get("is_144a") and facts["is_144a"]["value"] is True:
        parts.append("144A")
    coupon = coupon_text(facts)
    if coupon:
        parts.append(coupon)
    if facts.get("amount_usd") and money_text(facts["amount_usd"]["value"]):
        basis = facts.get("amount_basis")
        parts.append(
            money_text(facts["amount_usd"]["value"]) + (" drawn" if basis and basis["value"] == "drawn" else "")
        )  # type: ignore[operator]
    mat = maturity_text(facts)
    if mat:
        parts.append(f"matures {date_words(mat)}")
    return " · ".join(parts)
