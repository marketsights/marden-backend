"""The prose validator: model-written words may carry no figure of their own (spec 22 "Note writer": every
figure is filled in by code, not written by the model).

Code gives each figure a placeholder name and its text (`Figure`); the model writes a template that uses the
figures only as `{{name}}`. A template is rejected when, outside its placeholders, it holds

- a digit, or a number word ("three", "half", "twice", "dozen", "percent", ...);
- a placeholder code did not give;
- a word of direction or size ("rose", "fell", "sharply", ...): direction and size belong to the figures,
  whose texts code writes ("down 1.4 points on the week");
- a capitalized name that none of the figures, the lead or the allowed words contain (no outside facts).

After code fills the placeholders, every number in the text must be a number of one of the figures used
(`check_filled`, the last line of defence). The watchdog's rephrased titles go through the same checks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


RSQ = "\u2019"  # right single quotation mark (typographic apostrophe)
QUOTES = "'" + RSQ
PLACEHOLDER = re.compile(r"\{\{\s*([a-z][a-z0-9_]{0,40})\s*\}\}")
DIGITS = re.compile(r"\d+(?:[.,]\d+)*")
WORD = re.compile(f"[A-Za-z][A-Za-z{QUOTES}-]*")
NAME = re.compile(f"\\b[A-Z][A-Za-z0-9&{QUOTES}.-]*")
SENTENCE_START = re.compile("(?:^|[.!?:;]\\s+|\\(\\s*|[\"\u201c]\\s*)$")
NUMBER_WORDS = _words("""
zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen
seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred hundreds thousand
thousands million millions billion billions trillion dozen dozens half halves twice double doubled triple
tripled quadruple percent percentage pct second third fourth fifth sixth seventh eighth ninth tenth
""")
CLAIM_WORDS = _words("""
rose risen rise rising fell fallen fall falling up down higher lower highest lowest increase increased
increasing decrease decreased decreasing widen widened widening tighten tightened tightening improve improved
improving worsen worsened worsening jump jumped plunge plunged surge surged soar soared drop dropped
climb climbed gain gained sharply significantly dramatically steeply slightly modestly substantially
""")
ALLOWED_NAMES = frozenset({"Marden", "I"})


@dataclass(frozen=True)
class Figure:
    """One code-computed figure: its placeholder name, the text code wrote, what it means, and its record."""

    name: str
    text: str
    meaning: str
    ref: str | None = None


def number_set(text: str) -> set[Decimal]:
    """Every number written in digits ('1,300', '88.6', '2026-03' -> 2026 and 3), sign and $ / % ignored."""
    out: set[Decimal] = set()
    for m in DIGITS.finditer(text):
        raw = m.group(0)
        raw = raw.replace(",", "") if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", raw) else raw
        for part in re.split(r",", raw):
            try:
                out.add(Decimal(part).normalize())
            except InvalidOperation:
                continue
    return out


def _norm(word: str) -> str:
    """"Oracle's" -> 'oracle' (case, quotes and the possessive folded; typographic apostrophes too)."""
    w = word.strip(QUOTES + ".").lower()
    return re.sub(f"[{QUOTES}]s$", "", w)


def vocabulary(*texts: str) -> set[str]:
    words: set[str] = {_norm(n) for n in ALLOWED_NAMES}
    for t in texts:
        for w in WORD.findall(t or ""):
            words.add(_norm(w))
    return words


def outside(template: str) -> str:
    """The template's own words: placeholders blanked out."""
    return PLACEHOLDER.sub(" ", template)


def check_prose(template: str, figures: dict[str, Figure], known: set[str]) -> list[str]:
    """Problems with a model-written template (empty: it may be filled)."""
    problems: list[str] = []
    used = PLACEHOLDER.findall(template)
    unknown = sorted({n for n in used if n not in figures})
    if unknown:
        problems.append(f"unknown placeholders: {', '.join(unknown)}")
    if "{" in PLACEHOLDER.sub("", template) or "}" in PLACEHOLDER.sub("", template):
        problems.append("a brace outside a {{placeholder}}")
    own = outside(template)
    if DIGITS.search(own):
        problems.append(f"a number written in the prose: {DIGITS.search(own).group(0)!r}")  # type: ignore[union-attr]
    words = [_norm(w) for w in WORD.findall(own)]
    if hits := sorted({w for w in words if w in NUMBER_WORDS}):
        problems.append(f"number words in the prose: {', '.join(hits)}")
    if hits := sorted({w for w in words if w in CLAIM_WORDS}):
        problems.append(f"direction or size words in the prose (the figures carry them): {', '.join(hits)}")
    names = []
    for m in NAME.finditer(own):
        if SENTENCE_START.search(own[: m.start()]):
            continue
        token = m.group(0).rstrip("." + QUOTES)
        if token and _norm(token) not in known:
            names.append(token)
    if names:
        problems.append(f"names the figures do not hold: {', '.join(sorted(set(names)))}")
    return problems


def fill(template: str, figures: dict[str, Figure]) -> str:
    text = PLACEHOLDER.sub(lambda m: figures[m.group(1)].text, template)
    return re.sub(r"\s+", " ", text).strip()


def used_figures(template: str, figures: dict[str, Figure]) -> list[Figure]:
    seen: list[Figure] = []
    for name in PLACEHOLDER.findall(template):
        if name in figures and figures[name] not in seen:
            seen.append(figures[name])
    return seen


def check_filled(text: str, allowed: list[Figure], extra: tuple[str, ...] = ()) -> list[str]:
    """Every number in the filled text is a number of the figures used (or of `extra`: the lead, the title)."""
    pool: set[Decimal] = set()
    for f in allowed:
        pool |= number_set(f.text)
    for t in extra:
        pool |= number_set(t)
    stray = sorted(str(n) for n in number_set(text) - pool)
    return [f"numbers that are not filled figures: {', '.join(stray)}"] if stray else []
