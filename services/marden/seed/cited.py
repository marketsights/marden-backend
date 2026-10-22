"""Does the document a seeded value cites actually state it? (tag F needs a filed document that states it)

    docs = CitedDocs(conn)
    states(docs.text(url), "amount_usd", 18_000_000_000)   # True / False / None

The archive carries no per-field source: an aidebt row lists every URL its research used (``src``), and the
loaders tagged each field with the first one's host. A fund's N-CSRS listing its own $1.2mm slice of a loan
does not state the loan's $18B size, so a host rule alone is not enough for F. ``states`` looks for the value
in the stored text of the cited document, in the number and date formats filings use:

- USD: "$18.0 billion", "$18bn", "18,000,000,000", "18,000,000" (a thousands table), "$18,000" (a millions
  table, with a "$" sign or "million" after it), "775.0 million", and bare digits in N-PORT XML;
- percents: "6.875%", "6.87500%", "6.875 percent", N-PORT ``annualizedRt`` 6.87500000; spreads also as
  "250 basis points" / "250 bps";
- dates: "November 9, 2029", "Nov. 9, 2029", "9 November 2029", "11/09/2029", "11/9/29", "2029-11-09"; a
  month ("2032-03") is stated by "March 2032" or any day in it; a year by "due ... 2032" / "2032 Notes";
- years of term: "15 years", "15-year", "fifteen (15) years", "fifteen-year";
- free text: each number it carries (percents, dollar amounts, dates, "120 days") must be stated; text with
  none is stated only verbatim.

``states`` returns None when there is nothing it can check (no stored text, a zero, an empty value); callers
treat None like False: no F without a document that states the value.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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

# Pages that are not a document: a search form, an EDGAR company browse page, a filing's directory index.
_NOT_A_DOCUMENT = re.compile(r"/edgar/search/|cgi-bin/browse-edgar|/$")

MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")
_MON_ABBR = {1: ("jan",), 2: ("feb",), 3: ("mar",), 4: ("apr",), 5: (), 6: ("jun",), 7: ("jul",), 8: ("aug",),
             9: ("sep", "sept"), 10: ("oct",), 11: ("nov",), 12: ("dec",)}
NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
                "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen",
                "twenty")
_TENS = {20: "twenty", 30: "thirty", 40: "forty", 50: "fifty"}
BENCHMARK_WORDS = {
    "SOFR": (r"\bsofr\b", r"secured overnight financing rate"),
    "LIBOR": (r"\blibor\b", r"london interbank offered rate"),
    "EURIBOR": (r"\beuribor\b", r"euro interbank offered rate"),
    "SONIA": (r"\bsonia\b", r"sterling overnight index average"),
    "BBSY": (r"\bbbsy\b",),
    "BSBY": (r"\bbsby\b",),
}


# ---------------------------------------------------------------- document text


def is_document_url(url: str | None) -> bool:
    return bool(url) and not _NOT_A_DOCUMENT.search(str(url))


def normalize_text(text: str) -> str:
    """Lower-case, whitespace-folded text (marden.agents.text.normalize), the form ``states`` searches."""
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


# ---------------------------------------------------------------- number patterns


def _num(s: str) -> str:
    """A number token: not glued to more digits on either side ("18" never matches inside "118" or "18,5")."""
    return rf"(?<![\d.,]){re.escape(s)}(?![\d]|[.,]\d)"


def _decimals(x: float, most: int = 3) -> list[str]:
    """x written with 0..most decimals, keeping only the spellings that equal x ("2.6" -> 2.6, 2.60, 2.600)."""
    out: list[str] = []
    for d in range(most + 1):
        s = f"{x:,.{d}f}"
        if abs(float(s.replace(",", "")) - x) < 1e-9 and s not in out:
            out.append(s)
    return out


def _usd_patterns(v: float) -> list[str]:
    pats: list[str] = []
    if v == int(v):
        iv = int(v)
        pats += [_num(f"{iv:,}"), rf"(?<![\d.,]){iv}(?:\.0+)?(?![\d]|[.,]\d)"]
        if iv % 1000 == 0 and iv >= 1_000_000_000:  # a thousands table ("2,600,000")
            pats.append(_num(f"{iv // 1000:,}"))
    m = v / 1e6
    for s in _decimals(m, 2):
        pats.append(_num(s) + r"\s*(?:million|mm|mn|m)\b")
        if m >= 1000:  # a millions table: "$ 2,600" (the sign keeps "2,600 employees" out)
            pats.append(r"\$\s?" + _num(s))
    b = v / 1e9
    if b >= 0.1:
        for s in _decimals(b, 3):
            pats.append(_num(s) + r"\s*(?:billion|bn|b)\b")
    return pats


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


def _month_names(m: int) -> list[str]:
    return [MONTHS[m - 1], *_MON_ABBR[m]]


def _date_patterns(iso: str) -> list[str]:
    parts = iso.split("-")
    y = int(parts[0])
    if len(parts) == 1:
        return [rf"\b(?:due|matur\w*)\b[^.;]{{0,60}}?\b{y}\b", rf"\b{y}\s+(?:senior\s+)?(?:secured\s+)?"
                rf"(?:unsecured\s+)?(?:convertible\s+)?(?:notes|debentures|bonds|term loans?)\b"]
    m = int(parts[1])
    names = "|".join(_month_names(m))
    if len(parts) == 2:
        return [rf"\b(?:{names})\.?\s+(?:\d{{1,2}}(?:st|nd|rd|th)?,?\s+)?{y}\b",
                rf"\b\d{{1,2}}\s+(?:{names})\.?,?\s+{y}\b", rf"\b{y}-{m:02d}\b",
                rf"(?<![\d/]){m:02d}/(?:\d{{1,2}}/)?(?:{y}|{y % 100:02d})(?![\d])",
                rf"(?<![\d/]){m}/(?:\d{{1,2}}/)?(?:{y}|{y % 100:02d})(?![\d])"]
    d = int(parts[2])
    return [rf"\b(?:{names})\.?\s+0?{d}(?:st|nd|rd|th)?,?\s+{y}\b", rf"\b0?{d}\s+(?:{names})\.?,?\s+{y}\b",
            rf"\b{y}-{m:02d}-{d:02d}\b",
            rf"(?<![\d/])0?{m}/0?{d}/(?:{y}|{y % 100:02d})(?![\d])"]


def _number_word(n: int) -> str | None:
    if 0 <= n < len(NUMBER_WORDS):
        return NUMBER_WORDS[n]
    tens, ones = divmod(n, 10)
    if tens * 10 in _TENS:
        return _TENS[tens * 10] + (f"[- ]{NUMBER_WORDS[ones]}" if ones else "")
    return None


def _years_patterns(n: float) -> list[str]:
    s = f"{n:g}"
    pats = [_num(s) + r"(?:\s*\(\w+\))?[- ]?(?:years?|yrs?)\b"]
    if n == int(n) and (word := _number_word(int(n))):
        pats.append(rf"\b{word}(?:\s*\({int(n)}\))?[- ](?:years?|yr)\b")
    return pats


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
    spans: list[tuple[int, int]] = []
    for m in _SPREAD.finditer(v):
        amount = float(m[2])
        bp = amount if (m[3] or "").lower().startswith("bp") or ("." not in m[2] and amount >= 20) else amount * 100
        checks.append(_any(text, _bp_patterns(bp)))
        spans.append(m.span())
    for m in _TOK_PCT.finditer(v):
        if any(a <= m.start() < b for a, b in spans):
            continue
        checks.append(_any(text, _pct_patterns(float(m[1]))))
    for m in _TOK_USD.finditer(v):
        amount = float(m[1].replace(",", ""))
        unit = (m[2] or "").lower()
        amount *= 1e9 if unit in ("billion", "bn", "b") else 1e6 if unit in ("million", "mm", "m") else 1
        checks.append(_any(text, _usd_patterns(amount)))
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

WINDOW = 400  # chars: a bare table amount counts as this instrument's when another of its terms is this close
FAMILIES = (
    frozenset({"coupon", "coupon_type", "coupon_rate_pct", "coupon_spread_bp", "coupon_benchmark", "rate_text"}),
    frozenset({"maturity", "maturity_text"}),
)


def family(field: str) -> frozenset[str]:
    return next((f for f in FAMILIES if field in f), frozenset({field}))


def value_patterns(field: str, value: Any) -> list[str]:
    """Regexes (for ``normalize_text`` text) that state ``value`` on their own (with a unit, sign or month)."""
    if value is None or value == "" or value == []:
        return []
    if field in USD_FIELDS:
        v = float(value)
        if v <= 0:
            return []
        return _usd_patterns(v)
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


def anchor_patterns(terms: Mapping[str, Any], field: str) -> list[str]:
    """Patterns of the instrument's other terms (families other than ``field``'s): an amount printed bare in a
    table ("750" under "$ in millions") counts only next to one of them. A maturity anchors by its year."""
    own = family(field)
    out: list[str] = []
    for f, v in terms.items():
        if f in own or v is None or v == "":
            continue
        if f in DATE_FIELDS and re.match(r"\d{4}", str(v)):
            out.append(rf"\b{str(v)[:4]}\b")
        else:
            out += value_patterns(f, v)
    return out


def _usd_bare_patterns(v: float) -> list[str]:
    pats: list[str] = []
    if v == int(v) and int(v) % 1000 == 0:
        pats.append(_num(f"{int(v) // 1000:,}"))  # a thousands table
    for s in _decimals(v / 1e6, 1):
        if v / 1e6 >= 1:
            pats.append(_num(s))  # a millions table
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


def scope(text: str | None, *, ids: list[str] | tuple[str, ...] = (),
          names: list[str] | tuple[str, ...] = ()) -> str | None:
    """A fund holdings report (N-PORT XML) lists hundreds of securities: keep only the holdings that are this
    instrument (by CUSIP / ISIN, else by issuer name), so another holding's coupon is not read as its own.
    Any other document is returned whole. No matching holding: '' (the report does not state the value)."""
    if not text or "<invstorsec>" not in text:
        return text
    blocks = re.findall(r"<invstorsec>.*?</invstorsec>", text)
    keys = [i.lower() for i in ids if i]
    picked = [b for b in blocks if any(k in b for k in keys)] if keys else []
    if not picked:
        words = [w for n in names if n for w in [n.lower().strip()] if len(w) >= 3]
        picked = [b for b in blocks if any(re.search(rf"<(?:name|title)>[^<]*\b{re.escape(w)}", b) for w in words)]
    return " ".join(picked)


_OTHER_CURRENCY = re.compile(r"(?:chf|eur|gbp|sek|nok|dkk|aud|cad|jpy|cny|hkd|sgd|inr|€|£|¥|a\$|c\$|s\$)\s*$")


def _usd_match(text: str, m: re.Match[str]) -> bool:
    """Not an amount in another currency ("CHF 275,000", "€500 million")."""
    return not _OTHER_CURRENCY.search(text[max(0, m.start() - 8): m.start()])


def states(text: str | None, field: str, value: Any, *, anchors: list[str] | tuple[str, ...] = (),
           window: int = WINDOW) -> bool | None:
    """Does ``text`` (``normalize_text`` form) state ``value`` for ``field``? None: nothing checkable.

    A value is stated by any of its unit-bearing forms anywhere in the text; an amount also by a bare table
    number ("750", "275,000") within ``window`` chars of an ``anchors`` match (the instrument's other terms,
    ``anchor_patterns``). Amounts in another currency do not count. Fund holdings reports should be cut to
    this instrument's holdings first (``scope``).
    """
    if text is None or value is None or value == "" or value == []:
        return None
    pats = value_patterns(field, value)
    if not pats:
        if field in USD_FIELDS:  # zero or negative: nothing a document would print to check
            return None
        if field == "maturity_text":
            ranged = _range_states(text, str(value))
            if ranged is not None:
                return ranged
        return _text_states(text, str(value)) if isinstance(value, str) else None
    usd = field in USD_FIELDS
    for p in pats:
        if any(not usd or _usd_match(text, m) for m in re.finditer(p, text)):
            return True
    if not (usd and anchors):
        return False
    anchor_pos = _positions(text, list(anchors))
    if not anchor_pos:
        return False
    for p in _usd_bare_patterns(float(value)):
        for m in re.finditer(p, text):
            if _near(m.start(), anchor_pos, window) and _usd_match(text, m):
                return True
    return False


@dataclass(frozen=True)
class Evidence:
    """Where a value was found: the first candidate URL whose stored text states it."""

    url: str
    document_id: int | None


def first_stating(lookup: CitedDocs | TextLookup, urls: list[str], field: str, value: Any, *,
                  anchors: list[str] | tuple[str, ...] = (), ids: list[str] | tuple[str, ...] = (),
                  names: list[str] | tuple[str, ...] = ()) -> Evidence | None:
    """The first of ``urls`` whose stored text states the value (N-PORT reports scoped to this instrument)."""
    for url in urls:
        if states(scope(lookup(url), ids=ids, names=names), field, value, anchors=anchors):
            ids = getattr(lookup, "document_ids", {})
            return Evidence(url, ids.get(url))
    return None
