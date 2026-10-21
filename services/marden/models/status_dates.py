"""In-service dates stated in text, and how far a later statement moved them (status rule D1).

Statements in the fact store give delivery / energization targets as written: 'mid-2026', 'Q4 2026',
'second half of calendar year 2026', 'Feb-1-2027', '9/30/2026', '2028'. `first_period` reads the first
target of a statement as a month interval; `slip_months` compares two of them.

Fixed conventions (docs/methods/status-rules-v0.1.md):
- A period counts as its last month (00-global: a date known only to the year is Dec 31): Q4 2026 ->
  2026-12, H1 2027 -> 2027-06, mid-2026 -> 2026-06, early 2027 -> 2027-03, late 2026 -> 2026-12.
- A later statement that is coarser and still contains the earlier date is not a slip (precision lost,
  not time): 'middle of 2027' -> 'calendar year 2027' moves nothing.
- Statement dates are not targets: text in brackets, dates in a short 'who / when:' label at the start
  ('Jun-11-2026 call (DCD): ...', 'Apr-24-2026 Related Digital: ...'), and dates after 'as of', 'on',
  'since' are skipped. A clause naming an outside estimate
  (Epoch, Aterio, satellite tracking) ends the statement: only the parties' own dates count.
- A statement that says the schedule did not change ('no change reported', 'no slippage', 'no revised
  date') carries no new target, even when it repeats a date.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
    "nov": 11, "dec": 12,
}
_MON = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?"
        r"|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
_Y = r"(20[2-4]\d)"
_YY = r"(20[2-4]\d|\d\d)"
_CAL = r"(?:\s*(?:calendar(?:\s+year)?|cy|fy)\s*)?"
_ORD = {"first": 1, "second": 2, "third": 3, "fourth": 4, "1": 1, "2": 2, "3": 3, "4": 4}

# (kind, pattern); tried at each position, first match wins. Groups are read by `_period`.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ymd", re.compile(rf"\b{_Y}-(0[1-9]|1[0-2])-([0-3]\d)\b")),
    ("mdy_slash", re.compile(r"\b(1[0-2]|0?[1-9])/([0-3]?\d)/(20[2-4]\d|\d\d)\b")),
    ("mon_d_y", re.compile(rf"\b({_MON})[ \-]+([0-3]?\d)(?:st|nd|rd|th)?,?[ \-]+{_Y}\b", re.I)),
    ("d_mon_y", re.compile(rf"\b([0-3]?\d)[ \-]({_MON})[ \-,]+{_Y}\b", re.I)),
    ("yq", re.compile(rf"\b{_Y}-Q([1-4])\b", re.I)),
    ("q", re.compile(rf"\b(?:Q([1-4])|([1-4])Q){_CAL}[ \-]?{_YY}\b", re.I)),
    ("q_words", re.compile(rf"\b(first|second|third|fourth) quarter (?:of )?{_CAL}{_Y}\b", re.I)),
    ("h", re.compile(rf"\b(?:([12])H|H([12])){_CAL}[ \-]?{_YY}\b", re.I)),
    ("h_words", re.compile(rf"\b(first|second) half (?:of )?(?:the )?{_CAL}{_Y}\b", re.I)),
    ("fuzzy", re.compile(rf"\b(early|mid|middle of|late|end of(?: the)?|end|year-end|ye)[ \-]?(?:of )?{_CAL}{_Y}\b",
                         re.I)),
    ("y_fuzzy", re.compile(rf"\b{_Y}-(mid|end)\b", re.I)),
    ("mon_y", re.compile(rf"\b({_MON})[ .,\-]*(?:of )?(20[2-4]\d|'\d\d)\b", re.I)),
    ("ym", re.compile(rf"\b{_Y}-(0[1-9]|1[0-2])\b(?!-\d)")),
    ("y", re.compile(r"(?<![\d\-/.$,])(20[2-4]\d)(?!\d|\.\d)")),
]
_ANY = re.compile("|".join(f"(?:{p.pattern})" for _, p in _PATTERNS), re.I)
_BRACKETS = re.compile(r"\([^()]*\)|\[[^\[\]]*\]")
_ESTIMATE = re.compile(r"\b(epoch|aterio|satellite|cleanview|semianalysis|analysts?|baxtel)\b", re.I)
_NO_CHANGE = re.compile(r"\b(no change|no slippage|no revised date)\b", re.I)
_SKIP_BEFORE = re.compile(r"\b(as of|on|since|dated|filed|announced)\s*$", re.I)
LABEL_CHARS = 40  # a ':' this close to the start ends a 'who / when' label, not a target


@dataclass(frozen=True)
class Period:
    """A stated target as a month interval (months counted as year * 12 + month - 1)."""

    start: int
    end: int
    text: str

    @property
    def month(self) -> str:
        """The month the period counts as (its last month), 'YYYY-MM'."""
        return f"{self.end // 12:04d}-{self.end % 12 + 1:02d}"


def _m(year: int, month: int) -> int:
    return year * 12 + month - 1


def _year(text: str) -> int:
    text = text.lstrip("'")
    return int(text) if len(text) == 4 else 2000 + int(text)


def _period(kind: str, g: tuple[str, ...], text: str) -> Period | None:
    g = tuple(x for x in g if x is not None)
    if kind == "ymd":
        y, mo = int(g[0]), int(g[1])
        return Period(_m(y, mo), _m(y, mo), text)
    if kind == "mdy_slash":
        y, mo = _year(g[2]), int(g[0])
        return Period(_m(y, mo), _m(y, mo), text)
    if kind in ("mon_d_y", "d_mon_y"):
        mon = g[0] if kind == "mon_d_y" else g[1]
        y, mo = int(g[2]), MONTHS[mon[:3].lower()]
        return Period(_m(y, mo), _m(y, mo), text)
    if kind == "mon_y":
        y, mo = _year(g[1]), MONTHS[g[0][:3].lower()]
        return Period(_m(y, mo), _m(y, mo), text)
    if kind in ("yq", "q", "q_words"):
        if kind == "yq":
            y, q = int(g[0]), int(g[1])
        else:
            q, y = _ORD[g[0].lower()], _year(g[1])
        return Period(_m(y, 3 * q - 2), _m(y, 3 * q), text)
    if kind in ("h", "h_words"):
        h, y = _ORD[g[0].lower()], _year(g[1])
        return Period(_m(y, 6 * h - 5), _m(y, 6 * h), text)
    if kind in ("fuzzy", "y_fuzzy"):
        word, y = (g[0].lower(), int(g[1])) if kind == "fuzzy" else (g[1].lower(), int(g[0]))
        if word == "early":
            return Period(_m(y, 1), _m(y, 3), text)
        if word in ("mid", "middle of"):
            return Period(_m(y, 6), _m(y, 6), text)
        return Period(_m(y, 10), _m(y, 12), text)  # late, end (of), year-end, YE
    if kind == "ym":
        y, mo = int(g[0]), int(g[1])
        return Period(_m(y, mo), _m(y, mo), text)
    if kind == "y":
        y = int(g[0])
        return Period(_m(y, 1), _m(y, 12), text)
    return None


def _match_at(text: str, pos: int) -> tuple[str, re.Match[str]] | None:
    for kind, pattern in _PATTERNS:
        m = pattern.match(text, pos)
        if m:
            return kind, m
    return None


def clean(text: str) -> str:
    """The part of a statement that can carry the parties' own target dates."""
    out = text
    while True:
        stripped = _BRACKETS.sub(" ", out)
        if stripped == out:
            break
        out = stripped
    estimate = _ESTIMATE.search(out)
    if estimate:
        out = out[: estimate.start()]
    return out


def periods(text: str | None) -> list[Period]:
    """Every target period stated in `text`, in order, statement dates skipped."""
    if not text:
        return []
    body = clean(text)
    found: list[Period] = []
    colon = body.find(":")
    pos = colon + 1 if 0 <= colon <= LABEL_CHARS else 0
    while True:
        hit = _ANY.search(body, pos)
        if not hit:
            return found
        start = hit.start()
        matched = _match_at(body, start)
        if matched is None:
            pos = start + 1
            continue
        kind, m = matched
        pos = m.end()
        if _SKIP_BEFORE.search(body[:start]):
            continue
        period = _period(kind, m.groups(), m.group(0))
        if period is not None:
            found.append(period)


def first_period(text: str | None) -> Period | None:
    """The first target a statement gives; None when it gives none or says nothing changed."""
    if text and _NO_CHANGE.search(text):
        return None
    found = periods(text)
    return found[0] if found else None


def slip_months(before: Period, after: Period) -> int:
    """Months the later statement moved the target (negative = earlier). A coarser later period that
    contains the earlier date moves nothing."""
    coarser = (after.end - after.start) > (before.end - before.start)
    if coarser and after.start <= before.end <= after.end:
        return 0
    return after.end - before.end


_EXPLICIT = re.compile(
    r"\b(?:delay\w*|slip\w*|push\w*(?: back)?|postpon\w*|deferr?\w*)\b[^.;]{0,30}?~?(\d{1,2})"
    r"(?:\s*(?:-|to|\u2013)\s*\d{1,2})?\s*months?\b",
    re.I,
)
_FROM_TO = re.compile(r"\bfrom\s+(.{3,30}?)\s+to\s+(.{3,30}?)(?:[;,.)]|\s+after\b|$)", re.I)


def stated_slip(text: str | None) -> int | None:
    """A slip the text states itself: 'delayed ~6 months', 'from Sep-2026 to Feb-1-2027'."""
    if not text:
        return None
    m = _EXPLICIT.search(text)
    if m:
        return int(m.group(1))
    m = _FROM_TO.search(text)
    if m:
        before, after = first_period(m.group(1)), first_period(m.group(2))
        if before and after:
            return slip_months(before, after)
    return None
