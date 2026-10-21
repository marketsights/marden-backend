"""Grid-region review items resolved from primary documents (grid_evidence_list.EVIDENCE).

grid_join opens a review item when boundary files disagree at a subject's location (overlapping ISO/RTO or
balancing-authority polygons, interconnection seams). A person settles it from the subject's own documents:
a filing, a commission record or an operator publication that says which ISO/RTO serves the facility. This
collector reads those statements, checks each against the document and writes the conclusion as facts.

Per statement (raw first): the document is fetched once through the Fetcher (declared User-Agent, per-host
limits; SEC hosts share the 5 req/s bucket), stored in the raw store with a `document` row, and its text must
contain the quote verbatim (whitespace ignored); a quote that is not in the document stops the run.

Facts (producer 'collector:grid_evidence', published, source = the document, valid_from = its filing date,
quote = the statement's words, quote_loc = where it is and how the value follows):
- a stated value (`iso`, `utility`, ...) is tag F. A stated `utility` must be one of the subject's HIFLD
  territories (grid_join `utility_territories`), so the value keeps HIFLD's spelling; otherwise it is not written;
- from a stated ISO/RTO: the balancing authority is the ISO's only EIA-930 balancing authority (ERCOT -> ERCO,
  MISO -> MISO; tag F), or, when the ISO has several (SPP: SWPP, SWPW), the one of them whose HIFLD Control
  Areas boundary contains the subject in grid_join's facts (tag R: read off a territory map); the
  interconnection is that balancing authority's (EIA-930), with the same tag;
- two statements that disagree on a subject's field, or a derivation with no single answer, write nothing for
  that field: the evidence is added to the open review item (`payload.evidence`) and the item stays open.

Then each concluded field's grid_join candidates are decided (decided_by = the producer): the candidate with
the concluded value is published, every other one is rejected (reason in audit_log), which closes the review
item; `payload.resolution` records the value, tag and evidence fact. Reruns write nothing new. Runs daily
after grid_join (a new boundary version opens new candidates) and on demand:
`python -m marden collect grid_evidence`.
"""

from __future__ import annotations

import argparse
import html
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any

import pymupdf

from marden import cli
from marden.collectors import grid_join
from marden.collectors.grid_evidence_list import EVIDENCE, Doc, Statement
from marden.collectors.rates_common import ParseError, RunStats, collect, print_stats
from marden.collectors.sec_index import TERMS_NOTE as SEC_TERMS
from marden.core import db, documents, http
from marden.core.db import Conn, Row
from marden.core.facts import FactWriter
from marden.core.jsonutil import jsonb
from marden.core.queue import app
from marden.core.sources import SourceSpec, declare_source

SOURCE_ID = "grid_evidence"
PRODUCER = "collector:grid_evidence"
CRON_UTC = "57 10 * * *"  # daily 10:57 UTC, ten minutes after grid_join
GRID_FIELDS = ("interconnection", "iso", "balancing_authority")
TERMS_NOTE = (
    "Primary documents a person selected (grid_evidence_list.EVIDENCE), each fetched once and kept in the raw "
    "store; no crawling. Every listed document is an SEC EDGAR filing. " + SEC_TERMS
)
SOURCE = declare_source(SOURCE_ID, "Grid-region evidence from primary documents", "manual",
                        "https://www.sec.gov/Archives/edgar/data/", 24 * 60, TERMS_NOTE)


@dataclass(frozen=True)
class Conclusion:
    field: str
    value: str
    tag: str  # F stated or following from a stated ISO by EIA-930; R read off a territory map
    why: str  # appended to the statement's location in quote_loc


@dataclass
class Finding:
    """What one statement says about one subject, after derivation."""
    statement: Statement
    document: documents.DocumentRow
    conclusions: list[Conclusion]
    unresolved: dict[str, str]  # field -> why no value follows


# ---------------------------------------------------------------- documents and quotes
_SPACE = re.compile(r"\s+")


def squash(text: str) -> str:
    """NFKC, every Unicode space as one ASCII space."""
    return _SPACE.sub(" ", unicodedata.normalize("NFKC", text)).strip()


def document_text(data: bytes, content_type: str | None) -> str:
    """The readable text of an HTML or PDF document (tags dropped, entities decoded)."""
    if (content_type or "").startswith("application/pdf") or data[:5] == b"%PDF-":
        with pymupdf.open(stream=data, filetype="pdf") as pdf:
            return squash(" ".join(page.get_text() for page in pdf))
    text = data.decode("utf-8", errors="replace")
    text = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return squash(html.unescape(text))


def has_quote(text: str, quote: str) -> bool:
    """The quote occurs in the text, ignoring whitespace (tags split words in filings' HTML)."""
    def key(s: str) -> str:
        return _SPACE.sub("", unicodedata.normalize("NFKC", s))
    return bool(quote.strip()) and key(quote) in key(text)


def stored_document(doc: Doc, conn: Conn, fetcher: http.Fetcher | None = None) -> documents.DocumentRow:
    """The stored copy of a listed document (filings do not change), fetched and stored the first time."""
    row = documents.latest_document(SOURCE_ID, doc.url, conn=conn)
    if row is not None:
        return row
    return http.fetch_document(SOURCE_ID, doc.url, doc_type=doc.doc_type, title=doc.title, doc_date=doc.doc_date,
                               meta={"source_name": doc.source_name}, fetcher=fetcher, conn=conn)


def checked_document(doc: Doc, quotes: list[str], conn: Conn) -> documents.DocumentRow:
    row = stored_document(doc, conn)
    text = document_text(row.read(), row.content_type)
    missing = [q for q in quotes if not has_quote(text, q)]
    if missing:
        raise ParseError(f"{doc.url} (document {row.id}) does not contain the quote {missing[0][:80]!r}")
    return row


# ---------------------------------------------------------------- derivation
def grid_join_facts(conn: Conn, subject_type: str, subject_id: str, field: str) -> list[Row]:
    return conn.execute(
        """SELECT * FROM fact WHERE subject_type = %s AND subject_id = %s AND field = %s AND producer = %s
             AND status IN ('candidate', 'published') ORDER BY id""",
        (subject_type, subject_id, field, grid_join.PRODUCER),
    ).fetchall()


def _region(conn: Conn, region_id: str | None) -> Row | None:
    if not region_id:
        return None
    return conn.execute("SELECT id, name, parent_id, meta FROM grid_region WHERE id = %s", (region_id,)).fetchone()


def _label(row: Row) -> str:
    return (row["meta"] or {}).get("label") or row["name"]


def _iso_region(conn: Conn, label: str) -> Row | None:
    return conn.execute(
        "SELECT id, name, parent_id, meta FROM grid_region WHERE kind = 'iso_rto' AND meta ->> 'label' = %s",
        (label,),
    ).fetchone()


def from_iso(conn: Conn, subject: tuple[str, str], iso: str) -> tuple[list[Conclusion], dict[str, str]]:
    """The stated ISO/RTO, and the balancing authority and interconnection that follow (module rules)."""
    out = [Conclusion("iso", iso, "F", f"states {iso}")]
    region = _iso_region(conn, iso)
    if region is None:
        why = f"{iso} is not an ISO/RTO in grid_region"
        return out, {"balancing_authority": why, "interconnection": why}
    codes = list((region["meta"] or {}).get("ba_codes") or [])
    if len(codes) == 1:
        code, tag = codes[0], "F"
        how = f"{iso}'s only balancing authority per EIA-930 is {code}"
    else:
        hits = [r for r in grid_join_facts(conn, *subject, "balancing_authority") if r["value"] in codes]
        if len({r["value"] for r in hits}) != 1:
            why = (f"{iso} has balancing authorities {', '.join(codes)} (EIA-930) and "
                   f"{'none' if not hits else 'several'} of their HIFLD Control Areas boundaries contain the subject")
            return out, {"balancing_authority": why, "interconnection": why}
        code, tag = hits[0]["value"], "R"
        how = (f"of {iso}'s balancing authorities ({', '.join(codes)}, EIA-930) only {code}'s HIFLD Control Areas "
               f"boundary contains the subject (grid_join fact {hits[0]['id']}, document {hits[0]['document_id']})")
    out.append(Conclusion("balancing_authority", code, tag, how))
    inter = _region(conn, (_region(conn, f"ba:{code}") or {}).get("parent_id") or region["parent_id"])
    if inter is None:
        return out, {"interconnection": f"no interconnection is recorded for balancing authority {code}"}
    out.append(Conclusion("interconnection", _label(inter), tag, f"{how}; {code} is in the {inter['name']} "
                                                                   "(EIA-930)"))
    return out, {}


def from_utility(conn: Conn, subject: tuple[str, str], utility: str) -> tuple[list[Conclusion], dict[str, str]]:
    terr = grid_join_facts(conn, *subject, "utility_territories")
    names = terr[0]["value"] if terr and isinstance(terr[0]["value"], list) else []
    if utility not in names:
        return [], {"utility": f"{utility} is not among the subject's HIFLD retail territories ({names or 'none'})"}
    return [Conclusion("utility", utility, "F", f"names the serving utility; HIFLD territory {utility!r} contains "
                                                f"the subject (grid_join fact {terr[0]['id']})")], {}


def derive(conn: Conn, subject: tuple[str, str], statement: Statement) -> tuple[list[Conclusion], dict[str, str]]:
    out: list[Conclusion] = []
    unresolved: dict[str, str] = {}
    for field, value in statement.states:
        if field == "iso":
            got, why = from_iso(conn, subject, value)
        elif field == "utility":
            got, why = from_utility(conn, subject, value)
        else:
            got, why = [Conclusion(field, value, "F", f"states {field} {value}")], {}
        out.extend(got)
        unresolved.update(why)
    return out, unresolved


# ---------------------------------------------------------------- writing and deciding
def _subject_exists(conn: Conn, subject_type: str, subject_id: str) -> bool:
    table, key = {"site": ("site", "slug"), "campus": ("campus", "id"), "building": ("building", "id")}[subject_type]
    return conn.execute(f"SELECT 1 FROM {table} WHERE {key} = %s", (subject_id,)).fetchone() is not None


def _evidence(f: Finding, c: Conclusion | None = None) -> dict[str, Any]:
    out = {"document_id": f.document.id, "source_url": f.statement.doc.url, "title": f.statement.doc.title,
           "quote": f.statement.quote}
    if c is not None:
        out.update(value=c.value, tag=c.tag)
    return out


def _review_ids(conn: Conn, fact_ids: list[int]) -> list[int]:
    rows = conn.execute("SELECT id FROM review_item WHERE kind = 'fact' AND ref_type = 'fact' AND ref_id = ANY(%s)",
                        ([str(i) for i in fact_ids],)).fetchall()
    return [r["id"] for r in rows]


def decide(facts: FactWriter, subject: tuple[str, str], c: Conclusion, fact_id: int, f: Finding) -> dict[str, int]:
    """Publish grid_join's candidate with the concluded value, reject the others, record the resolution."""
    cands = [r for r in grid_join_facts(facts.conn, *subject, c.field) if r["status"] == "candidate"]
    if not cands:
        return {"published": 0, "rejected": 0}
    keep = [r["id"] for r in cands if r["value"] == c.value]
    drop = [r["id"] for r in cands if r["value"] != c.value]
    reviews = _review_ids(facts.conn, [r["id"] for r in cands])
    published = facts.publish(keep, decided_by=PRODUCER)
    rejected = facts.reject(drop, f"{f.statement.doc.title} ({f.statement.doc.url}) states {c.field} {c.value} "
                                  f"for this facility (fact {fact_id})", decided_by=PRODUCER) if drop else []
    resolution = {"resolution": {**_evidence(f, c), "field": c.field, "fact_id": fact_id,
                                 "decided": date.today().isoformat()}}
    facts.conn.execute("UPDATE review_item SET payload = payload || %s WHERE id = ANY(%s)",
                       (jsonb(resolution), reviews))
    return {"published": len(published), "rejected": len(rejected)}


def leave_open(conn: Conn, subject: tuple[str, str], field: str, why: str, items: list[Finding]) -> int:
    """Attach the evidence to the subject's open review item for the field; it stays open."""
    cands = [r["id"] for r in grid_join_facts(conn, *subject, field) if r["status"] == "candidate"]
    reviews = _review_ids(conn, cands)
    payload = {"evidence": [_evidence(f) for f in items], "evidence_note": why}
    conn.execute("UPDATE review_item SET payload = payload || %s WHERE id = ANY(%s) AND status = 'open'",
                 (jsonb(payload), reviews))
    return len(reviews)


def quotes_by_doc(evidence: tuple[Statement, ...]) -> dict[Doc, list[str]]:
    out: dict[Doc, list[str]] = defaultdict(list)
    for s in evidence:
        out[s.doc].append(s.quote)
    return out


def store_documents(evidence: tuple[Statement, ...] = EVIDENCE, fetcher: http.Fetcher | None = None,
                    stats: RunStats | None = None) -> None:
    """Fetch and store every listed document not stored yet, each in its own transaction (kept on a later
    failure, so a rerun does not fetch it again)."""
    for doc in quotes_by_doc(evidence):
        with db.transaction() as conn:
            row = stored_document(doc, conn, fetcher)
        if stats is not None:
            stats.document(row)


def resolve(conn: Conn, stats: RunStats | None = None, evidence: tuple[Statement, ...] = EVIDENCE) -> dict[str, Any]:
    stats = stats or RunStats()
    facts = FactWriter(conn)
    docs = {d: checked_document(d, quotes, conn) for d, quotes in quotes_by_doc(evidence).items()}
    found: dict[tuple[str, str], list[Finding]] = defaultdict(list)
    missing: list[str] = []
    for s in evidence:
        for subject in s.subjects:
            if not _subject_exists(conn, *subject):
                missing.append(":".join(subject))
                continue
            conclusions, unresolved = derive(conn, subject, s)
            found[subject].append(Finding(s, docs[s.doc], conclusions, unresolved))
    results: list[dict[str, Any]] = []
    decided = {"published": 0, "rejected": 0}
    left_open = 0
    for subject, items in found.items():
        values: dict[str, set[str]] = defaultdict(set)
        for f in items:
            for c in f.conclusions:
                values[c.field].add(c.value)
        for f in items:
            for c in f.conclusions:
                if len(values[c.field]) > 1:
                    continue
                res = facts.add(*subject, c.field, c.value, tag=c.tag, source_name=f.statement.doc.source_name,
                                source_url=f.statement.doc.url, document_id=f.document.id,
                                valid_from=f.statement.doc.doc_date, producer=PRODUCER, status="published",
                                quote=f.statement.quote, quote_loc=f"{f.statement.doc.title}: {f.statement.loc}; "
                                                                   f"{c.why}")
                stats.facts_seen += 1
                stats.facts_new += int(res.created)
                for k, v in decide(facts, subject, c, res.id, f).items():
                    decided[k] += v
                results.append({"subject": ":".join(subject), "field": c.field, "value": c.value, "tag": c.tag,
                                "fact_id": res.id, "document_id": f.document.id, "new": res.created})
        conflicts = {k for k, v in values.items() if len(v) > 1}
        for field in sorted({k for f in items for k in f.unresolved if k not in values} | conflicts):
            why = (f"primary documents disagree: {', '.join(sorted(values[field]))}" if field in conflicts
                   else next(f.unresolved[field] for f in items if field in f.unresolved))
            left_open += leave_open(conn, subject, field, why, items)
            results.append({"subject": ":".join(subject), "field": field, "unresolved": why})
    out = {"documents": len(docs), "subjects": len(found), "missing_subjects": missing, "decided": decided,
           "left_open": left_open, "results": results}
    stats.extra.update({k: v for k, v in out.items() if k != "results"})
    return out


def run(*, source: SourceSpec = SOURCE, evidence: tuple[Statement, ...] = EVIDENCE,
        fetcher: http.Fetcher | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def body(stats: RunStats) -> None:
        store_documents(evidence, fetcher, stats)
        with db.transaction() as conn:
            out.update(resolve(conn, stats, evidence))

    result = collect(source, body)
    result["results"] = out.get("results", [])
    return result


@app.periodic(cron=CRON_UTC, periodic_id="daily")
@app.task(name="collect.grid_evidence", queue="collect", queueing_lock="collect.grid_evidence", lock="collect.grid")
def collect_task(timestamp: int | None = None) -> dict[str, Any]:
    out = run()
    out.pop("results", None)
    return out


@cli.action("collect", "grid_evidence", help="Resolve grid-region review items from primary documents")
def collect_cli(args: argparse.Namespace) -> int:
    return print_stats(run())
