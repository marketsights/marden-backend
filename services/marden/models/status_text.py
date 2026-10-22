"""Fixed text rules the status model applies to published statements (docs/methods/status-rules-v0.1.md).

Most chain signals in the fact store are still statements, not structured fields: a press headline, a
filing excerpt, a dossier note. These rules read them in code, with no model in the loop: each rule is a
pattern that must match inside one clause (text between ';' or '.'), with the guards below, and every
hit keeps the clause it matched so the chain link can show it.

- negation: 'no force majeure notice', 'cannot terminate', 'not an event of default' never match;
- modal / contractual wording: 'may terminate', 'right to terminate', 'except force majeure' never match
  (a right or a clause is not an event);
- procedure: a denied *motion*, *rehearing* or *complaint*, or a withdrawn *counsel*, is not a power
  denial (a denied rehearing request against an approved permit is good news for the project);
- withdrawal: a power clause whose only denial word is a withdrawal is rule 'power_withdrawn', which the
  status rules count only when no other permit, application or queue position is live (`status_power`);
- filing captions: an 8-K item caption ('Item 1.02 Termination of a Material Definitive Agreement') names
  a kind of filing, not a contract cut (with 'Entry into ...' it is the usual refinancing pair).

Ratings: `parse_rating` reads S&P / Fitch / DBRS / KBRA letter grades and Moody's grades onto one notch
scale (AAA = 1 ... BBB- = 10 ... D = 22) and returns the lowest grade stated for the party itself;
clauses about a bond, a backstop or a guarantor and text in brackets are not the party's rating.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SP_SCALE = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-", "B+", "B",
            "B-", "CCC+", "CCC", "CCC-", "CC", "C"]
MOODYS_SCALE = ["Aaa", "Aa1", "Aa2", "Aa3", "A1", "A2", "A3", "Baa1", "Baa2", "Baa3", "Ba1", "Ba2", "Ba3", "B1",
                "B2", "B3", "Caa1", "Caa2", "Caa3", "Ca", "C"]
NOTCH = {g: i + 1 for i, g in enumerate(SP_SCALE)}
NOTCH_MOODYS = {g: i + 1 for i, g in enumerate(MOODYS_SCALE)}
DEFAULT_NOTCH = 22
BBB_MINUS = NOTCH["BBB-"]

_CLAUSE_SPLIT = re.compile(r"(?<=[;.!?])\s+|\s+[\u2013\u2014]\s+|\s*;\s*|\n")  # en / em dash
_NEGATION = re.compile(r"\b(no|not|never|cannot|can't|without|nor|unlikely|denies (?:any|that))\b[^,;]{0,30}$", re.I)
_MODAL = re.compile(r"\b(may|might|could|would|can|right to|rights|option to|if|unless|except|whether|extended for)\b"
                    r"[^,;]{0,25}$", re.I)


def clauses(text: str | None) -> list[str]:
    if not text:
        return []
    return [c.strip() for c in _CLAUSE_SPLIT.split(text) if c and c.strip()]


@dataclass(frozen=True)
class Hit:
    rule: str
    clause: str


def _guarded(pattern: re.Pattern[str], clause: str, *, modal: bool = True) -> bool:
    for m in pattern.finditer(clause):
        before = clause[: m.start()]
        if _NEGATION.search(before):
            continue
        if modal and _MODAL.search(before):
            continue
        return True
    return False


# ---------------------------------------------------------------- rule patterns
FORCE_MAJEURE = re.compile(
    r"force majeure (?:notice|claim|declaration|letter)|notice of force majeure"
    r"|(?:invok|declar|fil|issu|sen[dt]|serv|claim|trigger|cit)\w*\s+(?:a\s+|the\s+|its\s+)?(?:\w+\s+)?force majeure"
    r"|force majeure\s+(?:was\s+|has been\s+|is\s+)?(?:invoked|declared|filed|claimed|issued|notice)",
    re.I,
)
CONTRACT_CUT = re.compile(
    r"\b(?:cut|cuts|cutting|reduc\w+|cancel\w*|terminat\w+|scal\w+ back|drop\w*|walk\w* away from|pull\w* out of"
    r"|exit\w*|back\w* out of|abandon\w*)\b[^;.]{0,40}\b(?:contract|lease|order|commitment|capacity|deal|agreement"
    r"|partnership|offtake)\b"
    r"|\b(?:contract|lease|order|commitment|deal|agreement|partnership)\b[^;.]{0,25}\b(?:was |were |has been |is )?"
    r"(?:cut|reduced|cancel+ed|terminated|ended|scrapped)\b"
    r"|\b(?:exit\w*|pull\w* out|walk\w* away|withdr\w+)\b[^;.]{0,20}\b(?:from|of)\b[^;.]{0,30}"
    r"\b(?:project|site|campus)\b",
    re.I,
)
FUNDING_NEED = re.compile(
    r"\b(?:cash burn|burn rate|negative free cash flow|free cash flow (?:deficit|shortfall)|funding gap"
    r"|cash deficit|needs? (?:to raise|new (?:funding|capital|money)|external (?:funding|financing))"
    r"|must raise|(?:seek|seeks|seeking|plans? to raise|raising) (?:new |additional |more )?"
    r"(?:funding|capital|equity|debt))\b",
    re.I,
)
COLLATERAL_CONTESTED = re.compile(
    r"\bpost\w*\s+(?:\w+\s+){0,3}collateral\b[^;.]{0,40}\b(?:contest\w*|disput\w*|demand\w*|call\w*)\b"
    r"|\b(?:contest\w*|disput\w*)\b[^;.]{0,40}\bcollateral\b",
    re.I,
)
POWER_OBJECT = re.compile(
    r"\b(?:right[- ]of[- ]way|ROW|pipeline|lateral|gas|power|interconnect\w*|PPA|power purchase|air permit"
    r"|permit|turbines?|fuel cells?|substation|transmission|generation|large[- ]load|electric\w*|energi[sz]\w*"
    r"|easement|CPCN|ESA|energy service)\b",
    re.I,
)
POWER_DENIAL = re.compile(
    r"\b(?:denied|denials?|denies|deny|rejected|rejects|rejections?|withdrawn|withdrew|withdraws|withdrawals?"
    r"|refused|refuses|revoked|vacated|cancel+ed)\b",
    re.I,
)
PROCEDURE = re.compile(
    r"\b(?:motions?|counsel|confidential|interven\w*|complaint|petitions?|rehearing|stay|stays|subpoena|data request"
    r"|hearing schedule|discovery|complete\w*|incomplete\w*|deficien\w*)\b",
    re.I,
)
VOLUNTARY = re.compile(r"\b(?:replac\w+|switch\w*|instead of|in favou?r of)\b", re.I)
HALTED = re.compile(
    r"\b(?:construction|work|building|buildout|build-out)\b[^;.]{0,25}\b(?:halt\w*|paus\w*|stopp\w*|suspend\w*"
    r"|froze|frozen|ceas\w*)\b"
    r"|\b(?:halt\w*|paus\w*|stopp\w*|suspend\w*|ceas\w*)\b[^;.]{0,15}\b(?:construction|work on|buildout|build-out)\b"
    r"|\bstop[- ]work order\b",
    re.I,
)
CONTINUING = re.compile(r"\b(?:continu\w*|resum\w*|restart\w*|despite|lifted|ended|ends)\b", re.I)
CANCELLED = re.compile(r"^\s*(?:cancel+ed|abandoned|terminated)\b", re.I)
DEFAULT_NOTICE = re.compile(
    r"\bnotice of default\b|\bdefault notice\b|\breservation of rights\b"
    r"|\b(?:issu\w*|sen[dt]|serv\w*|deliver\w*|receiv\w*)\s+(?:an?\s+|the\s+)?(?:notice of default|default notice)\b"
    r"|\bdeclar\w*\s+(?:an?\s+)?(?:event of )?default\b"
    r"|\bevent of default\s+(?:was |has been |is )?(?:declared|called|occurred|continuing)\b"
    r"|\baccelerat\w+\s+(?:of\s+)?(?:the\s+)?(?:loans?|debt|notes|facility|obligations)\b"
    r"|\bmissed\s+(?:an?\s+|its\s+)?(?:interest|coupon|principal|debt service)\s+payment\b|\bpayment default\b"
    r"|\b(?:is|was|went|are|were)\s+in default\b|\bdefaulted\b",
    re.I,
)
SYNDICATION_STALLED = re.compile(
    r"\bsyndicat\w*\b[^;.]{0,30}\b(?:stall\w*|struggl\w*|fail\w*|pulled|hung|halt\w*|shelved|postpon\w*)\b"
    r"|\b(?:stall\w*|failed|pulled|hung|shelved)\b[^;.]{0,15}\bsyndicat\w*\b"
    r"|\barrangers?\s+(?:are\s+|were\s+)?(?:holding|stuck with|left holding)\s+more\b",
    re.I,
)
DEFAULTED_STATUSES = frozenset({"default", "defaulted", "in default", "accelerated", "bankrupt", "chapter 11"})


def find(rule: str, pattern: re.Pattern[str], text: str | None, *, modal: bool = True) -> Hit | None:
    for clause in clauses(text):
        if _guarded(pattern, clause, modal=modal):
            return Hit(rule, clause)
    return None


def force_majeure(text: str | None) -> Hit | None:
    return find("force_majeure", FORCE_MAJEURE, text)


FILING_CAPTION = re.compile(
    r"\b(?:entry into|termination of|amendments? to)\s+(?:an?\s+)?material definitive agreements?\b", re.I
)


def contract_cut(text: str | None) -> Hit | None:
    return find("contract_cut", CONTRACT_CUT, FILING_CAPTION.sub(" ", text) if text else text)


def funding_need(text: str | None) -> Hit | None:
    return find("funding_need", FUNDING_NEED, text, modal=False)


def collateral_contested(text: str | None) -> Hit | None:
    return find("collateral_contested", COLLATERAL_CONTESTED, text)


def construction_halted(text: str | None) -> Hit | None:
    """Construction halted, paused or stopped - not 'work continuing despite a pause' or a pause lifted."""
    for clause in clauses(text):
        if not CONTINUING.search(clause) and _guarded(HALTED, clause):
            return Hit("construction_halted", clause)
    return None


def default_notice(text: str | None) -> Hit | None:
    return find("default_notice", DEFAULT_NOTICE, text)


def syndication_stalled(text: str | None) -> Hit | None:
    return find("syndication_stalled", SYNDICATION_STALLED, text, modal=False)


def power_denied(text: str | None) -> Hit | None:
    """A power source denied or withdrawn: a denial word and a power object in one clause, not a procedural
    ruling and not a voluntary switch of technology. Rule 'power_denied' when a clause denies, rejects,
    revokes or cancels; 'power_withdrawn' when the only such word is a withdrawal (status v0.4: a withdrawn
    permit or queue position counts only if nothing replaces it, `status_power.replacement`)."""
    withdrawn: Hit | None = None
    for clause in clauses(text):
        body = _strip_brackets(clause)  # side notes in brackets are about something else
        if PROCEDURE.search(body) or VOLUNTARY.search(body):
            continue
        objects = [_word_index(body, m.start()) for m in POWER_OBJECT.finditer(body)]
        for m in POWER_DENIAL.finditer(body):
            before = body[: m.start()]
            if _NEGATION.search(before) or _MODAL.search(before):
                continue
            at = _word_index(body, m.start())
            if any(abs(at - o) <= POWER_WINDOW_WORDS for o in objects):
                if not WITHDRAWAL.fullmatch(m.group(0)):
                    return Hit("power_denied", clause)
                withdrawn = withdrawn or Hit("power_withdrawn", clause)
    return withdrawn


POWER_WINDOW_WORDS = 8  # the denial word and the power object it denies sit within this many words
WITHDRAWAL = re.compile(r"withdrawn|withdrew|withdraws|withdrawals?", re.I)  # POWER_DENIAL's withdrawal words


def _word_index(text: str, pos: int) -> int:
    return len(text[:pos].split())


# ---------------------------------------------------------------- ratings
_LETTER = re.compile(r"(?<![A-Za-z0-9])(AAA|AA|A|BBB|BB|B|CCC|CC)([+-](?![A-Za-z]))?(?![A-Za-z0-9+])")
_MOODYS = re.compile(r"(?<![A-Za-z0-9])(Aaa|Aa[123]|A[123]|Baa[123]|Ba[123]|B[123]|Caa[123]|Ca)(?![A-Za-z0-9])")
_DEFAULTED = re.compile(r"(?<![A-Za-z0-9])(SD|RD|D)(?![A-Za-z0-9])")
_AGENCY = re.compile(r"(S&P|Fitch|DBRS|KBRA|Moody'?s)\s*:?\s*$", re.I)
_UNRATED = re.compile(r"\b(unrated|not rated|no (?:public |credit )?rating)\b", re.I)
_HIGH_YIELD = re.compile(
    r"\b(high[- ]yield|junk|sub-investment[- ]grade|non-investment[- ]grade|speculative[- ]grade)\b", re.I
)
HIGH_YIELD_NOTCH = NOTCH["BB+"]  # stated sub-investment grade without a letter grade: BB+ or lower
_OTHER_PARTY = re.compile(r"^\W*(?:bond|notes?|backstop|guarant\w*|deal|issue|tranche|class|sponsor|parent|vci|"
                          r"project|loan)\b", re.I)
_BRACKETS = re.compile(r"\([^()]*\)|\[[^\[\]]*\]")
_TO_GRADE = re.compile(r"\bto\s+$", re.I)
_REQUIREMENT = re.compile(
    r"\b(requir\w*|threshold|trigger\w*|minimum|covenant|tariff|must (?:be|have|maintain|keep))\b", re.I
)


@dataclass(frozen=True)
class Rating:
    grade: str | None  # the lowest grade stated, S&P style ('high yield' when only that is said); None = unrated
    notch: int | None
    clause: str

    @property
    def at_or_below_bbb_minus(self) -> bool:
        return self.notch is not None and self.notch >= BBB_MINUS


def _strip_brackets(text: str) -> str:
    while True:
        out = _BRACKETS.sub(" ", text)
        if out == text:
            return out
        text = out


def _grades(segment: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for m in _MOODYS.finditer(segment):
        found.append((NOTCH_MOODYS[m.group(1)], m.group(1)))
    for m in _LETTER.finditer(segment):
        grade = m.group(1) + (m.group(2) or "")
        if len(grade) == 1 and not _AGENCY.search(segment[: m.start()]):
            continue  # a bare 'A' / 'B' is a grade only right after an agency name
        if _MOODYS.match(segment, m.start()):
            continue
        if grade in NOTCH:
            found.append((NOTCH[grade], grade))
    for m in _DEFAULTED.finditer(segment):
        if _AGENCY.search(segment[: m.start()]):
            found.append((DEFAULT_NOTCH, m.group(1)))
    return found


def parse_rating(text: str | None, names: list[str] | None = None) -> Rating | None:
    """The party's own rating in a rating text ('Baa2 (neg) / BBB- (S&P, cut 2026-07-09) / BBB' -> BBB-,
    'Fluidstack unrated (Google backstop AA+/Aa2)' -> unrated). A downgrade headline ('S&P cut Oracle to
    BBB-') gives the grade after 'to'. With `names`, only clauses naming the party count. A grade a
    contract or tariff requires ('requiring A-/A3 rating') is not a rating. None when no rating is stated."""
    if not text:
        return None
    for clause in clauses(_strip_brackets(text)):
        if _OTHER_PARTY.search(clause) or _REQUIREMENT.search(clause):
            continue
        if names is not None and not mentions(clause, names):
            continue
        grades = _grades(clause)
        if grades:
            after_to = [g for g in _LETTER.finditer(clause) if _TO_GRADE.search(clause[: g.start()])]
            if after_to:
                grade = after_to[0].group(1) + (after_to[0].group(2) or "")
                if grade in NOTCH:
                    return Rating(grade, NOTCH[grade], clause)
            notch, _ = max(grades)
            return Rating(SP_SCALE[notch - 1] if notch <= len(SP_SCALE) else "D", notch, clause)
        if _HIGH_YIELD.search(clause):
            return Rating("high yield", HIGH_YIELD_NOTCH, clause)
        if _UNRATED.search(clause):
            return Rating(None, None, clause)
    return None


def mentions(text: str | None, names: list[str]) -> bool:
    """Whether the text names one of the party / site names (whole words, case-insensitive)."""
    if not text:
        return False
    return any(re.search(rf"(?<![A-Za-z]){re.escape(n)}(?![A-Za-z])", text, re.I) for n in names if n)
