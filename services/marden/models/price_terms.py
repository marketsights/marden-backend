"""Instrument terms for the price model, read from the instrument's facts and its issue comp.

Contract terms (coupon, margin, maturity) are written by the seed with the archive's load date as
`valid_from`, although they were public when the instrument was issued (8-K, offering release, fund
filings). The model therefore reads them from today's facts and uses them on any day on or after the
instrument's issue date (its issue comp's `issue_date`, else its first price observation): a backtest
never sees an instrument before it existed. Everything else is read point in time.

Parsing is code on the stored values: `coupon_type`, `coupon_rate_pct`, `coupon_spread_bp`, else the
`coupon` text ('9.25%', 'SOFR+2.50% ...', 'S+2.375%'); `maturity` as 'YYYY-MM-DD', 'YYYY-MM' (the 15th)
or 'YYYY' (July 1). The issue comp (series `issue_comp:<fid>`, seed q3x comp.json) is linked by site,
deal type, coupon or margin and maturity year; the link and its score go into the output basis.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from marden.core.db import Conn, Row
from marden.models import pit
from marden.models.price_bonds import Terms
from marden.readmodels.context import business_today

TERM_FIELDS = ("coupon", "coupon_type", "coupon_rate_pct", "coupon_spread_bp", "coupon_benchmark", "maturity",
               "amount_usd", "tenants", "seniority", "gpu_model", "gpu_collateral", "backstop", "identifiers")
_PCT = re.compile(r"^\s*~?(\d+(?:\.\d+)?)\s*%")
_FLOAT = re.compile(r"\b(?:Term\s+|Daily compounded\s+|1M\s+|3M\s+|Monthly\s+)?(?:SOFR|S)\s*\+\s*(\d+(?:\.\d+)?)"
                    r"\s*(%|bp)?", re.I)
_ISSUED_AT = re.compile(r"issued at (\d{2,3}(?:\.\d+)?)", re.I)
LINK_MIN = 7
_NON_USD = re.compile(r"\b(EUR|GBP|CHF|JPY|AUD|CAD)\b")
KIND_TYPES = {
    "bond": {"dc_project_bond", "gpu_abs", "gpu_loan"},
    "construction_loan": {"dc_construction_loan"},
    "term_loan": {"gpu_loan", "dc_construction_loan"},
    "ddtl": {"gpu_loan", "dc_construction_loan"},
    "abs": {"dc_abs"},
    "cmbs": {"dc_abs"},
    "lease": {"gpu_lease", "gpu_loan"},
}


def parse_maturity(value: Any) -> tuple[date | None, str | None]:
    if not isinstance(value, str):
        return None, None
    v = value.strip()[:10]
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            return date.fromisoformat(v), None
        if re.fullmatch(r"\d{4}-\d{2}", v[:7]):
            return date(int(v[:4]), int(v[5:7]), 15), f"maturity {v[:7]} taken as the 15th"
        if re.fullmatch(r"\d{4}", v[:4]):
            return date(int(v[:4]), 7, 1), f"maturity {v[:4]} taken as July 1"
    except ValueError:
        return None, None
    return None, None


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


@dataclass
class Instrument:
    id: str
    name: str
    kind: str
    issuer: str | None
    issuer_name: str
    site_slug: str | None
    facts: dict[str, Row] = field(default_factory=dict)
    terms: Terms | None = None
    terms_missing: list[str] = field(default_factory=list)
    comp: dict[str, Any] | None = None

    def value(self, fld: str) -> Any:
        row = self.facts.get(fld)
        return None if row is None else row["value"]

    @property
    def usd(self) -> bool:
        return not _NON_USD.search(self.name or "")

    @property
    def issue_date(self) -> date | None:
        return self.comp["issue_date"] if self.comp else None

    @property
    def issue_price(self) -> float:
        return float(self.comp.get("issue_price") or 100.0) if self.comp else 100.0


def build_terms(inst: Instrument) -> tuple[Terms | None, list[str]]:
    missing: list[str] = []
    notes: list[str] = []
    maturity, note = parse_maturity(inst.value("maturity"))
    if maturity is None and inst.comp is not None:
        maturity, note = parse_maturity(inst.comp.get("maturity"))
        if maturity is not None:
            notes.append("maturity from the issue comp")
    if note:
        notes.append(note)
    if maturity is None:
        missing.append(f"instrument {inst.id}: maturity")
    text = inst.value("coupon")
    text = text if isinstance(text, str) else ""
    ctype = inst.value("coupon_type")
    rate = _num(inst.value("coupon_rate_pct"))
    margin = _num(inst.value("coupon_spread_bp"))
    fm = _FLOAT.search(text)
    if margin is None and fm:
        margin = float(fm.group(1)) * (1.0 if (fm.group(2) or "%").lower() == "bp" else 100.0)
    if rate is None and not fm:
        m = _PCT.match(text)
        rate = float(m.group(1)) if m else None
    if ctype not in ("fixed", "floating"):
        ctype = "floating" if margin is not None and rate is None else ("fixed" if rate is not None else None)
    if ctype is None and inst.comp is not None:  # the comp's own rate text
        comp_rate = str(inst.comp.get("rate") or "")
        fm = _FLOAT.search(comp_rate)
        if fm:
            ctype, margin = "floating", float(fm.group(1)) * 100.0
            notes.append("margin from the issue comp")
        elif inst.comp.get("coupon_pct") is not None:
            ctype, rate = "fixed", float(inst.comp["coupon_pct"])
            notes.append("coupon from the issue comp")
        elif inst.comp.get("benchmark") == "SOFR" and inst.comp.get("issue_spread_bp") is not None:
            ctype, margin = "floating", float(inst.comp["issue_spread_bp"])
            notes.append("margin = the comp's spread at issue")
    if ctype == "fixed" and rate is None:
        missing.append(f"instrument {inst.id}: coupon rate")
    if ctype == "floating" and margin is None:
        missing.append(f"instrument {inst.id}: floating margin")
    if ctype is None:
        missing.append(f"instrument {inst.id}: coupon terms")
    if missing:
        return None, missing
    assert maturity is not None and ctype is not None
    freq = 2 if ctype == "fixed" else 4
    return Terms(inst.id, maturity, ctype, rate if ctype == "fixed" else None,
                 margin if ctype == "floating" else None, freq, tuple(notes)), []


def load_instruments(conn: Conn, ids: Sequence[str] | None = None, *, issuers: Sequence[str] | None = None,
                     kinds: Sequence[str] | None = None) -> dict[str, Instrument]:
    sql = "SELECT id, name, kind, issuer_entity_id, issuer_name, site_slug FROM instrument WHERE true"
    params: dict[str, Any] = {}
    if ids is not None:
        sql += " AND id = ANY(%(ids)s)"
        params["ids"] = list(ids)
    if issuers is not None:
        sql += " AND issuer_entity_id = ANY(%(issuers)s)"
        params["issuers"] = list(issuers)
    if kinds is not None:
        sql += " AND kind = ANY(%(kinds)s)"
        params["kinds"] = list(kinds)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    out = {r["id"]: Instrument(r["id"], r["name"], r["kind"], r["issuer_entity_id"], r["issuer_name"],
                               r["site_slug"]) for r in rows}
    if not out:
        return out
    for r in pit.fact_rows(conn, business_today(), "instrument", list(out), TERM_FIELDS):
        out[r["subject_id"]].facts.setdefault(r["field"], r)
    return out


def load_comps(conn: Conn, as_of: date) -> list[dict[str, Any]]:
    """Issue comps public on `as_of` (valid_from = the issue date): one dict per comp."""
    rows = pit.fact_rows(conn, as_of, "series", None, ["deal_terms", "issue_date", "issue_spread_bp", "tier"])
    comps: dict[str, dict[str, Any]] = {}
    for r in rows:
        if not r["subject_id"].startswith("issue_comp:"):
            continue
        c = comps.setdefault(r["subject_id"], {"id": r["subject_id"], "source_name": r["source_name"],
                                                "source_url": r["source_url"]})
        c.setdefault(r["field"], r["value"])
    out = []
    for c in comps.values():
        terms = c.get("deal_terms") if isinstance(c.get("deal_terms"), dict) else {}
        try:
            issue = date.fromisoformat(str(c.get("issue_date"))[:10])
        except ValueError:
            continue
        rate_text = str(terms.get("rate") or "")
        m = _ISSUED_AT.search(rate_text)
        out.append({
            "id": c["id"], "issue_date": issue, "issue_spread_bp": _num(c.get("issue_spread_bp")),
            "tier": (c.get("tier") or {}).get("code") if isinstance(c.get("tier"), dict) else c.get("tier"),
            "type_code": terms.get("type_code"), "site_slug": terms.get("site_slug"), "rate": rate_text,
            "coupon_pct": _num(terms.get("coupon_pct")), "maturity": terms.get("maturity"),
            "tenor_years": _num(terms.get("tenor_years")), "benchmark": terms.get("benchmark"),
            "rating": terms.get("rating"), "contract": terms.get("contract"), "name": terms.get("name"),
            "issue_price": _num(terms.get("issue_price")) or (float(m.group(1)) if m else None),
            "spread_method": terms.get("spread_method"), "tenant": terms.get("tenant_summary"),
            "source_name": c["source_name"], "source_url": c["source_url"],
        })
    return sorted(out, key=lambda c: (c["issue_date"], c["id"]))


def _digits(text: str) -> str:
    return re.sub(r"[^0-9]", "", text)


def link_comp(inst: Instrument, comps: Sequence[dict[str, Any]]) -> tuple[dict[str, Any] | None, int]:
    """The issue comp that is this instrument (best score >= LINK_MIN and unique): site +3, deal type +2,
    coupon or margin +3, maturity year +2, DDTL number +4, phase number +3."""
    rate = _num(inst.value("coupon_rate_pct"))
    margin = _num(inst.value("coupon_spread_bp"))
    mat, _ = parse_maturity(inst.value("maturity"))
    name_digits = _digits(inst.name.split("(")[0]) if "DDTL" in inst.name.upper() else ""
    m_phase = re.search(r"\bphase\s*(\d)", inst.name, re.I)
    phase = m_phase.group(1) if m_phase else None
    scored = []
    for c in comps:
        s = 0
        if inst.site_slug and c["site_slug"] == inst.site_slug:
            s += 3
        if c["type_code"] in KIND_TYPES.get(inst.kind, set()):
            s += 2
        if rate is not None and ((c["coupon_pct"] is not None and abs(rate - c["coupon_pct"]) < 0.01)
                                 or f"{rate:g}%" in c["rate"]):
            s += 3
        if margin is not None and c["issue_spread_bp"] is not None and c["benchmark"] == "SOFR" \
                and abs(margin - c["issue_spread_bp"]) < 6:
            s += 3
        cm, _ = parse_maturity(c["maturity"])
        if mat is not None and cm is not None and cm.year == mat.year:
            s += 2
        if name_digits and c["id"].upper().endswith(f"DDTL{name_digits}"):
            s += 4
        if phase and re.search(rf"_P{phase}(?:_|$)", c["id"].upper()):
            s += 3
        scored.append((s, c))
    if not scored:
        return None, 0
    scored.sort(key=lambda sc: -sc[0])
    best = scored[0][0]
    if best < LINK_MIN or (len(scored) > 1 and scored[1][0] == best):
        return None, best
    return scored[0][1], best


def attach(instruments: dict[str, Instrument], comps: Sequence[dict[str, Any]]) -> None:
    for inst in instruments.values():
        inst.comp, score = link_comp(inst, comps)
        if inst.comp is not None:
            inst.comp = {**inst.comp, "linkScore": score}
        inst.terms, inst.terms_missing = build_terms(inst)
