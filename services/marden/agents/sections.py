"""Split long documents into sections with page references (the router picks the ones that matter).

Credit agreements, indentures, offering memoranda and 10-Qs run to hundreds of pages; one extraction call
reads one section of at most `max_chars` characters. Boundaries fall on headings where possible (ARTICLE,
Section 2.01, Item 1.01, Note 7, SCHEDULE, EXHIBIT), else on page or paragraph breaks. PDFs are split on
page boundaries, so a section can be sent as its own PDF (marden.agents.text.pdf_pages).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from marden.agents.text import FORM_FEED, DocText

HEADING = re.compile(
    r"^(?:ARTICLE\s+[IVXLC\d]+\b.*|Article\s+[IVXLC\d]+\b.*|SECTION\s+\d+(?:\.\d+)*\b.*|Section\s+\d+\.\d+\b.*"
    r"|Item\s+\d+(?:\.\d+)?[A-Z]?\.?\s.*|ITEM\s+\d+(?:\.\d+)?[A-Z]?\.?\s.*|PART\s+[IVX]+\b.*"
    r"|(?:NOTE|Note)\s+\d+\.?\s.*|SCHEDULE\s+[\dA-Z.\-]+\b.*|Schedule\s+[\dA-Z.\-]+\b.*|EXHIBIT\s+[A-Z\d.\-]+\b.*"
    r"|ANNEX\s+[A-Z\d]+\b.*)$"
)
MAX_TITLE = 120


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    start: int
    end: int
    first_page: int
    last_page: int

    @property
    def pages(self) -> str:
        return f"p{self.first_page}" if self.first_page == self.last_page else f"p{self.first_page}-{self.last_page}"

    def describe(self, doc: DocText, preview: int = 240) -> dict[str, str]:
        head = re.sub(r"\s+", " ", doc.text[self.start:self.start + preview]).strip()
        return {"id": self.id, "title": self.title, "pages": self.pages, "chars": str(self.end - self.start),
                "starts": head}


def _headings(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    pos = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip().replace(FORM_FEED, "")
        if 4 <= len(stripped) <= 200 and HEADING.match(stripped):
            out.append((pos + (len(line) - len(line.lstrip())), stripped[:MAX_TITLE]))
        pos += len(line)
    return out


def _cut_points(doc: DocText, max_chars: int) -> list[int]:
    """Section starts: headings first, then page / paragraph breaks, so no section exceeds max_chars."""
    text = doc.text
    heads = [p for p, _ in _headings(text)]
    page_starts = [s for s, _ in doc.pages]
    cuts = [0]
    while len(text) - cuts[-1] > max_chars:
        lo, hi = cuts[-1] + max_chars // 3, cuts[-1] + max_chars
        options = [p for p in heads if lo <= p <= hi] or [p for p in page_starts if lo <= p <= hi]
        if options:
            cut = options[-1]
        else:
            para = text.rfind("\n\n", lo, hi)
            cut = para + 2 if para > 0 else (text.rfind("\n", lo, hi) + 1 or hi)
            if cut <= cuts[-1]:
                cut = hi
        cuts.append(cut)
    return cuts


def split(doc: DocText, max_chars: int) -> list[Section]:
    """Sections covering the whole text, each at most max_chars (PDFs on page boundaries)."""
    if doc.kind == "pdf":
        return _split_pages(doc, max_chars)
    if len(doc.text) <= max_chars:
        return [Section("all", doc.title or "whole document", 0, len(doc.text), 1, doc.page_count)]
    cuts = _cut_points(doc, max_chars)
    titles = dict(_headings(doc.text))
    out = []
    for i, start in enumerate(cuts):
        end = cuts[i + 1] if i + 1 < len(cuts) else len(doc.text)
        title = titles.get(start) or re.sub(r"\s+", " ", doc.text[start:start + 80]).strip()
        out.append(Section(f"s{i + 1}", title, start, end, doc.page_of(start), doc.page_of(max(start, end - 1))))
    return out


def _split_pages(doc: DocText, max_chars: int) -> list[Section]:
    if len(doc.text) <= max_chars:
        return [Section("all", doc.title or "whole document", 0, len(doc.text), 1, doc.page_count)]
    out: list[Section] = []
    first = 1
    while first <= doc.page_count:
        last = first
        while last < doc.page_count and doc.pages[last][1] - doc.pages[first - 1][0] <= max_chars:
            last += 1
        start, end = doc.pages[first - 1][0], doc.pages[last - 1][1]
        title = re.sub(r"\s+", " ", doc.text[start:start + 80]).strip() or f"pages {first}-{last}"
        out.append(Section(f"p{first}-{last}" if first != last else f"p{first}", title, start, end, first, last))
        first = last + 1
    return out


def section_text(doc: DocText, section: Section) -> str:
    """The section's text with page markers ('[page 12]') so quotes can cite pages."""
    parts = []
    for page in range(section.first_page, section.last_page + 1):
        p_start, p_end = doc.pages[page - 1]
        lo, hi = max(p_start, section.start), min(p_end, section.end)
        if hi > lo:
            parts.append(f"[page {page}]\n{doc.text[lo:hi]}")
    return "\n\n".join(parts)
