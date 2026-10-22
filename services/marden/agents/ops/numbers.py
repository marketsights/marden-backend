"""The prose validator: model-written words may carry no figure of their own (spec 22 "Note writer": every
figure is filled in by code, not written by the model).

Code gives each figure a placeholder name and its text (`Figure`); the model writes a template that uses the
figures only as `{{name}}`. A template is rejected when, outside its placeholders, it holds

- a digit, any other numeral ("½", "Ⅲ", "²"), or a number word ("three", "half", "twice", "several", "percent",
  ...);
- a link, an address or markup ("https://", "www.", "@", "<", "[", backticks): the note names no source of its
  own; its records carry their references;
- more than MAX_OWN_WORDS words of its own;
- a placeholder code did not give;
- a word of direction or size ("rose", "fell", "sharply", ...): direction and size belong to the figures,
  whose texts code writes ("down 1.4 points on the week");
- a word of advice or forecast ("buy", "should", "will", "likely");
- a capitalized name that none of the figures, the lead or the allowed words contain (no outside facts); a
  capitalized word opening a sentence passes only if it is a common opening word ("The", "Its", "Both").

After code fills the placeholders, every number in the text must be a number of one of the figures used
(`check_filled`, the last line of defence). The watchdog's rephrased titles go through the same checks.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


RSQ = "\u2019"  # right single quotation mark (typographic apostrophe)
QUOTES = "'" + RSQ
PLACEHOLDER = re.compile(r"\{\{\s*([a-z][a-z0-9_]{0,40})\s*\}\}")
DIGITS = re.compile(r"\d+(?:[.,]\d+)*")
LETTER = r"[^\W\d_]"  # any letter, accented ones included ('Doña')
WORD = re.compile(f"{LETTER}(?:{LETTER}|[{QUOTES}-])*")
# A capitalized token: an ASCII capital, or any letter outside a-z ('Ørsted' must not pass as a lowercase word)
NAME = re.compile(f"(?<!\\w)[^\\W\\d_a-z][\\w&{QUOTES}.-]*")
LINKISH = re.compile(r"://|\bwww\.|@|[<>\[\]`]|\b[a-z0-9-]+\.(?:com|net|org|io|ai|co|gov|edu|us|uk)\b", re.I)
MAX_OWN_WORDS = 120  # the prompt asks for at most 80 words of the model's own per paragraph
SENTENCE_START = re.compile("(?:^|[.!?]\\s+|\\(\\s*|[\"\u201c]\\s*)$")
# Words that may open a sentence without being a name. Any other capitalized word (at a sentence start too:
# "Oracle sees ...", or after a colon or semicolon) must be in the figures, the lead or ALLOWED_NAMES.
COMMON_STARTS = _words("""
a an the this that these those it its their there here ranked behind records record lead main key another
other latest recent both each also and but while over under after before since from for of on in at with
by to as so yet then still plus further meanwhile separately elsewhere together alongside against besides
however overall next why how what which where when all most some not only even""")
NUMBER_WORDS = _words("""
zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen
seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred hundreds thousand
thousands million millions billion billions trillion dozen dozens half halves halved halving twice thrice once
double doubled doubles doubling triple tripled triples tripling quadruple twofold threefold tenfold percent
percentage pct first second third fourth fifth sixth seventh eighth ninth tenth several few couple single
multiple quarter quarters
""")
CLAIM_WORDS = _words("""
rose risen rise rises rising fell fallen fall falls falling up down high higher highest highly low lower lowest
increase increased increases increasing decrease decreased decreases decreasing decline declined declines
declining widen widened widens widening tighten tightened tightens tightening narrow narrowed narrows narrowing
improve improved improves improving worsen worsened worsens worsening worse worst better best deteriorate
deteriorated deteriorating weaken weakened weakening strengthen strengthened strengthening jump jumped jumps
jumping plunge plunged plunges plunging surge surged surges surging soar soared soars soaring drop dropped drops
dropping climb climbed climbs climbing gain gained gains gaining slip slipped slips slipping slide slid slides
sliding tumble tumbled spike spiked rally rallied slump slumped sink sank sinking grow grew grows growing shrink
shrank shrinking lose lost loses losing upgrade upgraded downgrade downgraded large larger largest small smaller
smallest big bigger biggest huge massive sizable sizeable steep sharp sharply significant significantly
dramatic dramatically steeply slight slightly modest modestly substantial substantially material materially
marginal marginally heavily
""")
# Advice and forecasts are not the note's to give (spec 22: the note states computed numbers and their records).
ADVICE_WORDS = _words("""
buy buys buying bought sell sells selling sold recommend recommends recommended recommendation advise advises
advised should must avoid will expect expects expected expecting likely unlikely forecast forecasts predict
predicts predicted soon
""")
ALLOWED_NAMES = frozenset({"Marden", "I"})


@dataclass(frozen=True)
class Figure:
    """One code-computed figure: its placeholder name, the text code wrote, what it means, and its record."""

    name: str
    text: str
    meaning: str
    ref: str | None = None


def numerals(text: str) -> list[str]:
    """Characters with a numeric value that are not ASCII digits ('½', 'Ⅲ', '²', '三'); DIGITS finds the rest."""
    return sorted({c for c in text if not c.isascii() and unicodedata.numeric(c, None) is not None})


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
    if odd := numerals(own):
        problems.append(f"a numeral written in the prose: {', '.join(odd)}")
    if link := LINKISH.search(own):
        problems.append(f"a link, address or markup in the prose: {link.group(0)!r}")
    words = [_norm(w) for w in WORD.findall(own)]
    if len(words) > MAX_OWN_WORDS:
        problems.append(f"{len(words)} words of its own (at most {MAX_OWN_WORDS})")
    if hits := sorted({w for w in words if w in NUMBER_WORDS}):
        problems.append(f"number words in the prose: {', '.join(hits)}")
    if hits := sorted({w for w in words if w in CLAIM_WORDS}):
        problems.append(f"direction or size words in the prose (the figures carry them): {', '.join(hits)}")
    if hits := sorted({w for w in words if w in ADVICE_WORDS}):
        problems.append(f"advice or forecast words in the prose: {', '.join(hits)}")
    names = []
    for m in NAME.finditer(own):
        token = m.group(0).rstrip("." + QUOTES)
        if not token or _norm(token) in known:
            continue
        if SENTENCE_START.search(own[: m.start()]) and _norm(token) in COMMON_STARTS:
            continue
        names.append(token)
    if names:
        problems.append(f"names the figures do not hold: {', '.join(sorted(set(names)))}")
    return problems


def repeated_words(template: str, figures: dict[str, Figure]) -> list[str]:
    """A label that repeats the first word of the figure it introduces ("Cushion: {{r2}}" where r2 reads
    "Cushion -2 months ...") would read twice once filled."""
    out, start = [], 0
    for m in PLACEHOLDER.finditer(template):
        before = WORD.findall(template[start:m.start()])
        start = m.end()
        fig = figures.get(m.group(1))
        first = WORD.findall(fig.text)[:1] if fig else []
        if before and first and _norm(before[-1]) == _norm(first[0]):
            out.append(f"the word before {{{{{m.group(1)}}}}} repeats its first word ({first[0]!r})")
    return out


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
    texts = [f.text for f in allowed] + list(extra)
    odd = [c for c in numerals(text) if not any(c in t for t in texts)]
    out = [f"numbers that are not filled figures: {', '.join(stray)}"] if stray else []
    if odd:
        out.append(f"numerals that are not filled figures: {', '.join(odd)}")
    return out
