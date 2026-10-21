"""Reading air permit documents (shared by the state adapters of `air_permits`).

Only what a document states in words is taken, with the sentence it came from:

- `pdf_text`: the text layer of a PDF (pymupdf); a scanned page has none and yields '' (nothing is guessed).
- `dates`: 'January 22, 2025', '1/22/2025', '01/22/2025' -> date.
- `labelled`: 'Label\\nValue' or 'Label: Value' header blocks of agency forms (TCEQ technical reviews).
- `unit_statements`: sentences or list items that name generating equipment, e.g. 'Reduce the count of Titan 350
  simple-cycle turbines from six to five and increase the site-rated power to 38 MW per turbine' or '90 turbines
  configured to operate in combined cycle mode'. Each keeps its verbatim quote; count, model and MW rating are
  filled only when the same words state them ('from six to five' -> 5; '38 MW per turbine' -> 38 each).
- `emission_totals`: pollutant / tons-per-year pairs from an emission summary, only when every pollutant has
  exactly one number.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

import pymupdf

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
LONG_DATE = re.compile(rf"\b({MONTHS})\s+(\d{{1,2}}),\s*(\d{{4}})\b")
SLASH_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
}
COUNT = r"(\d{1,4}|" + "|".join(NUMBER_WORDS) + r")"
EQUIPMENT = re.compile(
    r"\b(turbines?|engines?|generators?|gensets?|fuel cells?|energy servers?|server systems?|boilers?|"
    r"reciprocating|SOFC)\b", re.I,
)
MODEL = re.compile(  # specific models first; the OEM-only fallback is GENERIC_MODEL
    r"\b(Titan\s?\d{3}|Taurus\s?\d{2}|Mars\s?\d{3}|Centaur\s?\d{2}|TM\s?2500\w*|LM\s?\d{4}\w*|LMS\s?100\w*|"
    r"[67][HF]A?\.\d{2}\w*|Frame\s?\d{1,4}\w*|SGT\d?-\d{3,4}\w*|J\s?6\d\d\w*|Jenbacher\s+J?\d{3}\w*|"
    r"C\s?175\w*|C\s?32\w*|(?:Cat(?:erpillar)?\s+)?G?3[56]\d\d\w*|W(?:aertsila|ärtsilä)\s*\d{2}\w*|M501\w*|"
    r"Aero\s?DT\d+|NovaLT\s?\d+|Bloom Energy Server\w*|Energy Server System)\b",
    re.I,
)
GENERIC_MODEL = re.compile(r"\b(Siemens Energy|Mitsubishi Power|Bloom Energy|Solar Turbines|Caterpillar|Jenbacher)\b", re.I)
NOISE = re.compile(r"[§*$]|\bTAC\b")
OEMS = (("Solar", r"\b(Titan|Taurus|Mars|Centaur|Solar Turbines)\b"), ("GE Vernova", r"\b(GE|LM\s?\d{4}|LMS|TM\s?2500|[67][HF]A?\.)"),
        ("Siemens Energy", r"\b(Siemens|SGT)"), ("INNIO Jenbacher", r"\b(Jenbacher|J\s?6\d\d)"),
        ("Caterpillar", r"\b(Caterpillar|Cat\s|C\s?175|C\s?32|3516|3520)"), ("Bloom Energy", r"\bBloom\b"),
        ("Wärtsilä", r"W(?:aertsila|ärtsilä)"), ("Mitsubishi", r"\b(Mitsubishi|M501)"), ("Baker Hughes", r"\bNovaLT"))
MW = re.compile(r"(\d{1,4}(?:\.\d+)?)\s*(?:MW|megawatts?)\b(\s*(?:per|each|/)\s*(?:turbine|engine|unit|generator))?", re.I)
FROM_TO = re.compile(rf"\bfrom\s+{COUNT}\s+to\s+{COUNT}\b", re.I)
LEAD_EQUIPMENT = re.compile(  # the counted thing is equipment: '90 turbines', 'six emergency generator engines'
    r"(?!hours|MW|tons|percent|days)(\S+\s+){0,7}?(combustion\s+)?(turbines?|engines?|generators?|gensets?|fuel cells?|"
    r"boilers?|energy servers?)\b(?!\s+configurations?)", re.I,
)
LEADING_COUNT = re.compile(rf"^\s*(?:\(?{COUNT}\)?\s*(?:\({COUNT}\))?)\s+(?P<rest>.+)$", re.I)
POLLUTANTS = ("PM2.5", "PM10", "PM", "VOC", "NOx", "NOX", "CO2e", "CO", "SO2", "H2SO4", "H2S", "NH3", "HAPs", "HAP", "Pb")


def pdf_text(data: bytes) -> str:
    """The PDF's text layer, pages joined; '' for a scan without one or a file that is not a PDF."""
    try:
        with pymupdf.open(stream=data, filetype="pdf") as pdf:
            return "\n".join(page.get_text() for page in pdf)
    except Exception:
        return ""


DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", " ": " ",
                        "•": " ", "●": " ", "": " "})
# TCEQ technical reviews list changes as bullets whose glyph lands mid-line in the text layer, so a list item
# starts at its verb instead.
ITEM_START = re.compile(r"\s(?=(?:Reduce|Increase|Add|Remove|Replace|Install|Construct|Modify|Authorize|Decrease)\s)")


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text.translate(DASHES)).strip()


def to_date(text: str | None) -> date | None:
    if not text:
        return None
    if m := LONG_DATE.search(text):
        return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").date()
    if m := SLASH_DATE.search(text):
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


def labelled(text: str, labels: list[str]) -> dict[str, str]:
    """The value after each label in a form header: 'Label:\\nValue', 'Label\\nValue' or 'Label: Value'. A label
    appearing twice keeps its first value. Values stop at the next known label."""
    lines = [ln.strip() for ln in text.splitlines()]
    out: dict[str, str] = {}
    label_set = {lab.lower().rstrip(":") for lab in labels}
    for i, line in enumerate(lines):
        head = line.rstrip(":").strip()
        key = next((lab for lab in labels if head.lower() == lab.lower().rstrip(":")), None)
        inline = None
        if key is None:
            for lab in labels:
                if line.lower().startswith(lab.lower().rstrip(":") + ":"):
                    key, inline = lab, line[len(lab.rstrip(":")) + 1:].strip()
                    break
        if key is None or key in out:
            continue
        if inline:
            out[key] = inline
            continue
        parts = []
        for nxt in lines[i + 1:i + 6]:
            if not nxt:
                if parts:
                    break
                continue
            if nxt.lower().rstrip(":") in label_set:
                break
            parts.append(nxt)
            if not nxt.endswith(","):
                break
        if parts:
            out[key] = " ".join(parts)
    return out


def count_value(word: str | None) -> int | None:
    if word is None:
        return None
    w = word.lower()
    return int(w) if w.isdigit() else NUMBER_WORDS.get(w)


def oem_of(text: str) -> str | None:
    return next((name for name, pattern in OEMS if re.search(pattern, text)), None)


def _clauses(text: str) -> list[str]:
    """Sentences, bullets and list items of a description ('Sources include 90 turbines ..., 3 turbines ...,
    six emergency generator engines' gives the sentence and each item)."""
    out = []
    for sentence in re.split(r"(?<=[.;:])\s+(?=[A-Z(])", squash(text)):
        for item in ITEM_START.split(sentence):
            out.append(item.strip())
            if item.count(", ") >= 2:
                out += [p.strip() for p in re.split(r",\s+(?:and\s+)?|;\s+", item) if p.strip()]
    return [s for s in out if s]


def _per_unit(clause: str, mw: re.Match[str]) -> bool:
    """'38 MW per turbine', '45 megawatts (MW) each', '2 MW diesel generators' state a rating per unit."""
    if mw.group(2) or re.search(r"\b(each|per (turbine|engine|unit|generator))\b", clause, re.I):
        return True
    after = clause[mw.end():mw.end() + 60]
    return bool(re.match(r"\s*(\(MW\)\s*)?(\S+\s+){0,2}?(turbines?|engines?|generators?|gensets?|fuel cells?)\b", after, re.I))


def unit_statements(text: str, *, limit: int = 12) -> list[dict[str, Any]]:
    """Statements that name generating equipment, each with count / model / MW only where stated."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for clause in _clauses(text):
        if not EQUIPMENT.search(clause) or len(clause) > 600 or clause in seen or NOISE.search(clause):
            continue
        model = MODEL.search(clause) or GENERIC_MODEL.search(clause)
        mw = MW.search(clause)
        count = None
        if (m := FROM_TO.search(clause)) and EQUIPMENT.search(clause[:m.start()]):
            count = count_value(m.group(2))
        elif m := LEADING_COUNT.match(re.sub(r"^(?:Sources include|consisting of|including|and|add)\s+", "", clause, flags=re.I)):
            if LEAD_EQUIPMENT.match(m.group("rest")):
                count = count_value(m.group(2) or m.group(1))
        if count is None and model and (m := re.search(rf"(?:^|(?<=\s)){COUNT}\s+(?:\(\d+\)\s+)?(?:\S+\s+){{0,3}}$",
                                                       clause[:model.start()], re.I)):
            count = count_value(m.group(1))  # '90 Siemens Energy SGT-800 combustion turbines'
        if not (model or mw or count):
            continue
        seen.add(clause)
        each = bool(mw and _per_unit(clause, mw))
        out.append({
            "quote": clause,
            "count": count,
            "model": squash(model.group(0)) if model else None,
            "oem": oem_of(clause),
            "rating_mw_each": float(mw.group(1)) if mw and each else None,
            "rating_mw": float(mw.group(1)) if mw and not each else None,
        })
    kept = _fold(out)
    kept.sort(key=lambda u: (u["model"] is None, u["count"] is None, kept.index(u)))  # stated model and count first
    return kept[:limit]


def _fold(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A long list sentence is dropped in favour of its items; an item already said by a kept sentence (same
    count, model, MW) is dropped in favour of the sentence."""
    keys = ("count", "model", "rating_mw_each", "rating_mw")

    def inside(b: dict[str, Any], a: dict[str, Any]) -> bool:
        return b is not a and b["quote"] != a["quote"] and b["quote"] in a["quote"]

    drop = {i for i, a in enumerate(items) if len(a["quote"]) > 250 and any(inside(b, a) for b in items)}
    for i, a in enumerate(items):
        if i in drop:
            continue
        for j, b in enumerate(items):
            if j not in drop and inside(b, a) and all(b[k] is None or a[k] == b[k] for k in keys):
                drop.add(j)
    return [x for i, x in enumerate(items) if i not in drop]


def emission_totals(text: str, start: str, *, unit: str = "tpy") -> list[dict[str, Any]]:
    """'<start> PM 1,334.24 PM10 1,281.63 ...' -> [{pollutant, value, unit, quote}] when the pairs are clean."""
    flat = squash(text)
    i = flat.find(start)
    if i < 0:
        return []
    block = flat[i + len(start):i + len(start) + 900]
    names = "|".join(re.escape(p) for p in POLLUTANTS)
    pairs = re.findall(rf"\b({names})\s+(<\s*)?([\d,]+(?:\.\d+)?)\b", block)
    out: list[dict[str, Any]] = []
    seen = set()
    for name, less, value in pairs:
        if name in seen:
            break
        seen.add(name)
        out.append({"pollutant": name, "value": float(value.replace(",", "")), "unit": unit,
                    "less_than": bool(less), "quote": f"{name} {less or ''}{value}".replace("  ", " ")})
    return out if len(out) >= 3 else []
