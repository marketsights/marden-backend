"""Dates as the sources state them ('Q3 2028', 'H1 CY2027', 'early 2027', 'Feb-1-2027', '2026-H2', '2028') as
month windows, for the cushion model. Pure functions; nothing is guessed beyond what the text says.

A month is an integer index (year * 12 + month - 1). Each expression found in a text becomes a `When`:
the window [lo, hi] of months it allows (a quarter is 3 months, a half 6, 'early' Jan-Apr, 'mid' May-Aug,
'late' / 'end' / 'YE' Oct-Dec, a year 12), its precision and where it sits in the text. Two kinds are set
aside rather than read as targets:

- statement dates: when a plan was said, not when it is due: '(Vantage, Aug-2025)', '(10-K 2026-07-29)',
  '(unchanged since Dec-2025)', 'Jun-11-2026 call (DCD): ...', 'Oracle Jun-2026: ...';
- non-schedule dates: a draw period, maturity, installment, purchase or announcement date
  ('draws through 2027-06-30', 'announced 27 Apr 2026').

`clause_kind` tells which milestone a clause of text is about: grid / gas supply ('energization Q1 2027'),
building shells ('Bldg 1 TDLR completion 2027-02-15'), rent ('rent commencement'), else a hall delivery.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
}
_MON = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|"
        r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_YR = r"(20[2-3]\d)"
_YR2 = r"(20[2-3]\d|[2-3]\d)"
_CAL = r"(?:calendar\s+(?:year\s+)?|cy\s*)?"
PRECISION_RANK = {"day": 0, "month": 1, "quarter": 2, "part": 3, "half": 4, "year": 5, "range": 6}
PARTS = {"early": (1, 4), "beginning": (1, 2), "mid": (5, 8), "middle": (5, 8), "late": (10, 12), "end": (10, 12)}
_ORD = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4}


def month_index(year: int, month: int) -> int:
    return year * 12 + month - 1


def month_of(day: date) -> int:
    return month_index(day.year, day.month)


def ym(m: int) -> str:
    return f"{m // 12:04d}-{m % 12 + 1:02d}"


def month_start(m: int) -> date:
    return date(m // 12, m % 12 + 1, 1)


def month_end(m: int) -> date:
    nxt = month_start(m + 1)
    return date.fromordinal(nxt.toordinal() - 1)


def _year(text: str) -> int:
    y = int(text)
    return y + 2000 if y < 100 else y


@dataclass(frozen=True)
class When:
    lo: int
    hi: int
    precision: str
    start: int
    end: int
    text: str
    role: str = "target"  # 'target' | 'said' (statement date) | 'other' (draw, maturity, purchase ...)

    @property
    def mid(self) -> float:
        return (self.lo + self.hi + 1) / 2

    @property
    def rank(self) -> int:
        return PRECISION_RANK[self.precision]

    def contains(self, other: When) -> bool:
        return self.lo <= other.lo and other.hi <= self.hi

    def label(self) -> str:
        return ym(self.lo) if self.lo == self.hi else f"{ym(self.lo)}..{ym(self.hi)}"


def _day(y: int, mo: int, d: int) -> tuple[int, int] | None:
    try:
        date(y, mo, d)
    except ValueError:
        return None
    m = month_index(y, mo)
    return m, m


# (regex, precision, function(match) -> (lo, hi) | None); applied in order, a matched span is consumed.
def _iso_day(m: re.Match) -> tuple[int, int] | None:
    return _day(int(m[1]), int(m[2]), int(m[3]))


def _us_day(m: re.Match) -> tuple[int, int] | None:
    return _day(_year(m[3]), int(m[1]), int(m[2]))


def _mon_day_year(m: re.Match) -> tuple[int, int] | None:
    return _day(int(m[3]), MONTHS[m[1][:3].lower()], int(m[2]))


def _day_mon_year(m: re.Match) -> tuple[int, int] | None:
    return _day(int(m[3]), MONTHS[m[2][:3].lower()], int(m[1]))


def _mon_year(m: re.Match) -> tuple[int, int]:
    idx = month_index(_year(m[2]), MONTHS[m[1][:3].lower()])
    return idx, idx


def _iso_month(m: re.Match) -> tuple[int, int]:
    idx = month_index(int(m[1]), int(m[2]))
    return idx, idx


def _quarter(q: int, y: int) -> tuple[int, int]:
    lo = month_index(y, 3 * q - 2)
    return lo, lo + 2


def _half(h: int, y: int) -> tuple[int, int]:
    lo = month_index(y, 1 if h == 1 else 7)
    return lo, lo + 5


def _part(word: str, y: int) -> tuple[int, int]:
    a, b = PARTS[word]
    return month_index(y, a), month_index(y, b)


_PATTERNS: list[tuple[re.Pattern[str], str, object]] = [
    (re.compile(rf"(?<![\d/]){_YR}-(\d{{1,2}})-(\d{{1,2}})(?!\d)"), "day", _iso_day),
    (re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})/(20[2-3]\d|[2-3]\d)(?!\d)"), "day", _us_day),
    (re.compile(rf"\b{_MON}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+{_YR}\b", re.I), "day", _mon_day_year),
    (re.compile(rf"\b{_MON}-(\d{{1,2}})-{_YR}\b", re.I), "day", _mon_day_year),
    (re.compile(rf"\b(\d{{1,2}})\s+{_MON}\.?,?\s+{_YR}\b", re.I), "day", _day_mon_year),
    (re.compile(rf"\b{_MON}\.?[-\s]+(?:of\s+)?'?{_YR2}\b", re.I), "month", _mon_year),
    (re.compile(rf"(?<![\d-]){_YR}-(0[1-9]|1[0-2])(?![\d])"), "month", _iso_month),
    (re.compile(rf"\bq([1-4])\s*(?:of\s+)?{_CAL}'?{_YR2}\b", re.I), "quarter",
     lambda m: _quarter(int(m[1]), _year(m[2]))),
    (re.compile(rf"\b{_YR}[-\s]?q([1-4])\b", re.I), "quarter", lambda m: _quarter(int(m[2]), int(m[1]))),
    (re.compile(rf"\b(first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter\s+(?:of\s+)?{_CAL}{_YR}\b", re.I),
     "quarter", lambda m: _quarter(_ORD[m[1].lower()], int(m[2]))),
    (re.compile(rf"\b([12])h\s*-?\s*{_CAL}'?{_YR2}\b", re.I), "half", lambda m: _half(int(m[1]), _year(m[2]))),
    (re.compile(rf"\bh([12])\s*-?\s*{_CAL}{_YR2}\b", re.I), "half", lambda m: _half(int(m[1]), _year(m[2]))),
    (re.compile(rf"\b{_YR}[-\s]h([12])\b", re.I), "half", lambda m: _half(int(m[2]), int(m[1]))),
    (re.compile(rf"\b(first|second)\s+half\s+(?:of\s+)?{_CAL}{_YR}\b", re.I), "half",
     lambda m: _half(1 if m[1].lower() == "first" else 2, int(m[2]))),
    (re.compile(rf"\b(early|mid|middle|late|end|beginning)(?:\s+of)?(?:\s+the)?[-\s]+{_CAL}{_YR}\b", re.I), "part",
     lambda m: _part(m[1].lower(), int(m[2]))),
    (re.compile(rf"\b(?:ye|year[-\s]end)\s*'?{_YR2}\b", re.I), "part", lambda m: _part("end", _year(m[1]))),
    (re.compile(rf"\b{_YR}-(mid|early|late|end)\b", re.I), "part", lambda m: _part(m[2].lower(), int(m[1]))),
    (re.compile(rf"(?<![\d-]){_YR}\s*[-\u2013]\s*{_YR}(?![\d-])"), "range",
     lambda m: (month_index(int(m[1]), 1), month_index(int(m[2]), 12))),
    (re.compile(rf"(?<![\d$.,/-]){_CAL}{_YR}(?![\d])(?!-\d)", re.I), "year",
     lambda m: (month_index(int(m[1]), 1), month_index(int(m[1]), 12))),
]

_NOT_SCHEDULE = re.compile(  # a dated non-schedule event right before the date, or a debt date a word or two before
    r"(?:\b(?:acquired|bought|purchased|founded|dated|signed|announced|groundbreaking|broke ground|issued|priced|"
    r"closed|filed|since|as of)\W*$)|(?:\b(?:draws?|draw period|matur\w*|bullet|balloon|put date|installments?|"
    r"retired?|retirement)"
    r"(?:\s+\w+){0,2}\W*$)", re.I)
# a statement date opening a line: 'Jun-11-2026 call (DCD): ...', 'Apr-24-2026 Related Digital: ...'
_SAID_AFTER = re.compile(r"^\s*(?:[A-Za-z][\w.&'-]*\s*){0,3}(?:\([^()]{0,30}\))?\s*:", re.I)
_NOT_SCHEDULE_AFTER = re.compile(r"^\s*(?:revenue|guidance|results|budget|capex|fiscal|earnings)\b", re.I)
_TARGET_WORDS = re.compile(r"\b(if|by|est|projects?|target|due|complet|operational|online|deliver|rent|energiz|"
                           r"commenc|expected|ready|live|phase|hall|bldg|building)", re.I)
_FORMS = re.compile(r"\b(?:10-[kq]|8-k|20-f|s-1)\b", re.I)


def _enclosing_parens(text: str, start: int, end: int) -> tuple[int, int] | None:
    open_at = text.rfind("(", 0, start)
    if open_at < 0 or text.rfind(")", open_at, start) >= 0:
        return None
    close_at = text.find(")", end)
    if close_at < 0 or text.find("(", end, close_at) >= 0:
        return None
    return open_at, close_at


def _role(text: str, start: int, end: int) -> str:
    before = text[max(0, start - 30):start]
    if _NOT_SCHEDULE.search(before) or _NOT_SCHEDULE_AFTER.match(text[end:end + 20]):
        return "other"
    if _SAID_AFTER.match(text[end:end + 40]) and not re.search(r"\b(by|in|from|to|until)\s*$", before, re.I):
        return "said"
    parens = _enclosing_parens(text, start, end)
    if parens is not None:
        inner = text[parens[0] + 1:start] + text[end:parens[1]]
        rest = _FORMS.sub("", inner)
        if len(rest.strip(" ,;:-")) <= 25 and not re.search(r"\d", rest) and not _TARGET_WORDS.search(rest):
            return "said"
    return "target"


def find_dates(text: str | None) -> list[When]:
    """Every date expression in `text`, in text order, each with its role."""
    if not text:
        return []
    taken = [False] * len(text)
    found: list[When] = []
    for pattern, precision, fn in _PATTERNS:
        for m in pattern.finditer(text):
            if any(taken[m.start():m.end()]):
                continue
            window = fn(m)  # type: ignore[operator]
            if window is None:
                continue
            for i in range(m.start(), m.end()):
                taken[i] = True
            found.append(When(window[0], window[1], precision, m.start(), m.end(), m[0],
                              _role(text, m.start(), m.end())))
    found.sort(key=lambda w: w.start)
    return _merge_ranges(text, found)


def _merge_ranges(text: str, found: list[When]) -> list[When]:
    """'H2 2025 - early 2027', 'Q4 2026-Q1 2027', 'Dec-2028 to Mar-2029': one window from the first to the last."""
    out: list[When] = []
    for w in found:
        prev = out[-1] if out else None
        if (prev is not None and prev.role == w.role == "target" and w.lo >= prev.lo
                and re.fullmatch(r"\s*(?:-|\u2013|to|through|thru)\s*", text[prev.end:w.start], re.I)):
            out[-1] = replace(prev, hi=max(prev.hi, w.hi), end=w.end, text=text[prev.start:w.end],
                              precision="range")
            continue
        out.append(w)
    return out


def targets(text: str | None) -> list[When]:
    return [w for w in find_dates(text) if w.role == "target"]


def statement_dates(text: str | None) -> list[When]:
    return [w for w in find_dates(text) if w.role == "said"]


def refine(base: When, others: list[When]) -> When:
    """The most precise expression inside `base`'s window ('2028 (... Phase 1 completion Q3 2028)' -> Q3 2028)."""
    inside = [w for w in others if base.contains(w) and w.rank < base.rank]
    return min(inside, key=lambda w: (w.rank, w.start)) if inside else base


def leading(exprs: list[When]) -> When | None:
    """The first target in the text, refined by the more precise targets inside its window."""
    return refine(exprs[0], exprs) if exprs else None


def earliest(exprs: list[When]) -> When | None:
    if not exprs:
        return None
    first = min(exprs, key=lambda w: (w.lo, w.rank, w.start))
    return refine(first, exprs)


def latest(exprs: list[When]) -> When | None:
    if not exprs:
        return None
    last = max(exprs, key=lambda w: (w.hi, -w.rank, w.start))
    return refine(last, [w for w in exprs if w.hi == last.hi])


_CLAUSE_SPLIT = re.compile(r"[;()\n]|\s/\s")
_GRID = re.compile(r"energiz|interconnect|substation|in[- ]service|pipeline|\bgas\b|lateral|\besa\b|"
                   r"transmission|\bgrid\b|power", re.I)
_SHELL = re.compile(r"tdlr|shell|registered completion|building completion|topped out", re.I)
_RENT = re.compile(r"\brent\b|commenc", re.I)
_BUILDING = re.compile(r"\b(?:bldgs?|buildings?)\b", re.I)


def clause_of(text: str, when: When) -> str:
    starts = [m.end() for m in _CLAUSE_SPLIT.finditer(text, 0, when.start)]
    after = _CLAUSE_SPLIT.search(text, when.end)
    return text[(starts[-1] if starts else 0):(after.start() if after else len(text))]


def clause_kind(text: str, when: When) -> str:
    """'grid' | 'shell' | 'rent' | 'hall' for the clause holding `when`."""
    clause = clause_of(text, when)
    if _SHELL.search(clause) or (re.search(r"tdlr", text, re.I) and _BUILDING.search(clause)):
        return "shell"
    if _GRID.search(clause):
        return "grid"
    if _RENT.search(clause):
        return "rent"
    return "hall"
