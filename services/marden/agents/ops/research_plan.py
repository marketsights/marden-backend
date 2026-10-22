"""The researcher's queries: gaps grouped into EDGAR and eLibrary searches, their windows, and the check that a
file found is about the subject (`relevance`)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from marden.agents.ops import gaps as gaps_mod
from marden.agents.ops import research_store as store
from marden.agents.text import UnreadableDocument, normalize, read_text
from marden.core import db
from marden.core.documents import DocumentRow

LOOKBACK_DAYS = 365
OVERLAP_DAYS = 7
MIN_TEXT_CHARS = 500  # below this the text layer is too thin to say the phrase is absent (a scan)


@dataclass
class Query:
    endpoint: str  # 'edgar_fts' | 'ferc_elibrary'
    query: str
    phrase: str
    ciks: list[str]
    gaps: list[gaps_mod.Gap] = field(default_factory=list)

    @property
    def watched(self) -> bool:
        return any(g.subject.watched for g in self.gaps)

    @property
    def weight(self) -> int:
        return max(g.plan.weight for g in self.gaps)

    def subjects(self) -> list[dict[str, str]]:
        seen = {(g.subject.type, g.subject.id) for g in self.gaps}
        return [{"type": t, "id": i} for t, i in sorted(seen)]

    def items(self) -> list[str]:
        return sorted({g.item for g in self.gaps})


@dataclass
class Plan:
    edgar: list[Query]
    ferc: list[Query]
    collector_gaps: list[gaps_mod.Gap]
    not_searched: list[dict[str, Any]]


def plan(all_gaps: list[gaps_mod.Gap], last: dict[str, dict[str, Any]] | None = None) -> Plan:
    """Group the gaps into queries (one query serves every subject and item it covers) and order them."""
    edgar: dict[tuple[str, str], Query] = {}
    ferc: dict[str, Query] = {}
    collector, why_not, unnamed = [], {}, {}
    for g in all_gaps:
        p = g.plan
        if p.why_not:
            why_not.setdefault((g.item, p.why_not), set()).add(g.subject.id)
            continue
        if p.collector:
            collector.append(g)
            continue
        if not g.subject.phrases:
            unnamed.setdefault(g.item, set()).add(g.subject.id)
            continue
        for phrase in g.subject.phrases:
            ciks = g.subject.ciks if len(phrase.split()) == 1 else []
            for term in p.edgar:
                q = f'"{phrase}" {term}'
                edgar.setdefault((q, ",".join(ciks)), Query("edgar_fts", q, phrase, ciks)).gaps.append(g)
            if p.ferc and g.subject.us and len(phrase.split()) > 1:
                ferc.setdefault(phrase, Query("ferc_elibrary", phrase, phrase, [])).gaps.append(g)
    last = last or {}

    def order(qs: list[Query], endpoint: str) -> list[Query]:
        seen = last.get(endpoint) or {}
        return sorted(qs, key=lambda q: (not q.watched, seen.get(q.query) is not None,
                                         str(seen.get(q.query) or ""), -q.weight, q.query))

    not_searched = [{"item": item, "why": why, "subjects": sorted(s)} for (item, why), s in sorted(why_not.items())]
    not_searched += [{"item": item, "why": "no searchable name: a one-word name with no chain party's CIK, or none",
                      "subjects": sorted(s)} for item, s in sorted(unnamed.items())]
    return Plan(order(list(edgar.values()), "edgar_fts"), order(list(ferc.values()), "ferc_elibrary"), collector,
                not_searched)


def window(conn: db.Conn, q: Query, today: date) -> tuple[date, date]:
    end = store.last_window_end(conn, q.endpoint, q.query)
    since = end - timedelta(days=OVERLAP_DAYS) if end else today - timedelta(days=LOOKBACK_DAYS)
    return min(since, today), today


def tokens(text: str) -> str:
    """Words only, case- and accent-folded, padded: ' co vii llc ' (EDGAR matches phrases the same way,
    punctuation ignored: 'Co. VII, LLC' is the phrase 'Co. VII LLC'; 'Doña Ana' is 'Dona Ana')."""
    decomposed = unicodedata.normalize("NFKD", normalize(text))
    folded = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    return " " + " ".join(re.findall(r"[a-z0-9]+", folded)) + " "


def doc_text(doc: DocumentRow) -> str | None:
    """The stored file's folded text; None when the text layer is too thin to judge (a scan)."""
    try:
        text = read_text(doc.read(), doc.content_type, doc.url).text
    except UnreadableDocument:
        return None
    return tokens(text) if len(text) >= MIN_TEXT_CHARS else None


def relevance(text: str | None, q: Query, filer_cik: str | None) -> tuple[bool, str]:
    """Is this file about the query's subjects? It must name the phrase; a project name ('Project Jupiter')
    also needs the filer to be a chain party, or one of the subject's parties, county or SPVs named in the
    text (another company's 'Project Jupiter' is not ours). An exact legal name stands on its own."""
    if text is None:
        return True, "scanned or no text layer: queued unchecked (the extractor and linker decide)"
    if tokens(q.phrase) not in text:
        return False, f"its text does not name {q.phrase!r}"
    if gaps_mod.is_entity_name(q.phrase):
        return True, f"names {q.phrase!r} (an exact legal name)"
    if filer_cik and any(filer_cik in g.subject.ciks for g in q.gaps):
        return True, f"names {q.phrase!r}; filed by a chain party"
    context = [c for g in q.gaps for c in g.subject.context]
    hit = next((c for c in context if tokens(c) in text), None)
    if hit:
        return True, f"names {q.phrase!r} and {hit!r}"
    return False, (f"names {q.phrase!r} but none of the subject's parties, county or SPVs: likely another "
                   "project of the same name")
