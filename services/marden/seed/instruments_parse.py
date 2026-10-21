"""Pure parsers for the instruments loader: kinds, coupons, maturities, identifiers, names.

No database or file access here, so every rule can be unit-tested on real archive rows.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

# ---------------------------------------------------------------- identifiers

_CUSIP_RE = re.compile(r"^[0-9A-Z*@#]{9}$")
_ISIN_RE = re.compile(r"^[A-Z]{2}[0-9A-Z]{9}[0-9]$")
CUSIP_IN_TEXT = re.compile(r"\b[0-9]{3}[0-9A-Z]{5}[0-9]\b")
LOANX_IN_TEXT = re.compile(r"\bLX\d{6}\b")
PLACEHOLDER_CUSIPS = frozenset({"000000000", "999999999", "N/A", ""})


def _char_value(ch: str) -> int:
    if ch.isdigit():
        return int(ch)
    if "A" <= ch <= "Z":
        return ord(ch) - 55
    return {"*": 36, "@": 37, "#": 38}[ch]


def cusip_valid(value: str | None) -> bool:
    """A 9-character CUSIP whose check digit is right (placeholders such as 000000000 fail)."""
    if not value or value in PLACEHOLDER_CUSIPS or not _CUSIP_RE.match(value) or not value[8].isdigit():
        return False
    total = 0
    for i, ch in enumerate(value[:8]):
        v = _char_value(ch) * (2 if i % 2 else 1)
        total += v // 10 + v % 10
    return (10 - total % 10) % 10 == int(value[8])


def isin_valid(value: str | None) -> bool:
    """ISO 6166 check (letters expanded to two digits, Luhn over the digit string)."""
    if not value or not _ISIN_RE.match(value):
        return False
    digits = "".join(str(_char_value(ch)) for ch in value[:11])
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d) * (2 if i % 2 == 0 else 1)
        total += n // 10 + n % 10
    return (10 - total % 10) % 10 == int(value[11])


def nsin_like(value: str | None) -> bool:
    """A 9-digit code that is not a valid CUSIP: the national number inside an XS ISIN (common code)."""
    return bool(value) and bool(re.fullmatch(r"\d{9}", value or "")) and not cusip_valid(value)


def text_identifiers(*texts: Any) -> tuple[list[str], list[str]]:
    """CUSIPs (check digit verified) and LoanX codes named in free text."""
    cusips: list[str] = []
    loanx: list[str] = []
    for t in texts:
        s = str(t or "")
        for m in CUSIP_IN_TEXT.findall(s):
            if cusip_valid(m) and m not in cusips:
                cusips.append(m)
        for m in LOANX_IN_TEXT.findall(s):
            if m not in loanx:
                loanx.append(m)
    return cusips, loanx


# ---------------------------------------------------------------- instrument kind

KINDS = ("construction_loan", "term_loan", "ddtl", "bond", "convert", "abs", "cmbs", "lease", "facility", "muni",
         "other")
_CMBS = re.compile(r"CMBS|Commercial Mortgage|\bBX 20\d\d|\bBX-|\bSWCH 20|\bCONE 20|\bVDCM\b|Mortgage Trust", re.I)
_CONVERT = re.compile(r"convertible|exchangeable|\bconv\.?\b", re.I)
_DDTL = re.compile(r"\bDDTL\b|delayed[- ]draw|\bDD\b", re.I)
_CONSTRUCTION = re.compile(r"construction|quick-draw", re.I)
_LEASE = re.compile(r"\blease", re.I)
_MUNI = re.compile(r"revenue bonds|\bJDA\b|municipal|\bIDB\b|\bPILOT\b", re.I)
_FACILITY = re.compile(r"revolv|\bRCF\b|credit facility|L/C facility|warehouse|\bfacilit(y|ies)\b|\bMFSA\b", re.I)
_LOAN = re.compile(r"term loan|\bTLB?\b|\bloans?\b|unitranche|equipment financing|\bfinancing\b", re.I)
_BOND = re.compile(r"\bnotes?\b|\bbonds?\b|debentures", re.I)
_BOND_WEAK = re.compile(r"\bSr\b|senior|\bseries\b|\bclass\b", re.I)


def instrument_kind(row: dict[str, Any], *, convertible_flag: bool = False, group_name: str = "") -> str:
    """Map a legacy aidebt row (cat, rank, inst text) to instrument.kind.

    ``group_name`` is the archive's display name for the row's group (aidebt.gn); a loan in a group the
    archive names as construction loans ('Vantage-Oracle construction loans') is a construction loan.
    """
    kind = _kind(row, convertible_flag)
    if kind == "term_loan" and "construction" in group_name.lower():
        return "construction_loan"
    return kind


def _kind(row: dict[str, Any], convertible_flag: bool) -> str:
    inst = row.get("inst") or ""
    rank = row.get("rank") or ""
    cat = row.get("cat") or ""
    if rank.startswith("Preferred") or "preferred" in inst.lower():
        return "other"
    if rank.startswith("Guarantee") or re.search(r"guarant|deferred (purchase|revenue)|commitments", inst, re.I):
        return "other"
    if rank == "Convertible" or convertible_flag or _CONVERT.search(inst):
        return "convert"
    if cat == "abs" or rank.startswith("Secured (ABS"):
        return "cmbs" if _CMBS.search(f"{inst} {row.get('issuer') or ''}") else "abs"
    if _DDTL.search(inst):
        return "ddtl"
    if _CONSTRUCTION.search(inst):
        return "construction_loan"
    if _LEASE.search(inst):
        return "lease"
    if _MUNI.search(inst):
        return "muni"
    if inst.startswith("BDC / private-credit"):
        return "other"  # borrower-level placeholder rows, no tranche
    if re.match(r"term loan", inst, re.I):
        return "term_loan"
    if _BOND.search(inst):
        return "bond"
    if _FACILITY.search(inst):
        return "facility"
    if _LOAN.search(inst):
        return "term_loan"
    if _BOND_WEAK.search(inst) or cat == "util":
        return "bond"
    return "other"


# ---------------------------------------------------------------- coupon

_BENCH = {"SOFR": "SOFR", "S": "SOFR", "TERM SOFR": "SOFR", "LIBOR": "LIBOR", "L": "LIBOR", "EURIBOR": "EURIBOR",
          "E": "EURIBOR", "SONIA": "SONIA", "BBSY": "BBSY", "BSBY": "BSBY"}
_FLOAT = re.compile(r"(TERM SOFR|SOFR|EURIBOR|SONIA|LIBOR|BBSY|BSBY|\bS|\bL|\bE)\s*\+\s*(\d+(?:\.\d+)?)\s*(%|bps?)?",
                    re.I)
_FIXED = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%")


def parse_coupon(text: str | None) -> dict[str, Any]:
    """Structured coupon terms from the archive's free text (only what the text states).

    Returns a subset of {coupon_type, coupon_rate_pct, coupon_benchmark, coupon_spread_bp}.
    """
    s = (text or "").strip()
    if not s:
        return {}
    out: dict[str, Any] = {}
    approximate = s.startswith("~")  # '~SOFR+2.50%': the type is known, the number is not exact
    m = _FLOAT.search(s)
    if m:
        out["coupon_type"] = "floating"
        out["coupon_benchmark"] = _BENCH[m[1].upper()]
        if approximate:
            return out
        value = float(m[2])
        unit = (m[3] or "").lower()
        if unit == "%" or (not unit and "." in m[2] and value < 20):
            out["coupon_spread_bp"] = round(value * 100, 2)
        else:
            out["coupon_spread_bp"] = value
        return out
    if re.search(r"\bfloat(ing)?\b|\bFRN\b", s, re.I):
        return {"coupon_type": "floating"}
    m = _FIXED.match(s)
    if m and not re.match(r"^\s*\d+(?:\.\d+)?\s*%?\s*[-/]", s):
        rate = float(m[1])
        out["coupon_type"] = "zero" if rate == 0 else "fixed"
        out["coupon_rate_pct"] = rate
    return out


# ---------------------------------------------------------------- maturity

_DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR = re.compile(r"^(\d{4})$")


def parse_maturity(text: str | None) -> tuple[str | None, str | None]:
    """(ISO maturity at the precision given: YYYY-MM-DD / YYYY-MM / YYYY, or None; leftover text or None).

    A month or year is kept as a partial ISO date rather than padded to a made-up day.
    """
    s = (text or "").strip()
    if not s:
        return None, None
    m = _DAY.match(s)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat(), None
        except ValueError:
            return None, s
    m = _MONTH.match(s)
    if m and 1 <= int(m[2]) <= 12:
        return s, None
    if _YEAR.match(s):
        return s, None
    return None, s


def maturity_month(text: str | None) -> str | None:
    iso, _ = parse_maturity(text)
    return iso[:7] if iso and len(iso) >= 7 else None


# ---------------------------------------------------------------- names and entities

_SUFFIX = re.compile(r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|n\s?v|lp|l\s?p|llc|l\s?l\s?c|"
                     r"group|s\s?a|ag|ab|as|pty)\b\.?$")
UNNAMED = re.compile(r"\bn/d\b|unnamed|not named|not disclosed|undisclosed", re.I)


def normalize_name(name: str | None) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    return " ".join(s.split())


def core_name(name: str | None) -> str:
    """Lowercased name without punctuation or trailing legal-form words ('Oracle Corporation' -> 'oracle')."""
    s = normalize_name(name)
    while True:
        t = _SUFFIX.sub("", s).strip()
        if t == s:
            return s
        s = t


def entity_kind(cat: str | None) -> str:
    return {"util": "utility", "abs": "spv", "proj": "spv"}.get(cat or "", "company")


def is_unnamed(issuer: str | None) -> bool:
    return not (issuer or "").strip() or bool(UNNAMED.search(issuer or ""))


# ---------------------------------------------------------------- amounts

# Dictionary section 5 ("Units"): rows that hold a holder's par, not the issue size.
HOLDER_PAR_IDS = frozenset({"NP-BE-093712AK3", "EX-CRUSOE-079", "EX-VANTAGE_ORCL-367", "EX-VANTAGE_ORCL-368"})


def amount_basis(instrument_id: str, row: dict[str, Any], kind: str) -> str | None:
    """'drawn' for delayed-draw rows (the facility registry holds the commitment), 'holder_par' where the
    row holds one holder's par; None where the archive gives no reason to doubt it is the issue size."""
    if row.get("amt_m") is None:
        return None
    if instrument_id in HOLDER_PAR_IDS or re.search(r"-held portion|held portion", row.get("inst") or "", re.I):
        return "holder_par"
    if kind == "ddtl":
        return "drawn"
    return None
