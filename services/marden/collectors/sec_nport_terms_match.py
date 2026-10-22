"""Evidence rules for linking a fund's N-PORT position to an instrument that has no identifier on file
(no database, no network; `sec_nport_terms` runs them).

The `sec_nport` collector links positions by CUSIP / ISIN / LoanX only. Construction loans, private
placements, DDTLs and many ABS tranches carry no identifier Marden knows (PGIM files its loans under its own
codes such as 'B8AFB9GT6' with CUSIP 000000000), so their holders were never found even when the
instrument's own facts were read from a fund's N-PORT. Here a position links to an instrument only when
the filed terms agree with the instrument's facts on every count:

1. issuer: at least two of the instrument's issuer words in the position's name or title (one word when
   the instrument has one, or when the instrument's own facts cite this very filing or carry its filer code);
2. maturity: the same day when both give a day, else the same month (a year alone is not enough);
3. coupon: fixed: the filed annualized rate within 0.0051 of the instrument's rate (filers round 7.375 to
   7.38); floating: a floating or variable position, and when the instrument states a current rate read from
   this filing (its facts cite it), that rate; zero: a zero rate. No coupon type, no link;
4. nothing that tells siblings apart disagrees: entity numbers ('Issuer II' vs 'III', `holdings_link.qualifiers`),
   an ABS deal series ('2024-1') and class ('A2'), US state codes in the name ('TX' vs 'WI');
5. one instrument only: a position two instruments both pass stays unlinked (a parent package and its own
   tranche count as one: the tranche wins), and a position whose CUSIP / ISIN is another instrument's
   is left to the identifier route.

An unfunded commitment line ('VANTAGE ORACLE WI - UNFUNDED': the filer's rate field holds a placeholder)
follows the funded line of the same facility in the same filing: same name without the 'UNFUNDED' suffix,
same maturity. Each link carries its basis (the agreeing terms) for the holding's `link_basis` fact.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from marden.collectors.sec_nport_parse import id_keys
from marden.seed.holdings_link import name_tokens, qualifiers, qualifiers_agree
from marden.seed.instruments_parse import cusip_valid, isin_valid, normalize_name, parse_coupon

METHOD = "nport-terms-link@0.1"
RATE_TOLERANCE = 0.0051
PLACEHOLDER_RATE = 0.05  # a floating rate at or below this is a filer placeholder (unfunded lines file 0.01)
_SERIES = re.compile(r"\b(20\d\d)[- ](\d{1,2}(?:[a-z]{1,3})?|[a-z]{2,5}\d{0,2})\b", re.I)
_CLASS = re.compile(r"^(?:[a-d]\d{0,2}[a-z]?|r)$", re.I)
_UNFUNDED = re.compile(r"\s*[-,]?\s*\bunfunded\b.*$", re.I)
_CURRENT_RATE = re.compile(r"(\d{1,2}\.\d{1,4})\s*%")
# Words that name a kind of issuer, not which one: never the only issuer word two names share.
GENERIC = frozenset({
    "mortgage", "trust", "commercial", "abs", "asset", "assets", "capital", "funding", "securities", "power",
    "electric", "public", "service", "project", "infrastructure", "digital", "compute", "borrower", "spv",
    "international", "europe", "texas", "dev", "investor", "investors", "acquisition", "realty", "operating",
    "partners", "global", "solutions", "industries", "technology", "generation", "supply", "properties",
    "property", "fund", "trs", "pass", "through", "certificates", "cmbs", "sasb", "receivables", "lease",
})
# US state codes that also read as words in all-caps titles are left out.
_STATES = frozenset({
    "AK", "AZ", "AR", "CA", "CT", "FL", "GA", "IA", "IL", "KS", "KY", "MI", "MN", "MS", "MT", "NC", "ND", "NE", "NH",
    "NJ", "NM", "NV", "NY", "RI", "SD", "TN", "TX", "UT", "VA", "VT", "WA", "WI", "WV", "WY",
})


# ---------------------------------------------------------------- instrument side
@dataclass(frozen=True)
class Profile:
    """What an instrument's facts say that a filed position can be checked against."""

    id: str
    kind: str
    tokens: frozenset[str]  # issuer words (issuer name, issuer entity)
    quals: frozenset[str]
    states: frozenset[str]
    series: str | None
    tranche: str | None
    maturity: str | None  # 'YYYY-MM-DD' or 'YYYY-MM'
    coupon_type: str | None  # fixed | floating | zero
    rate: float | None  # a fixed coupon, percent
    current_rate: float | None  # a floating coupon's current rate as the instrument's facts state it
    keys: frozenset[str] = frozenset()  # identifiers on file
    cited: frozenset[str] = frozenset()  # accessions the instrument's facts cite (N-PORT URLs)
    filer_codes: frozenset[str] = frozenset()  # filer codes of lines already linked to it
    tranche_keys: frozenset[str] = frozenset()  # CUSIPs of lines that state this tranche's class
    parent: str | None = None  # the package this instrument is a tranche of


def accession_of(url: str | None) -> str | None:
    """'.../data/929523/000094040026038879/primary_doc.xml' -> '0000940400-26-038879'."""
    m = re.search(r"/(\d{10})(\d{2})(\d{6})/", str(url or ""))
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def deal_series(text: str | None) -> tuple[str | None, str | None]:
    """ABS / CMBS deal series and class from a name: 'II 2024-1 A1' -> ('2024-1', 'A1'), 'COLO 2021-2A A2' ->
    ('2021-2', 'A2'), 'CLGIX 2022-1CAN B (CAD)' -> ('2022-1', 'B'), 'BX 2025-VLT7 C' -> ('2025-VLT7', 'C'),
    'CONE 2026-DFW3 A' -> ('2026-DFW3', 'A')."""
    s = str(text or "")
    m = _SERIES.search(s)
    if m is None:
        return None, None
    tail = m.group(2).upper()
    digits = re.match(r"\d+", tail)
    series = f"{m.group(1)}-{int(digits.group(0))}" if digits else f"{m.group(1)}-{tail}"
    rest = re.sub(r"[^A-Za-z0-9 ]+", " ", s[m.end():]).split()
    tranche = next((w.upper() for w in rest[:2] if _CLASS.match(w)), None)
    return series, tranche


def state_codes(*texts: Any) -> frozenset[str]:
    """US state codes written as their own capitalised word ('VANTAGE ORACLE TX', 'Construction debt (TX + WI)')."""
    words = re.findall(r"\b[A-Z]{2}\b", " ".join(str(t or "") for t in texts))
    return frozenset(w for w in words if w in _STATES)


def current_rate(coupon_text: Any) -> float | None:
    """The current rate a floating coupon's text states: 'Floating, 6.17%', '~SOFR+2.50% (6.17% at 31 Jul per
    PGIM N-PORT)', 'Floating (6.37% current)'. A spread ('SOFR+2.50%') is not a rate."""
    s = str(coupon_text or "")
    s = re.sub(r"(?:sofr|libor|euribor|sonia|s|t)\s*\+\s*\d+(?:\.\d+)?\s*%?", " ", s, flags=re.I)
    rates = [float(x) for x in _CURRENT_RATE.findall(s)]
    return rates[0] if len(rates) == 1 else None


def profile(
    row: Mapping[str, Any], facts: Mapping[str, Any], *, cited: Iterable[str] = (), filer_codes: Iterable[str] = (),
    parent: str | None = None,
) -> Profile:
    """A Profile from an instrument row (id, kind, name, issuer_name, entity_name) and its current fact values
    (coupon, coupon_type, coupon_rate_pct, maturity, identifiers)."""
    coupon = facts.get("coupon")
    parsed = parse_coupon(str(coupon)) if isinstance(coupon, str) else {}
    ctype = facts.get("coupon_type") or parsed.get("coupon_type")
    rate = facts.get("coupon_rate_pct", parsed.get("coupon_rate_pct"))
    keys: set[str] = set()
    for item in facts.get("identifiers") or []:
        if isinstance(item, dict) and item.get("type") in ("cusip", "isin", "loanx"):
            keys |= id_keys(str(item.get("value") or ""))
    keys |= id_keys(row.get("cusip"), row.get("isin"))
    maturity = str(facts.get("maturity") or "").strip()
    series, tranche = deal_series(row.get("name"))
    return Profile(
        id=str(row["id"]), kind=str(row["kind"]),
        tokens=name_tokens(row.get("issuer_name"), row.get("entity_name")),
        quals=qualifiers(row.get("issuer_name"), row.get("name")),
        states=state_codes(row.get("name"), row.get("issuer_name")),
        series=series, tranche=tranche,
        maturity=maturity if re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", maturity) else None,
        coupon_type=str(ctype) if ctype in ("fixed", "floating", "zero") else None,
        rate=float(rate) if isinstance(rate, int | float) else None,
        current_rate=current_rate(coupon) if ctype == "floating" else None,
        keys=frozenset(keys), cited=frozenset(cited), filer_codes=frozenset(filer_codes), parent=parent,
    )


# ---------------------------------------------------------------- position side
@dataclass(frozen=True)
class Pos:
    """One filed N-PORT position (from the DERA data set or a primary_doc.xml)."""

    accession: str
    name: str
    title: str
    cusip: str | None
    isin: str | None
    others: tuple[str, ...]
    maturity: date | None
    coupon_kind: str | None  # Fixed | Floating | Variable | None as filed
    rate: float | None  # annualizedRt, percent
    asset_cat: str | None = None
    balance: str | None = None
    value_usd: str | None = None
    key: str = ""  # the caller's handle (DERA holding id, or filing ordinal)

    @property
    def text(self) -> str:
        return f"{self.name} {self.title}"

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(id_keys(self.cusip, self.isin, *self.others))

    @property
    def cusip_valid(self) -> str | None:
        return self.cusip if self.cusip and cusip_valid(self.cusip) else None

    @property
    def isin_valid(self) -> str | None:
        return self.isin if self.isin and isin_valid(self.isin) else None

    @property
    def unfunded(self) -> bool:
        return bool(re.search(r"\bunfunded\b", self.text, re.I))

    @property
    def stem(self) -> str:
        """The facility's name without an unfunded-commitment suffix ('VANTAGE ORACLE WI - UNFUNDED' -> 'vantage
        oracle wi')."""
        return normalize_name(_UNFUNDED.sub("", self.title or self.name))


def dera_date(text: str | None) -> date | None:
    """'15-APR-2030' (DERA) or '2030-04-15' (primary_doc.xml)."""
    s = (text or "").strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _float(text: Any) -> float | None:
    try:
        return None if text in (None, "") else float(text)
    except (TypeError, ValueError):
        return None


def pos_from_dera(holding_id: str, row: Mapping[str, Any]) -> Pos:
    ids = row.get("IDS") or ()
    isins = [i[0] for i in ids if i[0]]
    return Pos(
        accession=row["ACCESSION_NUMBER"], name=row.get("ISSUER_NAME") or "", title=row.get("ISSUER_TITLE") or "",
        cusip=(row.get("ISSUER_CUSIP") or None), isin=isins[0] if isins else None,
        others=tuple(i[1] for i in ids if i[1]), maturity=dera_date(row.get("MAT")),
        coupon_kind=(row.get("CPN") or None), rate=_float(row.get("RATE")), asset_cat=row.get("ASSET_CAT"),
        balance=row.get("BALANCE"), value_usd=row.get("CURRENCY_VALUE"), key=holding_id,
    )


# ---------------------------------------------------------------- the rules
@dataclass
class Evidence:
    instrument_id: str
    basis: list[str] = field(default_factory=list)

    def text(self) -> str:
        return "; ".join(self.basis)


def _maturity(p: Profile, pos: Pos) -> str | None:
    if p.maturity is None or pos.maturity is None:
        return None
    if len(p.maturity) == 10:
        return f"maturity {pos.maturity.isoformat()}" if pos.maturity.isoformat() == p.maturity else None
    return f"maturity month {p.maturity}" if pos.maturity.isoformat()[:7] == p.maturity else None


def _coupon(p: Profile, pos: Pos, cited: bool) -> str | None:
    kind = (pos.coupon_kind or "").lower()
    if p.coupon_type == "fixed" and p.rate is not None:
        if kind in ("fixed", "") and pos.rate is not None and abs(pos.rate - p.rate) <= RATE_TOLERANCE:
            return f"fixed {pos.rate:g}% (instrument {p.rate:g}%)"
        return None
    if p.coupon_type == "zero":
        return "zero coupon" if pos.rate == 0 and kind != "floating" else None
    if p.coupon_type == "floating" and kind in ("floating", "variable"):
        if cited and p.current_rate is not None and pos.rate is not None and pos.rate > PLACEHOLDER_RATE:
            if abs(pos.rate - p.current_rate) > RATE_TOLERANCE:
                return None
            return f"floating, {pos.rate:g}% as the instrument's facts state"
        return "floating"
    return None


def match(p: Profile, pos: Pos) -> Evidence | None:
    """Evidence that `pos` is instrument `p` (None when any rule fails; module docstring)."""
    if p.kind == "other":
        return None  # placeholder rows never link
    if p.keys and (pos.keys & p.keys or pos.cusip_valid or pos.isin_valid):
        return None  # an instrument with identifiers on file is linked by them, not by its terms
    cited = pos.accession in p.cited
    code = bool(p.filer_codes & set(pos.others))
    words = p.tokens & name_tokens(pos.name, pos.title)
    need = 1 if (cited or code) else min(2, len(p.tokens))
    if need == 0 or len(words) < need or not words - GENERIC:
        return None
    if not qualifiers_agree(p.quals, qualifiers(pos.name, pos.title)):
        return None
    pos_states = state_codes(pos.name, pos.title)
    if p.states and pos_states and not p.states & pos_states:
        return None
    series, tranche = deal_series(pos.title)
    if series is None:
        series, tranche = deal_series(pos.name)
    if p.series and series and series != p.series:
        return None
    if p.tranche and tranche and tranche != p.tranche:
        return None
    if p.tranche and p.coupon_type == "floating" and tranche is None and not (pos.keys & p.tranche_keys):
        return None  # floating classes of one deal share name, maturity and coupon type: the class must show
    mat = _maturity(p, pos)
    if mat is None:
        return None
    cpn = _coupon(p, pos, cited)
    if cpn is None:
        return None
    basis = [f"issuer words {', '.join(sorted(words))}", mat, cpn]
    if p.series and series:
        basis.append(f"series {series}" + (f" class {tranche}" if tranche else ""))
    elif pos.keys & p.tranche_keys:
        basis.append("CUSIP of lines that state the class")
    if cited:
        basis.append("the instrument's facts cite this filing")
    if code:
        basis.append("filer code of a line already linked")
    return Evidence(p.id, basis)


def decide(profiles: Sequence[Profile], pos: Pos, owned_keys: Mapping[str, str]) -> tuple[Evidence | None, list[str]]:
    """The one instrument `pos` is (None and the candidates when none or several pass). A CUSIP / ISIN that is
    another instrument's identifier settles it for that instrument (left to the identifier route)."""
    if any(k in owned_keys for k in pos.keys):
        return None, sorted({owned_keys[k] for k in pos.keys if k in owned_keys})
    found = [e for e in (match(p, pos) for p in profiles) if e is not None]
    if len(found) > 1:
        by_id = {p.id: p for p in profiles}
        tranches = {e.instrument_id for e in found if by_id[e.instrument_id].parent}
        parents = {by_id[i].parent for i in tranches}
        found = [e for e in found if e.instrument_id not in parents]
    if len(found) == 1:
        return found[0], [found[0].instrument_id]
    return None, sorted(e.instrument_id for e in found)


def follow_unfunded(linked: Mapping[str, tuple[Pos, Evidence]], others: Iterable[Pos]) -> dict[str, Evidence]:
    """Unfunded commitment lines of a filing whose funded line (same stem, same maturity) linked: pos.key ->
    Evidence. `linked` maps pos.key -> (pos, evidence) for the same filing."""
    funded: dict[tuple[str, date | None], set[str]] = {}
    for pos, ev in linked.values():
        if not pos.unfunded:
            funded.setdefault((pos.stem, pos.maturity), set()).add(ev.instrument_id)
    out: dict[str, Evidence] = {}
    for pos in others:
        if not pos.unfunded or pos.key in linked:
            continue
        ids = funded.get((pos.stem, pos.maturity), set())
        if len(ids) == 1:
            iid = next(iter(ids))
            out[pos.key] = Evidence(iid, [f"unfunded commitment of the linked line '{pos.stem}'",
                                          f"maturity {pos.maturity.isoformat() if pos.maturity else 'n/d'}"])
    return out
