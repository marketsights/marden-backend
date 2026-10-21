"""Linking holding lines to instruments (instrument_id stays null without a confident match).

Instrument identifiers come from the instruments loader's rows and ``identifiers`` facts, which use only the
sources docs/methods/price-sources.md allows (aiwcredit.ids, NP- id suffixes, aidebt and research text,
N-PORT lines). ``trace/match.json`` and ``trace/secs*.json`` are never read.

N-PORT lines that share an identifier (CUSIP, ISIN, LoanX ``LX######``, Bloomberg loan id ``BL#######``, or a
distinctive filer code such as PGIM's ``B8AFB91T2``) are one security. A line is linked by the first tier that
names any instrument; a tier that names more than one is ambiguous and the line stays unlinked:

1. identifier (per line): its CUSIP / ISIN / LoanX (from ``other`` or the title) / other code is an instrument's;
2. shared identifier: lines of its group resolved by tier 1 all point to one instrument;
3. cited filing: the instruments loader recomputed this instrument's fund mark from exactly this filing line;
4. issuer + coupon + maturity: issuer words, maturity (to the month or day) and coupon agree with exactly one
   instrument (fixed: same rate within 0.005; floating: a floating line).

BDC lines are grouped by facility key (borrower key, lien, spread, maturity month) across BDCs and linked by
tier 3 (the instruments loader's BDC mark line) or tier 4 (issuer words, maturity month, lien and spread or
fixed coupon). NAIC Schedule D lines link by CUSIP only. Borrower-level ``BDC-*`` placeholder instruments
(no tranche) are never link targets.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from marden.core.db import Conn
from marden.seed.holdings_lines import HoldingLine
from marden.seed.instruments_parse import cusip_valid, isin_valid, normalize_name

FACT_FIELDS = ("identifiers", "coupon_type", "coupon_rate_pct", "coupon_spread_bp", "maturity", "seniority")
_STOP = frozenset({
    "llc", "inc", "ltd", "lp", "corp", "co", "the", "and", "pty", "plc", "data", "centers", "center", "holdings",
    "holding", "holdco", "intermediate", "energy", "systems", "financing", "finance", "group", "loan", "loans",
    "parent", "company", "issuer", "revolving", "warehouse", "services", "technologies", "computer", "term",
    "first", "lien", "senior", "secured", "notes", "note", "bond", "bonds", "delayed", "draw", "fixed",
    "floating", "rate", "series", "class", "usd", "tlb", "new", "facility", "for", "of", "corporation",
})
RATE_TOLERANCE = 0.0051  # filers round the coupon (7.375 -> 7.38)


def name_tokens(*texts: Any) -> frozenset[str]:
    words = normalize_name(" ".join(str(t or "") for t in texts)).split()
    return frozenset(w for w in words if len(w) >= 3 and w not in _STOP and not w.isdigit())


def lien_of(seniority: Any, name: Any) -> str | None:
    text = f"{seniority or ''} {name or ''}".lower()
    if re.search(r"\bsub\b|subordinat", text):
        return "Sub"
    if "unsecured" in text:
        return "Unsec"
    if re.search(r"\b1l\b|first lien|senior secured|\bsecured\b", text):
        return "1L"
    return None


@dataclass
class Inst:
    id: str
    kind: str
    issuer: str
    keys: set[str]
    maturity: str | None
    coupon_type: str | None
    rate: float | None
    spread: float | None
    lien: str | None
    tokens: frozenset[str]


@dataclass
class Catalog:
    insts: dict[str, Inst]
    by_key: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    by_token: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # link targets only

    def candidates(self, tokens: frozenset[str]) -> list[Inst]:
        """Link targets sharing at least one issuer word (never the BDC-* placeholders)."""
        ids = set().union(*(self.by_token.get(t, set()) for t in tokens)) if tokens else set()
        return [self.insts[i] for i in sorted(ids)]


def load_catalog(conn: Conn) -> Catalog:
    facts: dict[str, dict[str, Any]] = defaultdict(dict)
    for r in conn.execute(
        "SELECT subject_id, field, value FROM fact_current WHERE subject_type = 'instrument' AND field = ANY(%s)",
        (list(FACT_FIELDS),),
    ).fetchall():
        facts[r["subject_id"]][r["field"]] = r["value"]
    cat = Catalog({})
    for r in conn.execute("SELECT id, kind, issuer_name, name, cusip, isin FROM instrument ORDER BY id").fetchall():
        f = facts.get(r["id"], {})
        keys = {k for k in (r["cusip"], r["isin"]) if k}
        keys |= {str(x["value"]) for x in f.get("identifiers") or [] if x.get("value")}
        mat = f.get("maturity")
        cat.insts[r["id"]] = Inst(
            id=r["id"], kind=r["kind"], issuer=r["issuer_name"], keys=keys,
            maturity=str(mat) if mat else None, coupon_type=f.get("coupon_type"),
            rate=_float(f.get("coupon_rate_pct")), spread=_float(f.get("coupon_spread_bp")),
            lien=lien_of(f.get("seniority"), r["name"]), tokens=name_tokens(r["issuer_name"]),
        )
        if not r["id"].startswith("BDC-"):
            for k in keys:
                cat.by_key[k].add(r["id"])
            for t in cat.insts[r["id"]].tokens:
                cat.by_token[t].add(r["id"])
    return cat


def _float(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- match rules


def names_agree(inst: Inst, tokens: frozenset[str]) -> bool:
    need = min(2, len(inst.tokens), len(tokens))
    return need > 0 and len(inst.tokens & tokens) >= need


def maturity_agrees(inst_mat: str | None, line_mat: str | None) -> bool:
    if not inst_mat or not line_mat or len(inst_mat) < 7 or len(line_mat) < 7:
        return False
    n = 10 if min(len(inst_mat), len(line_mat)) >= 10 else 7
    return inst_mat[:n] == line_mat[:n]


def nport_coupon_agrees(inst: Inst, kind: str | None, rate: float | None) -> bool:
    if inst.coupon_type == "fixed" and inst.rate is not None:
        return kind in ("fixed", None) and rate is not None and abs(rate - inst.rate) <= RATE_TOLERANCE
    if inst.coupon_type == "zero":
        return kind != "floating" and rate == 0
    return inst.coupon_type == "floating" and kind == "floating"


def bdc_coupon_agrees(inst: Inst, spread_bp: float | None) -> bool:
    if spread_bp is None:
        return True
    if inst.coupon_type == "floating" and inst.spread is not None:
        return abs(spread_bp - inst.spread) <= 1
    if inst.coupon_type == "fixed" and inst.rate is not None:
        return abs(spread_bp - inst.rate * 100) <= 1
    return True


def code_kind(code: str) -> str:
    if code.startswith("LX"):
        return "loanx"
    if cusip_valid(code):
        return "cusip"
    if isin_valid(code):
        return "isin"
    return "other_code"


# ---------------------------------------------------------------- grouping


class _Groups:
    """Union-find over line indexes joined by shared strong identifiers."""

    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def groupable_weak(code: str) -> bool:
    """A filer's own security code that is distinctive enough to join lines across funds (e.g. PGIM's
    'B8AFB91T2'): at least 7 characters mixing letters and digits."""
    return len(code) >= 7 and code.isalnum() and any(c.isdigit() for c in code) and any(c.isalpha() for c in code)


def nport_groups(lines: list[HoldingLine]) -> list[list[HoldingLine]]:
    groups = _Groups(len(lines))
    first: dict[str, int] = {}
    for i, line in enumerate(lines):
        for code in (*line.codes, *(c for c in line.weak_codes if groupable_weak(c))):
            if code in first:
                groups.union(first[code], i)
            else:
                first[code] = i
    out: dict[int, list[HoldingLine]] = defaultdict(list)
    for i, line in enumerate(lines):
        out[groups.find(i)].append(line)
    return list(out.values())


# ---------------------------------------------------------------- linking


Anchor = Mapping[tuple[Any, ...], set[str]]


@dataclass
class LinkReport:
    methods: Counter[str] = field(default_factory=Counter)
    unmatched: Counter[str] = field(default_factory=Counter)
    ambiguous: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))


Tier = tuple[str, Callable[[], set[str]]]


def _decide(tiers: Iterable[Tier]) -> tuple[str | None, str | None, set[str]]:
    """First tier naming any instrument decides: (instrument or None if ambiguous, method, candidates)."""
    for method, find in tiers:
        ids = find()
        if ids:
            return (next(iter(ids)) if len(ids) == 1 else None), method, ids
    return None, None, set()


def _apply(group: list[HoldingLine], decided: tuple[str | None, str | None, set[str]], report: LinkReport,
           label: str) -> None:
    """Link every line of ``group`` (all linkable) to the decided instrument, or count it unmatched."""
    iid, method, cands = decided
    for line in group:
        if iid is not None:
            line.instrument_id, line.link_method = iid, method
            report.methods[f"{line.source}.{method}"] += 1
        else:
            report.unmatched[f"{line.source}.{'ambiguous' if cands else 'no_match'}"] += 1
    if iid is None and cands and group:
        report.ambiguous[f"{group[0].source} {method}: {', '.join(sorted(cands))}"].append(label)


def _identifier_hits(cat: Catalog, lines: list[HoldingLine]) -> tuple[set[str], str]:
    hits: set[str] = set()
    kinds: Counter[str] = Counter()
    for ln in lines:
        for code in (*ln.codes, *ln.weak_codes):
            found = cat.by_key.get(code, set())
            hits |= found
            if found:
                kinds[code_kind(code)] += 1
    return hits, (kinds.most_common(1)[0][0] if kinds else "identifier")


def link_nport(lines: list[HoldingLine], cat: Catalog, anchors: Anchor, report: LinkReport) -> None:
    """Tier 1 per line (its own identifiers); lines without an identifier hit then take, as a group of lines
    sharing identifiers, the one instrument the group's resolved lines point to, else tiers 2 and 3."""
    for group in nport_groups([ln for ln in lines if ln.source == "nport"]):
        pending: list[HoldingLine] = []
        for ln in group:
            if not ln.linkable:
                report.unmatched["nport.not_debt"] += 1
                continue
            hits, method = _identifier_hits(cat, [ln])
            if hits:
                _apply([ln], _decide([(method, lambda hits=hits: hits)]), report, ln.title or ln.fund_name)
            else:
                pending.append(ln)
        if not pending:
            continue
        inherited = {ln.instrument_id for ln in group if ln.instrument_id}

        def cited(pending: list[HoldingLine] = pending) -> set[str]:
            # the instruments loader already tied this exact line to the instrument: one shared word suffices
            return {iid for ln in pending for iid in anchors.get((ln.filing_url, ln.period_end, ln.raw_mark), set())
                    if cat.insts[iid].tokens & name_tokens(ln.issuer_name, ln.title)}

        def terms(pending: list[HoldingLine] = pending) -> set[str]:
            out: set[str] = set()
            for ln in pending:
                toks = name_tokens(ln.issuer_name, ln.title)
                out |= {i.id for i in cat.candidates(toks) if names_agree(i, toks)
                        and maturity_agrees(i.maturity, ln.maturity)
                        and nport_coupon_agrees(i, ln.coupon_kind, ln.coupon_rate)}
            return out

        label = f"{pending[0].title or pending[0].issuer_name} ({len(pending)} lines)"
        tiers: list[Tier] = [("shared_identifier", lambda inherited=inherited: inherited), ("cited_filing", cited),
                             ("issuer_coupon_maturity", terms)]
        _apply(pending, _decide(tiers), report, label)


def link_bdc(lines: list[HoldingLine], cat: Catalog, anchors: Anchor, report: LinkReport) -> None:
    groups: dict[tuple[Any, ...], list[HoldingLine]] = defaultdict(list)
    for ln in lines:
        if ln.source == "bdc":
            groups[ln.facility_key].append(ln)
    for fkey, group in groups.items():
        toks = name_tokens(*sorted({f"{ln.bkey} {ln.issuer_name}" for ln in group}))
        head = group[0]

        def cited(group: list[HoldingLine] = group, toks: frozenset[str] = toks) -> set[str]:
            return {iid for ln in group for iid in anchors.get((ln.filing_url, ln.raw_mark), set())
                    if cat.insts[iid].tokens & toks and maturity_agrees(cat.insts[iid].maturity, ln.maturity)}

        def terms(head: HoldingLine = head, toks: frozenset[str] = toks) -> set[str]:
            return {i.id for i in cat.candidates(toks) if names_agree(i, toks)
                    and maturity_agrees(i.maturity, head.maturity)
                    and (i.lien is None or head.lien is None or i.lien == head.lien)
                    and bdc_coupon_agrees(i, head.spread_bp)}

        label = "|".join(str(v) for v in fkey)
        _apply(group, _decide([("cited_filing", cited), ("issuer_coupon_maturity", terms)]), report, label)


def link_naic(lines: list[HoldingLine], cat: Catalog, report: LinkReport) -> None:
    for ln in lines:
        if ln.source == "naic":
            hits, method = _identifier_hits(cat, [ln])
            _apply([ln], _decide([(method, lambda hits=hits: hits)]), report, f"{ln.fund_name}: {ln.title}")


def nport_anchors(marks: Iterable[Any]) -> dict[tuple[str, date, Decimal], set[str]]:
    out: dict[tuple[str, date, Decimal], set[str]] = defaultdict(set)
    for m in marks:
        out[(m.source_url, m.obs_date, m.price)].add(m.instrument_id)
    return out


def bdc_anchors(marks: Iterable[Any]) -> dict[tuple[str, Decimal], set[str]]:
    out: dict[tuple[str, Decimal], set[str]] = defaultdict(set)
    for m in marks:
        out[(m.source_url, m.price)].add(m.instrument_id)
    return out
