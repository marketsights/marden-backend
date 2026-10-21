"""Link one job's candidates to subjects: deterministic resolution first, then the linker model.

    done = link(llm, job, doc, text, cands, subjects)   # sets subject_type / subject_id / link on candidates

Writes agent_link rows (accepted for identifier / upload / confirmed matches, proposed otherwise) and a
review item for every proposed link. Returns False while a linker call is pending (batch planning).
"""

from __future__ import annotations

from typing import Any

from marden.agents.extract import Candidate
from marden.agents.linker import (
    LINKER_SYSTEM,
    RELATIONS,
    Mention,
    fuzzy,
    index,
    linker_tool,
    mention_key,
    mentions,
    resolve,
    write_link,
)
from marden.agents.llm import Llm, LlmRequest
from marden.agents.text import DocText, find_quote
from marden.core import db
from marden.core.jsonutil import jsonb

LINKABLE = ("entity", "instrument", "site", "campus", "plant")


def filer_of(conn: db.Conn, doc: Any) -> tuple[str, str] | None:
    cik = (doc.meta or {}).get("cik")
    if not cik:
        return None
    rows = conn.execute("SELECT id FROM entity WHERE ltrim(cik, '0') = ltrim(%s, '0')", (str(cik),)).fetchall()
    return ("entity", rows[0]["id"]) if len(rows) == 1 else None


def _site_list() -> str:
    return "\n".join(f"site {r.subject_id}: {r.name} ({r.extra[:160]})" for r in index().get("site") or [])


def request(job_id: int | None, sha: str | None, summary: str, todo: list[tuple[Mention, list[Any]]]) -> LlmRequest:
    parts = [f"Document summary: {summary[:800]}", "Financed sites Marden tracks:\n" + _site_list()]
    for m, cands in todo:
        listed = "\n".join(f"  - {r.subject_type} {r.subject_id}: {r.name}"
                           f"{f' ({r.extra[:120]})' if r.extra else ''} [score {s:.0f}]" for r, s in cands)
        quotes = "\n".join(f"  quoted: \"{q[:300]}\"" for q in m.quotes)
        parts.append(f"<mention kind=\"{m.kind}\">{m.name}\n{quotes}\n candidates:\n{listed or '  (none)'}\n</mention>")
    return LlmRequest(agent="linker", role="linker", system=LINKER_SYSTEM, tool=linker_tool(),
                      content=["\n\n".join(parts)], max_tokens=6000, job_id=job_id, document_sha256=sha,
                      section="link")


def _open_review(conn: db.Conn, link_id: int, mention: str, subject: str, evidence: str | None, method: str,
                 document_id: int) -> None:
    conn.execute(
        """INSERT INTO review_item (kind, ref_type, ref_id, title, reason, payload)
           VALUES ('link', 'agent_link', %s, %s, %s, %s)
           ON CONFLICT (kind, ref_type, ref_id) DO NOTHING""",
        (str(link_id), f"Link '{mention[:80]}' -> {subject}"[:200],
         f"proposed link ({method}); auto-accepted only on exact identifier matches",
         jsonb({"document_id": document_id, "evidence": evidence, "method": method})),
    )


def link(llm: Llm, job: dict[str, Any], doc: Any, text: DocText, cands: list[Candidate],
         subjects: list[dict[str, Any]], summary: str) -> bool:
    live = [c for c in cands if not c.hard_fail]
    ms = [m for m in mentions(live, subjects) if m.kind in LINKABLE]
    upload_links = {k: v for k, v in (job.get("links") or {}).items() if isinstance(v, str) and v}
    resolved: dict[tuple[str, str], tuple[int, str, str, str]] = {}  # (kind, key) -> (link id, status, type, id)
    todo: list[tuple[Mention, list[Any]]] = []
    with db.transaction() as conn:
        filer = filer_of(conn, doc)
        for m in ms:
            hit = resolve(m, filer=filer, links=upload_links)
            if hit is not None:
                lid, status = write_link(conn, document_id=doc.id, job_id=job["id"], mention=m.name,
                                         subject_type=hit[0], subject_id=hit[1], method=hit[2], score=hit[3],
                                         evidence=m.quotes[0] if m.quotes else None)
                resolved[(m.kind, m.key)] = (lid, status, hit[0], hit[1])
                if status == "proposed":
                    _open_review(conn, lid, m.name, f"{hit[0]} {hit[1]}", m.quotes[0] if m.quotes else None, hit[2],
                                 doc.id)
                if hit[2] != "name":
                    continue
            todo.append((m, fuzzy(m.kind, m.name)))
    done = True
    if todo:
        res = llm.call(request(job["id"], doc.sha256, summary, todo))
        if res is None:
            done = False
        elif res.tool_input:
            offered = {(m.kind, m.key): {r.subject_id for r, _ in cs} for m, cs in todo}
            by_key = {m.key: m for m, _ in todo}
            with db.transaction() as conn:
                for item in res.tool_input.get("links") or []:
                    _apply(conn, item, offered, by_key, resolved, doc, job, text)
    for c in cands:
        hit = resolved.get((c.subject_kind, mention_key(c.subject_name)))
        if hit is not None:
            c.link_id, c.link_status, c.subject_type, c.subject_id = hit
    return done


def _apply(conn: db.Conn, item: dict[str, Any], offered: dict[tuple[str, str], set[str]],
           by_key: dict[str, Mention], resolved: dict[tuple[str, str], tuple[int, str, str, str]], doc: Any,
           job: dict[str, Any], text: DocText) -> None:
    key = mention_key(str(item.get("mention") or ""))
    m = by_key.get(key)
    relation = item.get("relation")
    evidence = str(item.get("evidence") or "")
    if m is None or relation not in RELATIONS or find_quote(text, evidence) is None:
        return  # every link needs an exact passage as evidence
    stype, sid = item.get("subject_type"), str(item.get("subject_id") or "")
    if relation == "is":
        if stype != m.kind or sid not in offered.get((m.kind, key), set()):
            return
        if (m.kind, key) in resolved and resolved[(m.kind, key)][1] == "accepted":
            return
        lid, status = write_link(conn, document_id=doc.id, job_id=job["id"], mention=m.name, subject_type=stype,
                                 subject_id=sid, method="agent", evidence=evidence,
                                 score=float(item.get("confidence") or 0) * 100)
        resolved[(m.kind, key)] = (lid, status, stype, sid)
        _open_review(conn, lid, m.name, f"{stype} {sid}", evidence, "agent", doc.id)
        return
    rtype, rid = item.get("related_type"), item.get("related_id")
    if not (rtype and rid) or not any(r.subject_id == rid for r in index().get(rtype) or []):
        return
    target = resolved.get((m.kind, key))
    subject = (target[2], target[3]) if target else None
    if subject is None:
        return  # a chain link needs the mention itself resolved first
    lid, _ = write_link(conn, document_id=doc.id, job_id=job["id"], mention=m.name, subject_type=subject[0],
                        subject_id=subject[1], method="agent", evidence=evidence, relation=relation,
                        related=(rtype, rid), score=float(item.get("confidence") or 0) * 100)
    _open_review(conn, lid, m.name, f"{relation} {rtype} {rid}", evidence, "agent", doc.id)
