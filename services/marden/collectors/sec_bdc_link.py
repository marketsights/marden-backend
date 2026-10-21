"""Link BDC schedule lines to Marden instruments, pair unfunded commitments with their lines, and
compute the raw and unfunded-adjusted marks.

Linking (docs/methods/price-sources.md §4G): a line may link only to the instruments of its own
legal borrower (sec_bdc_borrowers). Each candidate is compared on LoanX id, spread (or fixed
coupon), maturity and line class (loan / revolver / bond-note-ABS), using the instrument's current
facts. A candidate with any conflict is dropped; the line links when exactly one candidate has
positive evidence (or, with no evidence anywhere, when the borrower is an SPV whose one listed
facility is its only debt, `Borrower.sole`). Anything else stays unlinked with its reason. A borrower with no tranche
instruments links its loan lines to its borrower-level BDC-* placeholder (no prices are written
there: the placeholder mixes tranches).

Marks: raw = 100 x FV / par (tag F, kind fund_mark), refused when cost/par is outside 0.5-1.5 (par
then includes the unfunded part or a scale is off) or the mark is outside 1-150. When the unfunded
commitment U is known: P = 100 x (FV + U) / (par + U), tag M, method bdc-unfunded-adj@0.1, where FV
includes any fair value the filer carries on the unfunded commitment's own line.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from marden.collectors import sec_bdc_borrowers as bb
from marden.collectors.sec_bdc_parse import SoiLine
from marden.core.db import Conn
from marden.seed.instruments_nport import fund_mark_price

METHOD_VERSION = "bdc-unfunded-adj@0.1"
QUANTUM = Decimal("0.0001")
COST_RATIO = (Decimal("0.5"), Decimal("1.5"))
PRICE_RANGE = (Decimal(1), Decimal(150))
SPREAD_TOLERANCE_BP = Decimal(1)
COUPON_TOLERANCE_PCT = Decimal("0.015")
MATURITY_TOLERANCE_MONTHS = 12
_FLOATING = re.compile(r"sofr|libor|euribor|sonia|float|prime|\bs\s*\+", re.I)
_SECURITY_KINDS = frozenset({"bond", "convert", "abs", "cmbs", "muni"})
_LOAN_KINDS = frozenset({"term_loan", "ddtl", "construction_loan"})
_LEGAL = re.compile(r"\b(?:llc|l\.?l\.?c|inc|lp|l\.?p|ltd|limited|corp|corporation|company|co|pty|s\.?a\.? r\.?l)\b\.?")


def norm_name(name: str) -> str:
    return " ".join(_LEGAL.sub(" ", re.sub(r"[,()]", " ", name.lower())).split())


@dataclass(frozen=True)
class InstrumentInfo:
    id: str
    kind: str
    name: str
    issuer_name: str
    cls: str | None
    spread_bp: Decimal | None
    fixed_rate: Decimal | None  # percent
    maturity: date | None
    maturity_precision: str | None  # 'day' | 'month' | 'year'
    loanx: frozenset[str]


def _maturity(value: Any) -> tuple[date | None, str | None]:
    text = str(value or "").strip()
    shapes = ((r"^(\d{4})-(\d{2})-(\d{2})", "day"), (r"^(\d{4})-(\d{2})$", "month"), (r"^(\d{4})$", "year"))
    for pattern, precision in shapes:
        m = re.match(pattern, text)
        if m:
            parts = [int(g) for g in m.groups()] + [1] * (3 - len(m.groups()))
            try:
                return date(*parts), precision
            except ValueError:
                return None, None
    return None, None


def _cls(kind: str, name: str) -> str | None:
    if kind in _SECURITY_KINDS:
        return "security"
    if kind == "facility":
        return "revolver" if "revolv" in name.lower() else "loan"
    return "loan" if kind in _LOAN_KINDS else None


def load_instruments(conn: Conn, ids: set[str]) -> dict[str, InstrumentInfo]:
    """The candidate instruments that exist, with the current facts linking compares."""
    rows = conn.execute(
        "SELECT id, kind, name, issuer_name FROM instrument WHERE id = ANY(%s)", (sorted(ids),)
    ).fetchall()
    facts: dict[str, dict[str, Any]] = defaultdict(dict)
    for f in conn.execute(
        """SELECT subject_id, field, value FROM fact_current WHERE subject_type = 'instrument' AND subject_id = ANY(%s)
           AND field IN ('coupon', 'coupon_spread_bp', 'coupon_rate_pct', 'maturity', 'identifiers')""",
        (sorted(ids),),
    ).fetchall():
        facts[f["subject_id"]][f["field"]] = f["value"]
    out = {}
    for r in rows:
        f = facts[r["id"]]
        coupon = str(f.get("coupon") or "")
        spread = Decimal(str(f["coupon_spread_bp"])) if f.get("coupon_spread_bp") is not None else None
        floating = spread is not None or bool(_FLOATING.search(coupon))
        fixed = None
        if not floating and f.get("coupon_rate_pct") is not None and not re.search(r"~|effective|accret", coupon):
            fixed = Decimal(str(f["coupon_rate_pct"]))
        maturity, precision = _maturity(f.get("maturity"))
        loanx = frozenset(str(i.get("value", "")).upper() for i in f.get("identifiers") or []
                          if isinstance(i, dict) and i.get("type") == "loanx")
        out[r["id"]] = InstrumentInfo(r["id"], r["kind"], r["name"], r["issuer_name"], _cls(r["kind"], r["name"]),
                                      spread, fixed, maturity, precision, loanx)
    return out


@dataclass(frozen=True)
class LineTerms:
    """What a schedule line says about its terms (tagged values first, then its text)."""

    cls: str
    spread_bp: Decimal | None
    rate_pct: Decimal | None
    floating: bool
    maturity: date | None
    loanx: frozenset[str]


def tagged_spread_bp(line: SoiLine) -> Decimal | None:
    """The tagged spread in bp. A spread equal to the tagged rate is a fixed coupon tagged twice
    (Ares tags 9.50% fixed as both), so it is not a spread."""
    if line.spread is None or not 0 < line.spread <= Decimal("0.3") or line.spread == line.rate:
        return None
    return line.spread * 10000


def tagged_rate_pct(line: SoiLine) -> Decimal | None:
    return line.rate * 100 if line.rate is not None and 0 <= line.rate <= Decimal("0.5") else None


def line_terms(line: SoiLine) -> LineTerms:
    spread = tagged_spread_bp(line)
    if spread is None:
        spread = bb.spread_bp_from_text(line.identifier)
    floating = spread is not None or bool(_FLOATING.search(line.identifier))
    return LineTerms(bb.line_class(line.identifier), spread, tagged_rate_pct(line), floating,
                     line.maturity or bb.maturity_from_text(line.identifier), bb.loanx_ids(line.identifier))


def _months_apart(a: date, b: date) -> int:
    return abs((a.year - b.year) * 12 + a.month - b.month)


def evidence(t: LineTerms, inst: InstrumentInfo) -> tuple[list[str], list[str]]:
    """(positive evidence, conflicts) for linking a line with terms `t` to `inst`."""
    pos: list[str] = []
    con: list[str] = []
    if inst.cls != t.cls:
        con.append(f"class {t.cls} vs {inst.cls}")
    if t.loanx and inst.loanx:
        (pos if t.loanx & inst.loanx else con).append("loanx")
    if t.spread_bp is not None:
        if inst.spread_bp is not None:
            (pos if abs(t.spread_bp - inst.spread_bp) <= SPREAD_TOLERANCE_BP else con).append("spread")
        elif inst.fixed_rate is not None:
            con.append("floating vs fixed")
    elif t.rate_pct is not None and not t.floating:
        if inst.fixed_rate is not None:
            (pos if abs(t.rate_pct - inst.fixed_rate) <= COUPON_TOLERANCE_PCT else con).append("coupon")
        elif inst.spread_bp is not None:
            con.append("fixed vs floating")
    if t.maturity is not None and inst.maturity is not None:
        if inst.maturity_precision == "year":
            (pos if t.maturity.year == inst.maturity.year else con).append("maturity year")
        elif inst.maturity_precision == "day" and t.maturity == inst.maturity:
            pos.append("maturity date")
        elif (t.maturity.year, t.maturity.month) == (inst.maturity.year, inst.maturity.month):
            pos.append("maturity month")
        elif _months_apart(t.maturity, inst.maturity) > MATURITY_TOLERANCE_MONTHS:
            con.append("maturity")
    return pos, con


@dataclass(frozen=True)
class Link:
    instrument_id: str | None
    method: str  # 'loanx', evidence names joined by '+', 'name', 'placeholder', or '' when unlinked
    reason: str = ""
    priced: bool = False  # prices go to tranche links only


def link_line(line: SoiLine, instruments: dict[str, InstrumentInfo]) -> Link:
    borrower = bb.BY_KEY[line.borrower]
    terms = line_terms(line)
    cands = [instruments[i] for i in borrower.tranches if i in instruments]
    if not cands:
        if borrower.placeholder in instruments and terms.cls in ("loan", "revolver"):
            return Link(borrower.placeholder, "placeholder", "borrower-level instrument")
        return Link(None, "", "no instrument for this borrower" if not borrower.tranches else "tranches not in DB")
    scored = [(inst, *evidence(terms, inst)) for inst in cands]
    ok = [(inst, pos) for inst, pos, con in scored if not con]
    by_loanx = [inst for inst, pos in ok if "loanx" in pos]
    if len(by_loanx) == 1:
        return Link(by_loanx[0].id, "loanx", priced=True)
    with_evidence = [(inst, pos) for inst, pos in ok if pos]
    if len(with_evidence) > 1:  # several tranches agree: a delayed-draw or term label can still decide
        kind = {"ddtl": "ddtl", "term": "term_loan"}.get(bb.facility_type(line.identifier) or "")
        narrowed = [(inst, pos) for inst, pos in with_evidence if kind and inst.kind == kind]
        with_evidence = narrowed if len(narrowed) == 1 else with_evidence
    if len(with_evidence) == 1:
        inst, pos = with_evidence[0]
        return Link(inst.id, "+".join(pos), priced=True)
    if not with_evidence and borrower.sole and len(cands) == 1 and len(ok) == 1:
        return Link(ok[0][0].id, "name", priced=True)  # the borrower SPV's only facility, nothing conflicting
    conflicts = sorted({c for _, _, con in scored for c in con})
    return Link(None, "", f"{len(cands)} candidates, {len(ok)} without conflict, {len(with_evidence)} with evidence"
                + (f"; conflicts: {', '.join(conflicts)}" if conflicts else ""))


# ---------------------------------------------------------------- unfunded commitments
def is_position(line: SoiLine) -> bool:
    """A funded debt line: named, with par and a fair value, not equity. Par-only rows are commitment
    tables (a delayed-draw amount tagged as principal); issuer subtotals have no identifier."""
    return (bool(line.identifier) and line.par is not None and line.par > 0 and line.fv is not None
            and bb.line_class(line.identifier) != "equity")


def is_unfunded_row(line: SoiLine) -> bool:
    return line.unfunded is not None and line.unfunded > 0 and not (line.par is not None and line.par > 0)


@dataclass(frozen=True)
class Unfunded:
    amount: Decimal
    fv_on_commitment: Decimal  # fair value the filer carries on the commitment's own line (often 0 or negative)
    source: str  # 'same line' or 'commitment line'


def assign_unfunded(lines: list[SoiLine]) -> tuple[dict[tuple[str, str], Unfunded], int]:
    """(adsh, line_key) of a position -> its unfunded commitment; plus how many commitment lines
    could not be paired with exactly one position of the same borrower in the same filing."""
    out: dict[tuple[str, str], Unfunded] = {}
    by_filing: dict[str, list[SoiLine]] = defaultdict(list)
    for line in lines:
        by_filing[line.adsh].append(line)
        if is_position(line) and line.unfunded is not None and line.unfunded > 0:
            out[(line.adsh, line.line_key)] = Unfunded(line.unfunded, Decimal(0), "same line")
    unpaired = 0
    for filing in by_filing.values():
        claims: dict[tuple[str, str], list[SoiLine]] = defaultdict(list)
        for row in (r for r in filing if is_unfunded_row(r)):
            cands = [p for p in filing if is_position(p) and p.borrower == row.borrower]
            kind = bb.facility_type(row.text)
            same_kind = [p for p in cands if kind and bb.facility_type(p.identifier) == kind]
            cands = same_kind or cands
            if len(cands) != 1:
                unpaired += 1
                continue
            claims[(cands[0].adsh, cands[0].line_key)].append(row)
        for key, rows in claims.items():
            if len(rows) != 1 or key in out:
                unpaired += len(rows)
                continue
            out[key] = Unfunded(rows[0].unfunded or Decimal(0), rows[0].fv or Decimal(0), "commitment line")
    return out, unpaired


# ---------------------------------------------------------------- marks
def raw_mark(line: SoiLine) -> tuple[Decimal | None, str | None]:
    """100 x FV / par, or (None, why not)."""
    if line.par is None or line.par <= 0:
        return None, "no par"
    if line.fv is None:
        return None, "no fair value"
    if line.cost is not None and not COST_RATIO[0] <= line.cost / line.par <= COST_RATIO[1]:
        return None, "cost/par outside 0.5-1.5"
    price = fund_mark_price(line.fv, line.par)  # the seed loaders' rounding, so the same line dedupes
    if price is None or not PRICE_RANGE[0] <= price <= PRICE_RANGE[1]:
        return None, "mark outside 1-150"
    return price, None


def adjusted_mark(line: SoiLine, unfunded: Unfunded) -> Decimal | None:
    """P = 100 x (FV + U) / (par + U), for a line whose raw mark passed."""
    if line.par is None or line.fv is None or unfunded.amount <= 0:
        return None
    total_fv = line.fv + unfunded.fv_on_commitment
    return (Decimal(100) * (total_fv + unfunded.amount) / (line.par + unfunded.amount)).quantize(
        QUANTUM, rounding=ROUND_HALF_EVEN)
