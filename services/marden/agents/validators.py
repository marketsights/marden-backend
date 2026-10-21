"""Code validators on every candidate (spec 22 layers 1 and 3): quote, number, unit, range, dates, sums.

Hard checks reject a candidate outright (an extracted value without its exact passage is never a fact):
    quote_found      the quote appears verbatim in the document text (normalized whitespace; marden.agents.text)
    value_shape      the value has the field's type (number, ISO date, text, list, object)
    subject_kind     the subject kind is one the field allows
    number_in_quote  a number field's value is written in the quote (with its scale word: '$2.6 billion',
                     '5.50%' for 550 bp, '1.35x')
    year_in_quote    a date field's year is written in the quote
    range            numbers and dates within sane bounds for the unit
    value_numbers    text, list and object values: every number in the value (a JSON number, '760.33', the
                     '5.50%' in 'Term SOFR + 5.50%') is written in the quote, with its scale word or a table's
                     stated scale; ISO dates inside a value are not amounts and are left to the verifier
    identifier_in_document  identifiers inside an object value (docket, permit or case number, queue id) are
                     printed somewhere in the document (they key later subject links)
Soft checks send a candidate to review instead of publishing it:
    implied_scale    the number matches only through a table's unstated scale ('in thousands')
    month_in_quote   a date's (or month's) month is not written in the quote
    date_order       closing <= commitment termination <= maturity on one subject
    tranche_sum      tranche amounts add to the facility size (within 0.5%)
"""

from __future__ import annotations

import itertools
import re
from collections import defaultdict
from datetime import date
from typing import Any

from marden.agents.extract import Candidate
from marden.agents.fields import NUMERIC, RANGES, fields_for
from marden.agents.text import DocText, find_quote, normalize

_PAGE_MARK = re.compile(r"\s*\[page \d+\]\s*")
_NUM = re.compile(
    r"(?<![\w.])\(?\$?\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\)?\s*"
    r"(trillion|billion|bn|b\b|million|mm\b|m\b|mln|thousand|kw\b|mw\b|gw\b|k\b|%|percent|per cent|basis points"
    r"|bps?\b|x\b|\u00d7)?",
    re.I,
)
SCALES = {"trillion": 1e12, "billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mm": 1e6, "m": 1e6, "mln": 1e6,
          "thousand": 1e3, "k": 1e3}
POWER = {"kw": 1e-3, "mw": 1.0, "gw": 1e3}  # to MW
TABLE_SCALE = re.compile(r"(?:dollars |amounts |shares |\$ )?in (thousands|millions|billions)\b", re.I)
TABLE_SCALES = {"thousands": 1e3, "millions": 1e6, "billions": 1e9}
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
DATE_MIN, DATE_MAX = date(1990, 1, 1), date(2100, 12, 31)


def _check(ok: bool, hard: bool, detail: str = "") -> dict[str, Any]:
    return {"ok": bool(ok), "hard": hard, **({"detail": detail} if detail else {})}


def numbers_in(quote: str) -> list[tuple[float, str]]:
    """(number, suffix) pairs written in a quote; the suffix is the scale or unit word, lower case."""
    out = []
    for m in _NUM.finditer(normalize(quote)):
        raw = m.group(1).replace(",", "") + (m.group(2) or "")
        try:
            out.append((float(raw), (m.group(3) or "").lower().strip()))
        except ValueError:
            continue
    return out


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(1e-9, abs(b) * 1e-6)


def number_matches(value: float, vtype: str, quote: str, table_scale: float | None = None) -> tuple[bool, bool]:
    """(explicit match, match only through an unstated x1,000 / x1,000,000 table scale).

    `table_scale` is the scale a table states above the quote ('(dollars in millions)'): a bare number
    times that scale is an explicit match."""
    implied = False
    for num, suffix in numbers_in(quote):
        if vtype == "mw" and suffix in POWER:
            if _close(value, num * POWER[suffix]):
                return True, False
            continue
        scale = SCALES.get(suffix, 1.0)
        options = [num * scale]
        if vtype == "usd" and suffix == "" and table_scale:
            options.append(num * table_scale)
        if vtype == "bp" and suffix in ("%", "percent", "per cent"):
            options.append(num * 100)
        if vtype == "pct" and suffix in ("basis points", "bp", "bps"):
            options.append(num / 100)
        if any(_close(value, o) for o in options):
            return True, False
        if vtype == "usd" and suffix == "" and any(_close(value, num * s) for s in (1e3, 1e6)):
            implied = True
    return False, implied


def stated_scale(doc: DocText, hit: object, window: int = 4000) -> float | None:
    """The nearest '(in millions)'-style scale stated before a quote, within `window` characters."""
    if hit is None:
        return None
    start = getattr(hit, "start", 0)
    found = list(TABLE_SCALE.finditer(doc.text, max(0, start - window), start))
    return TABLE_SCALES[found[-1].group(1).lower()] if found else None


def _date_value(value: str) -> date | None:
    try:
        return date.fromisoformat(value if len(value) == 10 else f"{value}-01")
    except ValueError:
        return None


_ISO_VALUE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
ID_KEYS = frozenset({"number", "docket", "case_number", "permit", "permit_number", "queue_id", "cusip", "isin", "lei",
                     "cik", "accession"})
GENERIC_TYPES = ("usd", "mw", "bp", "pct")
_ID_SPLIT = re.compile(r"\s*(?:[,;/&|]|\band\b|\s)\s*")


def _value_parts(value: Any, key: str | None, numbers: list[tuple[str, Any]], idents: list[str]) -> None:
    """Collect the numbers ('json', n) / ('text', (n, suffix)) and identifiers inside a text, list or object value."""
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, int | float):
        numbers.append(("json", float(value)))
    elif isinstance(value, str):
        s = value.strip()
        if not s or _ISO_VALUE.match(s):
            return
        if key in ID_KEYS:
            idents.append(s)
            return
        numbers.extend(("text", pair) for pair in numbers_in(s))
    elif isinstance(value, list):
        for item in value:
            _value_parts(item, None, numbers, idents)
    elif isinstance(value, dict):
        for k, v in value.items():
            _value_parts(v, str(k).strip().lower(), numbers, idents)


def _text_number_written(num: float, suffix: str, quote_numbers: list[tuple[float, str]]) -> bool:
    scaled = num * (POWER.get(suffix) or SCALES.get(suffix, 1.0))
    for qn, qs in quote_numbers:
        if _close(num, qn) or _close(scaled, qn * (POWER.get(qs) or SCALES.get(qs, 1.0))):
            return True
    return False


def value_number_checks(value: Any, quote: str, doc: DocText, hit: object) -> dict[str, Any]:
    """Checks for the numbers and identifiers inside a text / list / object value (see the module docstring)."""
    numbers: list[tuple[str, Any]] = []
    idents: list[str] = []
    _value_parts(value, None, numbers, idents)
    out: dict[str, Any] = {}
    if numbers:
        scale = stated_scale(doc, hit)
        in_quote = numbers_in(quote)
        missing, implied_only = [], []
        for kind, item in numbers:
            if kind == "json":
                if any(number_matches(item, vt, quote, scale)[0] for vt in GENERIC_TYPES):
                    continue
                if number_matches(item, "usd", quote, scale)[1]:
                    implied_only.append(item)
                    continue
                missing.append(item)
            elif not _text_number_written(item[0], item[1], in_quote):
                missing.append(f"{item[0]:g}{(' ' + item[1]) if item[1] else ''}")
        out["value_numbers"] = _check(not missing, True, "" if not missing else
                                      "not written in the quote: " + ", ".join(str(m) for m in missing[:6]))
        if implied_only and not missing:
            out["implied_scale"] = _check(False, False, "matches only with an unstated table scale")
    if idents:
        full = normalize(doc.text).lower()
        # '182126 / GHGPSDTX263 / PSDTX1688' or '182126, GHGPSDTX263 and PSDTX1688': each identifier on its own
        tokens = [t for i in idents for t in _ID_SPLIT.split(normalize(i).lower()) if len(t) >= 3]
        absent = [t for t in tokens if t not in full]
        out["identifier_in_document"] = _check(not absent, True, "" if not absent else
                                               "not in the document: " + ", ".join(absent[:4]))
    return out


def check_candidate(c: Candidate, doc: DocText, today: date | None = None) -> None:
    today = today or date.today()
    spec = fields_for(c.area).get(c.field)
    checks: dict[str, Any] = {}
    quote = _PAGE_MARK.sub(" ", c.quote).strip()
    c.quote = quote
    hit = find_quote(doc, quote) if quote else None
    c.hit = hit
    checks["quote_found"] = _check(hit is not None, True, "" if hit else "not in the document text")
    if hit is not None:
        c.page = hit.page
        if hit.mode != "exact":
            checks["quote_found"]["detail"] = hit.mode
    checks["value_shape"] = _check(c.value is not None and not c.problems, True, "; ".join(c.problems))
    if spec is None:
        checks["subject_kind"] = _check(False, True, "unknown field")
        c.checks = checks
        return
    checks["subject_kind"] = _check(c.subject_kind in spec.kinds, True,
                                    "" if c.subject_kind in spec.kinds
                                    else f"{c.field} is not a {c.subject_kind} field")
    if c.value is not None and spec.vtype in NUMERIC and spec.vtype != "number":
        explicit, implied = number_matches(float(c.value), spec.vtype, quote, stated_scale(doc, hit))
        checks["number_in_quote"] = _check(explicit or implied, True, "" if explicit or implied else
                                           f"{c.value} is not written in the quote")
        if implied and not explicit:
            checks["implied_scale"] = _check(False, False, "matches only with an unstated table scale")
        lo, hi = RANGES[spec.vtype]
        checks["range"] = _check(lo <= float(c.value) <= hi, True, f"outside {lo}-{hi} {spec.unit}")
    elif c.value is not None and spec.vtype == "number":
        explicit, _ = number_matches(float(c.value), "number", quote)
        checks["number_in_quote"] = _check(explicit, True, "" if explicit else f"{c.value} not in the quote")
    elif c.value is not None and spec.vtype in ("text", "list", "object"):
        checks.update(value_number_checks(c.value, quote, doc, hit))
    if c.value is not None and spec.vtype in ("date", "month"):
        day = _date_value(str(c.value))
        checks["range"] = _check(day is not None and DATE_MIN <= day <= DATE_MAX, True, "date out of range")
        if day is not None:
            norm = normalize(quote).lower()
            year_ok = str(day.year) in norm or f"'{str(day.year)[2:]}" in norm
            checks["year_in_quote"] = _check(year_ok, True, "" if year_ok else f"{day.year} not in the quote")
            if len(str(c.value)) in (7, 10):  # YYYY-MM-DD or a month field's YYYY-MM
                month = MONTHS[day.month - 1]
                written = (month[:3] in norm or f"{day.month}/" in norm or f"-{day.month:02d}-" in norm
                           or f"{day.month:02d}/" in norm)
                checks["month_in_quote"] = _check(written, False, "" if written else f"{month} not in the quote")
    if c.as_of is not None and c.as_of > today:
        c.as_of = None  # a future 'as of' is a value, not the date it became true
    c.checks = checks


def _subject_key(c: Candidate) -> tuple[str, str]:
    return c.subject_kind, normalize(c.subject_identifier or c.subject_name).lower()


def check_group(cands: list[Candidate]) -> None:
    """Cross-candidate checks on one document: date order and tranche sums, per subject."""
    by_subject: dict[tuple[str, str], dict[str, list[Candidate]]] = defaultdict(lambda: defaultdict(list))
    for c in cands:
        if not c.hard_fail:
            by_subject[_subject_key(c)][c.field].append(c)
    for fields in by_subject.values():
        order = ["closing_date", "commitment_termination_date", "maturity"]
        dated = [(f, _date_value(str(fields[f][0].value))) for f in order if fields.get(f)]
        dated = [(f, d) for f, d in dated if d is not None]
        for (f1, d1), (f2, d2) in itertools.pairwise(dated):
            if d1 > d2:
                for f in (f1, f2):
                    for c in fields[f]:
                        c.checks["date_order"] = _check(False, False, f"{f1} {d1} is after {f2} {d2}")
        for t in fields.get("tranches", []):
            items = (t.value or {}).get("tranches") if isinstance(t.value, dict) else None
            amounts = [x.get("amount_usd") for x in items or [] if isinstance(x, dict)]
            if not amounts or not all(isinstance(a, int | float) for a in amounts):
                continue
            total = float(sum(amounts))
            for size in fields.get("amount_usd", []) + fields.get("commitment_usd", []):
                ok = abs(total - float(size.value)) <= 0.005 * float(size.value)
                detail = "" if ok else f"tranches add to {total:,.0f}, facility {float(size.value):,.0f}"
                for c in (t, size):
                    c.checks["tranche_sum"] = _check(ok, False, detail)


def validate(cands: list[Candidate], doc: DocText, today: date | None = None) -> list[Candidate]:
    for c in cands:
        check_candidate(c, doc, today)
    check_group(cands)
    return cands
