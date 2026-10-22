"""Does the document a seeded value cites actually state it? (tag F needs a filed document that states it)

    docs = CitedDocs(conn)
    text = scope(docs.text(url), ids=["21881PAB0"], names=["coreweave"])   # N-PORT: this instrument's lines only
    states(text, "amount_usd", 18_000_000_000, anchors=anchor_patterns(terms, "amount_usd"))  # True/False/None

The archive carries no per-field source: an aidebt row lists every URL its research used (``src``) and the
loaders tagged each field with the first one's host. A fund's N-CSRS listing its own $1.2mm slice of a loan
does not state the loan's $18B size, so a host rule alone cannot give F. ``states`` looks for the value in the
stored text of the cited document, in the forms filings print:

- USD: "$18.0 billion", "$18bn", "$2.5875 billion" (for an archive figure rounded to $1M: 2,588), "775.0
  million", "$1,750,000,000", "18,000,000" in a thousands table and plain digits in N-PORT XML; a bare table
  number ("750" under "in millions", "275,000" under "in thousands") counts only within WINDOW chars of the
  instrument's name, or of two of its other terms (``anchors``: coupon, maturity, size), as in a debt table
  row "$750, 4.50%, due May 2028". An amount in another currency ("CHF 275,000") does not state a USD value;
- percents: "6.875%", "6.87500%", "6.875 percent", N-PORT ``annualizedRt`` 6.87500000; spreads also as
  "250 basis points" / "250 bps";
- dates: "November 9, 2029", "Nov. 9, 2029", "9 November 2029", "11/09/2029", "11/9/29", "2029-11-09"; a
  month ("2032-03") is stated by "March 2032" or any day in it; a year by "due ... 2032" / "2032 Notes";
- years of term: "15 years", "15-year", "fifteen (15) years", "fifteen-year";
- free text: each number it carries (percents, dollar amounts, dates, "120 days") must be stated; text with
  none is stated only verbatim.

A fund holdings report (N-PORT XML) lists hundreds of securities, so ``scope`` keeps only this instrument's
holdings (CUSIP / ISIN, else issuer name) before the check: another holding's 5.25% is not this one's coupon.

``states`` returns None when there is nothing to check (no stored text, a zero amount, a text value with no
number that is not quoted verbatim); callers treat None like False: no F without a document that states it.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from marden.core import rawstore
from marden.core.db import Conn

TextLookup = Callable[[str], "str | None"]

USD_FIELDS = frozenset({
    "amount_usd", "commitment_usd", "drawn_usd", "total_usd", "annual_usd", "lease_tcv_usd",
    "lease_annual_rent_usd", "cost_to_complete_usd", "total_project_cost_usd",
})
PCT_FIELDS = frozenset({"coupon_rate_pct"})
BP_FIELDS = frozenset({"coupon_spread_bp"})
BENCHMARK_FIELDS = frozenset({"coupon_benchmark"})
DATE_FIELDS = frozenset({"maturity"})
YEARS_FIELDS = frozenset({"term_years", "lease_term_years"})
TEXT_FIELDS = frozenset({"coupon", "maturity_text", "rate_text", "amortization", "start", "outside_date"})
CHECKED_FIELDS = USD_FIELDS | PCT_FIELDS | BP_FIELDS | BENCHMARK_FIELDS | DATE_FIELDS | YEARS_FIELDS | TEXT_FIELDS

WINDOW = 150  # chars: a bare table amount counts as this instrument's when another of its terms is this close
FAMILIES = (
    frozenset({"coupon", "coupon_type", "coupon_rate_pct", "coupon_spread_bp", "coupon_benchmark", "rate_text"}),
    frozenset({"maturity", "maturity_text"}),
)

# Pages that are not a document: a search form, an EDGAR company browse page, a filing's directory index.
_NOT_A_DOCUMENT = re.compile(r"/edgar/search/|cgi-bin/browse-edgar|/$")

MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")
_MON_ABBR = {1: ("jan",), 2: ("feb",), 3: ("mar",), 4: ("apr",), 5: (), 6: ("jun",), 7: ("jul",), 8: ("aug",),
             9: ("sep", "sept"), 10: ("oct",), 11: ("nov",), 12: ("dec",)}
NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
                "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen")
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty"}
BENCHMARK_WORDS = {
    "SOFR": (r"\bsofr\b", r"secured overnight financing rate"),
    "LIBOR": (r"\blibor\b", r"london interbank offered rate"),
    "EURIBOR": (r"\beuribor\b", r"euro interbank offered rate"),
    "SONIA": (r"\bsonia\b", r"sterling overnight index average"),
    "BBSY": (r"\bbbsy\b",),
    "BSBY": (r"\bbsby\b",),
}
_NAME_STOP = frozenset({"the", "and", "due", "notes", "note", "loan", "loans", "facility", "llc", "inc", "corp",
                        "senior", "xbrl", "member"})


# ---------------------------------------------------------------- document text


def is_document_url(url: str | None) -> bool:
    return bool(url) and not _NOT_A_DOCUMENT.search(str(url))


def normalize_text(text: str) -> str:
    """Lower-case, whitespace-folded text (marden.agents.text.normalize): the form ``states`` searches."""
    from marden.agents.text import normalize

    return normalize(text).lower()


class CitedDocs:
    """Normalized text of the stored document for a URL (the newest row with bytes), cached per URL.

    Record-only rows (``meta.record_only``: the archive named the document but had no bytes) have no text.
    """

    def __init__(self, conn: Conn, *, cache_size: int = 48):
        self.conn = conn
        self.cache_size = cache_size
        self._cache: OrderedDict[str, str | None] = OrderedDict()
        self.document_ids: dict[str, int] = {}

    def text(self, url: str | None) -> str | None:
        if not url or not is_document_url(url):
            return None
        if url in self._cache:
            self._cache.move_to_end(url)
            return self._cache[url]
        value = self._read(url)
        self._cache[url] = value
        if len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        return value

    __call__ = text

    def _read(self, url: str) -> str | None:
        from marden.agents.text import UnreadableDocument, read_text

        row = self.conn.execute(
            """SELECT id, storage_key, content_type FROM document
                WHERE url = %s AND coalesce(meta->>'record_only', 'false') <> 'true' AND coalesce(bytes, 1) > 0
                ORDER BY fetched_at DESC, id DESC LIMIT 1""",
            (url,),
        ).fetchone()
        if row is None:
            return None
        try:
            doc = read_text(rawstore.get(row["storage_key"]), row["content_type"], url)
        except (UnreadableDocument, rawstore.RawStoreError, OSError):
            return None
        self.document_ids[url] = row["id"]
        return normalize_text(doc.text)


def scope(text: str | None, *, ids: Sequence[str] = (), names: Sequence[str] = ()) -> str | None:
    """A fund holdings report (N-PORT XML) cut to this instrument's holdings: by CUSIP / ISIN, else by an
    issuer word in the holding's name or title. Any other document is returned whole; a report with no
    matching holding gives '' (it does not state the value)."""
    if not text or "<invstorsec>" not in text:
        return text
    blocks = re.findall(r"<invstorsec>.*?</invstorsec>", text)
    keys = [i.lower() for i in ids if i]
    picked = [b for b in blocks if any(k in b for k in keys)] if keys else []
    if not picked:
        words = [n.lower().strip() for n in names if n and len(n.strip()) >= 3]
        picked = [b for b in blocks if any(re.search(rf"<(?:name|title)>[^<]*\b{re.escape(w)}", b) for w in words)]
    return " ".join(picked)


# ---------------------------------------------------------------- amounts


_OTHER_CURRENCY = re.compile(
    r"(?:chf|eur|gbp|sek|nok|dkk|aud|cad|jpy|cny|hkd|sgd|inr|€|£|¥|a\$|c\$|s\$)\s*$")
_UNIT = {"billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mm": 1e6, "mn": 1e6, "m": 1e6}
_AMT_UNIT = re.compile(r"(\$\s?)?(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*(billion|bn|b|million|mm|mn|m)\b")
_AMT_FULL = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3}){2,}|\d{6,})(\.\d+)?(?![\d]|[.,]\d)")
_AMT_SIGN = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+)(\.\d+)?(?![\d]|[.,]\d)(?!\s*(?:billion|bn|million|mm|mn)\b)")
_AMT_BARE = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d+)(\.\d)?(?![\d]|[.,]\d|\s*%)")
_YEARISH = re.compile(r"(?:19|20)\d\d")


def _foreign(text: str, start: int) -> bool:
    return bool(_OTHER_CURRENCY.search(text[max(0, start - 8): start]))


@dataclass(frozen=True)
class _Amounts:
    stated: list[float]  # sorted USD values printed with a unit, a "$" in a millions table, or in full
    bare: list[tuple[int, float]]  # (offset, number) of other numbers: x 1e6 or x 1e3 counts near an anchor


@lru_cache(maxsize=8)
def _amounts(text: str) -> _Amounts:
    stated: list[float] = []
    for m in _AMT_UNIT.finditer(text):
        if len(m[4]) == 1 and not m[1]:  # "$18b" / "$775m", never "item 1b"
            continue
        if not _foreign(text, m.start()):
            stated.append(float(m[2].replace(",", "") + (m[3] or "")) * _UNIT[m[4]])
    for m in _AMT_FULL.finditer(text):
        if not _foreign(text, m.start()):
            x = float(m[1].replace(",", "") + (m[2] or ""))
            stated.append(x)
            if x * 1e3 >= 1e9 and "," in m[1]:  # "2,600,000" in a thousands table
                stated.append(x * 1e3)
    for m in _AMT_SIGN.finditer(text):  # "$ 2,600" in a millions table
        x = float(m[1].replace(",", "") + (m[2] or ""))
        if x < 1e6:
            stated.append(x * 1e6)
    bare: list[tuple[int, float]] = []
    for m in _AMT_BARE.finditer(text):
        if ("," not in m[1] and _YEARISH.fullmatch(m[1])) or _foreign(text, m.start()):
            continue
        bare.append((m.start(), float(m[1].replace(",", "") + (m[2] or ""))))
    return _Amounts(sorted(stated), bare)


def _tolerance(v: float) -> float:
    """Archive sizes are $m figures (amt_m x 1e6): a filing's $2,587.5 million states a 2,588 within $0.5m."""
    return 0.5e6 if v >= 1e7 and v % 1e6 == 0 else max(0.5, v * 1e-9)


Anchors = Mapping[str, Sequence[str]]  # kind ('name', 'coupon', 'maturity', 'amount') -> patterns


def _usd_states(text: str, v: float, anchors: Anchors, window: int) -> bool:
    found = _amounts(text)
    tol = _tolerance(v)
    i = bisect_left(found.stated, v - tol)
    if i < len(found.stated) and found.stated[i] <= v + tol:
        return True
    positions = {k: p for k, pats in anchors.items() if pats and (p := _positions(text, list(pats)))}
    if not positions:
        return False
    for pos, x in found.bare:
        if abs(x * 1e6 - v) > tol and abs(x * 1e3 - v) > tol:
            continue
        kinds = {k for k, p in positions.items() if _near(pos, p, window)}
        # a name, or two terms: this row of the table, not another's
        if ("name" in kinds or len(kinds) >= 2) and not _foreign(text, pos):
            return True
    return False


# ---------------------------------------------------------------- other patterns


def _num(s: str) -> str:
    """A number token: not glued to more digits on either side ("18" never matches inside "118" or "18,5")."""
    return rf"(?<![\d.,]){re.escape(s)}(?![\d]|[.,]\d)"


def _pct_patterns(p: float, *, xml: bool = True) -> list[str]:
    base = f"{p:.6f}".rstrip("0").rstrip(".")
    tail = r"(?:\.0+)?" if "." not in base else r"0*"
    num = rf"(?<![\d.]){re.escape(base)}{tail}(?![\d])"
    pats = [num + r"\s*(?:%|percent\b|per cent\b)"]
    if xml:
        pats.append(r"annualizedrt>\s*" + num + r"\s*<")
    return pats


def _bp_patterns(bp: float) -> list[str]:
    pats = _pct_patterns(bp / 100, xml=False)
    s = f"{bp:.2f}".rstrip("0").rstrip(".")
    pats.append(_num(s) + r"\s*(?:basis points?|bps?)\b")
    return pats


def _month_names(m: int) -> str:
    return "|".join([MONTHS[m - 1], *_MON_ABBR[m]])


def _date_patterns(iso: str) -> list[str]:
    parts = iso.split("-")
    y = int(parts[0])
    if len(parts) == 1:
        return [rf"\b(?:due|matur\w*)\b[^.;]{{0,60}}?\b{y}\b",
                rf"\b{y}\s+(?:senior\s+)?(?:secured\s+)?(?:unsecured\s+)?(?:convertible\s+)?"
                rf"(?:notes|debentures|bonds|term loans?)\b"]
    m = int(parts[1])
    names = _month_names(m)
    if len(parts) == 2:
        return [rf"\b(?:{names})\.?\s+(?:\d{{1,2}}(?:st|nd|rd|th)?,?\s+)?{y}\b",
                rf"\b\d{{1,2}}\s+(?:{names})\.?,?\s+{y}\b", rf"\b{y}-{m:02d}\b",
                rf"(?<![\d/])0?{m}/(?:\d{{1,2}}/)?(?:{y}|{y % 100:02d})(?![\d])"]
    d = int(parts[2])
    return [rf"\b(?:{names})\.?\s+0?{d}(?:st|nd|rd|th)?,?\s+{y}\b", rf"\b0?{d}\s+(?:{names})\.?,?\s+{y}\b",
            rf"\b{y}-{m:02d}-{d:02d}\b", rf"(?<![\d/])0?{m}/0?{d}/(?:{y}|{y % 100:02d})(?![\d])"]


def _number_word(n: int) -> str | None:
    if 0 <= n < len(NUMBER_WORDS):
        return NUMBER_WORDS[n]
    tens, ones = divmod(n, 10)
    if tens in _TENS:
        return _TENS[tens] + (f"[- ]{NUMBER_WORDS[ones]}" if ones else "")
    return None


def _years_patterns(n: float) -> list[str]:
    pats = [_num(f"{n:g}") + r"(?:\s*\(\w+\))?[- ]?(?:years?|yrs?)\b"]
    if n == int(n) and (word := _number_word(int(n))):
        pats.append(rf"\b{word}(?:\s*\({int(n)}\))?[- ](?:years?|yr)\b")
    return pats


def _positions(text: str, patterns: list[str], limit: int = 5000) -> list[int]:
    out: list[int] = []
    for p in patterns:
        for i, m in enumerate(re.finditer(p, text)):
            if i >= limit:
                break
            out.append(m.start())
    return sorted(out)


def _near(pos: int, anchors: list[int], window: int) -> bool:
    i = bisect_left(anchors, pos - window)
    return i < len(anchors) and anchors[i] <= pos + window


def _any(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text) for p in patterns)


# ---------------------------------------------------------------- free text


_TOK_PCT = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s?%")
_TOK_USD = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(billion|bn|b|million|mm|m)?\b", re.I)
_TOK_DATE = re.compile(r"\b(\d{4}-\d{2}(?:-\d{2})?)\b")
_TOK_DAYS = re.compile(r"\b(\d+)\s+(days|months)\b", re.I)
_SPREAD = re.compile(r"(sofr|libor|euribor|sonia|bbsy|bsby|ust|treasury)\s*\+\s*(\d+(?:\.\d+)?)\s*(%|bps?)?", re.I)


def _text_states(text: str, value: str) -> bool | None:
    """Every number the value carries is stated; a value with no number is stated only verbatim."""
    v = value.strip()
    if not v:
        return None
    checks: list[bool] = []
    spread_spans: list[tuple[int, int]] = []
    for m in _SPREAD.finditer(v):
        amount = float(m[2])
        unit = (m[3] or "").lower()
        bp = amount if unit.startswith("bp") or (not unit and "." not in m[2] and amount >= 20) else amount * 100
        checks.append(_any(text, _bp_patterns(bp)))
        spread_spans.append(m.span())
    for m in _TOK_PCT.finditer(v):
        if not any(a <= m.start() < b for a, b in spread_spans):
            checks.append(_any(text, _pct_patterns(float(m[1]))))
    for m in _TOK_USD.finditer(v):
        amount = float(m[1].replace(",", "")) * _UNIT.get((m[2] or "").lower(), 1)
        checks.append(_usd_states(text, amount, {}, WINDOW))
    for m in _TOK_DATE.finditer(v):
        checks.append(_any(text, _date_patterns(m[1])))
    for m in _TOK_DAYS.finditer(v):
        checks.append(bool(re.search(rf"\b{m[1]}\s*(?:\(\w+\)\s*)?{m[2].lower()}\b", text)))
    if checks:
        return all(checks)
    return True if normalize_text(v) in text else None


def _range_states(text: str, value: str) -> bool | None:
    """'2027-2029' (a maturity range): stated as a range ('2027 - 2029', '2027 through 2029', '2027 to 2029')."""
    m = re.fullmatch(r"\s*(\d{4})\s*[-\u2013/]\s*(\d{4})\s*", value)
    if not m:
        return None
    return bool(re.search(rf"\b{m[1]}\s*(?:-|/|to|through|and)\s*{m[2]}\b", text))


# ---------------------------------------------------------------- the check


def family(field: str) -> frozenset[str]:
    return next((f for f in FAMILIES if field in f), frozenset({field}))


def value_patterns(field: str, value: Any) -> list[str]:
    """Regexes (``normalize_text`` form) that state a non-amount ``value`` on their own. Amounts: ``states``."""
    if value is None or value == "" or value == []:
        return []
    if field in PCT_FIELDS:
        return _pct_patterns(float(value))
    if field in BP_FIELDS:
        return _bp_patterns(float(value))
    if field in BENCHMARK_FIELDS:
        return list(BENCHMARK_WORDS.get(str(value).upper(), (rf"\b{re.escape(str(value).lower())}\b",)))
    if field in DATE_FIELDS and re.fullmatch(r"\d{4}(?:-\d{2}){0,2}", str(value)):
        return _date_patterns(str(value))
    if field in YEARS_FIELDS:
        return _years_patterns(float(value))
    if field == "coupon":
        from marden.seed.instruments_parse import parse_coupon

        parsed = parse_coupon(str(value))
        if "coupon_spread_bp" in parsed:
            return _bp_patterns(parsed["coupon_spread_bp"])
        if "coupon_rate_pct" in parsed:
            return _pct_patterns(parsed["coupon_rate_pct"])
    return []


def name_anchor(name: str | None) -> list[str]:
    """The instrument's name as an anchor: its first three words, each by its first four letters ("Finance
    lease liabilities" -> fina.. leas.. liab..; "DDTL 2.0 Facility" -> ddtl 2.0); one word is not enough."""
    words = [w for w in re.findall(r"[a-z]+|\d+(?:\.\d+)?[a-z]?", (name or "").lower())
             if (len(w) >= 2 or w.isdigit()) and w not in _NAME_STOP]
    if len(words) < 2:
        return []
    gap = r"\W+(?:\w+\W+){0,2}"  # the name's own stop words ("loan", "due") may sit between
    return [r"\b" + gap.join(re.escape(w[:4]) + (r"\w*" if w[0].isalpha() else "") for w in words[:3])]


def anchor_patterns(terms: Mapping[str, Any], field: str, *, name: str | None = None) -> dict[str, list[str]]:
    """The instrument's name and other terms (families other than ``field``'s) as anchors for bare table
    amounts, by kind. A benchmark ('SOFR') or a bare year is on every page of a filing: neither anchors."""
    own = family(field)
    out: dict[str, list[str]] = {"name": name_anchor(name)}
    for f, v in terms.items():
        if f in own or v is None or v == "" or f in BENCHMARK_FIELDS or f == "coupon_type":
            continue
        if f in USD_FIELDS:
            try:
                x = float(v)
            except (TypeError, ValueError):
                continue
            if x > 0:
                out.setdefault("amount", []).extend([rf"\b{re.escape(f'{x / 1e6:,.0f}')}\s*(?:million|mm)\b",
                                                     rf"\b{re.escape(f'{x / 1e9:g}')}\s*(?:billion|bn)\b"])
            continue
        kind = "maturity" if f in DATE_FIELDS or f == "maturity_text" else "coupon" if f in family("coupon") else f
        out.setdefault(kind, []).extend(value_patterns(f, v))
    return {k: v for k, v in out.items() if v}


def states(text: str | None, field: str, value: Any, *, anchors: Anchors | None = None,
           window: int = WINDOW) -> bool | None:
    """Does ``text`` (``normalize_text`` form, N-PORT reports cut by ``scope``) state ``value`` for ``field``?

    True / False; None when there is nothing to check (no text, a zero amount, a numberless text value that
    is not quoted verbatim). ``anchors`` (``anchor_patterns``) let bare table amounts count near them.
    """
    if text is None or value is None or value == "" or value == []:
        return None
    if field in USD_FIELDS:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None
        return None if v <= 0 else _usd_states(text, v, anchors or {}, window)
    pats = value_patterns(field, value)
    if pats:
        return _any(text, pats)
    if field == "maturity_text" and (ranged := _range_states(text, str(value))) is not None:
        return ranged
    return _text_states(text, str(value)) if isinstance(value, str) else None


@dataclass(frozen=True)
class Evidence:
    """Where a value was found: the first candidate URL whose stored text states it."""

    url: str
    document_id: int | None


def first_stating(lookup: CitedDocs | TextLookup, urls: Sequence[str], field: str, value: Any, *,
                  anchors: Anchors | None = None, ids: Sequence[str] = (),
                  names: Sequence[str] = ()) -> Evidence | None:
    """The first of ``urls`` whose stored text states the value (N-PORT reports cut to this instrument)."""
    for url in urls:
        if states(scope(lookup(url), ids=ids, names=names), field, value, anchors=anchors):
            return Evidence(url, getattr(lookup, "document_ids", {}).get(url))
    return None
