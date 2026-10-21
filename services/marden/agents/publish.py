"""Publishing and approvals (spec 22 layers 4-5): what publishes by itself and what waits for a person.

A candidate publishes automatically only when it comes from a filed document (tag F: spec 22 layer 5,
"routine filed facts that pass layers 1-4 publish automatically"), every hard and soft code check passed,
the verifier agreed, its subject link is accepted (an exact identifier, the upload's own subject, or a
person's earlier confirmation), it is not material, and it does not contradict the current value from
another source. Otherwise it waits in the review queue (review_item, kind 'fact'): a reported (R) source,
disagreement, unsupported, a soft check, a proposed link, a conflict (filed beats reported; conflicts are
flagged, never resolved silently), or materiality (a value that can change a chain status, cushion band or
price, or fire an alert: material fields and status / cushion inputs on chain subjects, price-model inputs that
would change the current value, events the alert engine alerts on or the cushion model reads, and quotes the
materiality rules flag). Hard check failures are rejected outright. Published facts mark read models and models
dirty through the fact writer. `recheck()` re-runs the code checks on facts already published or waiting
(after a validator is tightened): a published fact that no longer passes is withdrawn (superseded, audit
logged) and goes back to review; a waiting one that fails a hard check is rejected.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import urlparse

from marden.agents.config import METHOD, PROMPT_VERSION
from marden.agents.extract import Candidate
from marden.agents.fields import fields_for
from marden.core import db
from marden.core.facts import FactWriter
from marden.core.jsonutil import canonical_json, hash_parts, jsonb
from marden.core.records import EventWriter
from marden.models.materiality import is_material

RATING_FIELDS = frozenset({"rating", "rating_summary", "rating_action", "tenant_rating", "sponsor_rating"})
FILED_KINDS = frozenset({"api", "portal", "feed"})
_CHAIN_SQL = {
    "site": "SELECT true AS x FROM site WHERE slug = %(id)s",
    "instrument": """SELECT true AS x FROM instrument i WHERE i.id = %(id)s AND (i.site_slug IS NOT NULL
                       OR EXISTS (SELECT 1 FROM chain_party c WHERE c.entity_id = i.issuer_entity_id))""",
    "entity": """SELECT true AS x WHERE EXISTS (SELECT 1 FROM chain_party WHERE entity_id = %(id)s)
                   OR EXISTS (SELECT 1 FROM instrument WHERE issuer_entity_id = %(id)s AND site_slug IS NOT NULL)""",
    "campus": "SELECT true AS x FROM site_link WHERE campus_id = %(id)s AND status = 'approved'",
    "plant": """SELECT true AS x FROM fact WHERE subject_type = 'plant' AND subject_id = %(id)s
                  AND field IN ('serves_sites', 'serves_campus') AND status = 'published'""",
    "obligation": """SELECT true AS x FROM fact WHERE subject_type = 'obligation' AND subject_id = %(id)s
                       AND field = 'linked_sites' AND status = 'published'""",
    "queue_project": """SELECT true AS x FROM fact WHERE subject_type = 'queue_project' AND subject_id = %(id)s
                          AND field IN ('linked_site', 'linked_campus') AND status = 'published'""",
}


# The alert engine (marden.alerts.rules.event_finding) raises an alert for every published event of these kinds.
ALERT_EVENT_KINDS = frozenset({"opposition"})


def _model_inputs() -> dict[str, frozenset[str]]:
    """The status and cushion models' input fields: a change there can move a chain status or cushion."""
    from marden.models import cushion_inputs, status_inputs

    return {
        "site": frozenset(status_inputs.SITE_FIELDS) | frozenset(cushion_inputs.SITE_FIELDS),
        "plant": frozenset(cushion_inputs.PLANT_FIELDS),
        "entity": frozenset(status_inputs.ENTITY_FIELDS),
        "instrument": frozenset(status_inputs.INSTRUMENT_FIELDS),
        "obligation": frozenset({"outside_date"}),
    }


def _cushion_event_kinds() -> frozenset[str]:
    from marden.models import cushion_inputs

    return frozenset(cushion_inputs.EVENT_KINDS)


def _price_inputs() -> dict[str, frozenset[str]]:
    """The price model's fact inputs: a new value there reprices the instrument, which can fire a price alert
    (PX) and move fund mark gaps."""
    from marden.models import price_equity, price_signals, price_terms

    return {
        "instrument": frozenset(price_terms.TERM_FIELDS),
        "entity": frozenset(price_equity.DEBT_FIELDS) | frozenset(price_equity.SHARE_FIELDS),
        "site": frozenset(price_signals.SITE_FIELDS),
    }


def chain_relevant(conn: db.Conn, subject_type: str, subject_id: str) -> bool:
    sql = _CHAIN_SQL.get(subject_type)
    return bool(sql and conn.execute(sql + " LIMIT 1", {"id": subject_id}).fetchone())


def changes_current(conn: db.Conn, c: Candidate) -> bool:
    """True unless the subject already has this exact value as its current published one."""
    row = conn.execute(
        "SELECT value FROM fact_current WHERE subject_type = %s AND subject_id = %s AND field = %s LIMIT 1",
        (c.subject_type, c.subject_id, c.field)).fetchone()
    return row is None or canonical_json(row["value"]) != canonical_json(c.value)


def event_title(c: Candidate) -> str:
    """The title of the event an event-like field writes (the alert engine's materiality rules read it)."""
    words = c.value if isinstance(c.value, str) else " ".join(
        str(c.value.get(k)) for k in ("kind", "action", "outcome", "agency", "rating", "body", "description",
                                      "status", "number") if isinstance(c.value, dict) and c.value.get(k))
    return f"{c.field.replace('_', ' ').capitalize()}: {words}"[:200]


def materiality(conn: db.Conn, c: Candidate) -> tuple[bool, list[str]]:
    """Whether publishing the candidate could change a chain status, a cushion band, a price or fire an alert
    (spec 22 layer 5), with the reason codes. Material candidates always wait for a person."""
    spec = fields_for(c.area)[c.field]
    codes: list[str] = []
    found = is_material({"title": c.quote, "kind": spec.event or c.area})
    codes += found.codes
    if spec.event:
        # the event this fact writes is judged by the alert engine on its own title and kind
        codes += is_material({"title": event_title(c), "kind": spec.event}).codes
        if spec.event in ALERT_EVENT_KINDS:
            codes.append("alert_event")
    if c.field in RATING_FIELDS:
        codes.append("rating")
    if c.subject_type and c.subject_id:
        if chain_relevant(conn, c.subject_type, c.subject_id):
            if spec.material:
                codes.append("chain_input")
            elif c.field in _model_inputs().get(c.subject_type, frozenset()):
                codes.append("model_input")
            elif spec.event in _cushion_event_kinds():
                codes.append("model_input")  # the cushion model reads these events on chain subjects
        if c.field in _price_inputs().get(c.subject_type, frozenset()) and changes_current(conn, c):
            codes.append("price_input")
    return bool(codes), sorted(set(codes))


def doc_source(conn: db.Conn, doc: Any) -> tuple[str, str, str | None]:
    """(tag, source_name, source_url) for facts read from a document. Filed beats reported."""
    src = conn.execute("SELECT name, kind FROM source WHERE id = %s", (doc.source_id,)).fetchone()
    meta = doc.meta or {}
    url = meta.get("source_url") or (doc.url if doc.url.startswith(("http://", "https://")) else None)
    host = urlparse(url).hostname or "" if url else ""
    if meta.get("filed") is not None:
        filed = bool(meta["filed"])
    elif src and src["kind"] in FILED_KINDS:
        filed = True
    else:
        filed = host.endswith((".gov", ".us")) or ".gov." in host
    name = meta.get("publisher") or (src["name"] if src and src["kind"] != "manual" else None) or host or "Staff upload"
    return ("F" if filed else "R"), str(name)[:120], url


def conflicts(conn: db.Conn, c: Candidate, document_id: int, valid_from: date, tag: str) -> list[dict[str, Any]]:
    """Published values this candidate contradicts: another source's value for the same subject, field and
    as-of date, or (filed beats reported) a filed value when this one is only reported. A later value from
    a filing is history moving on, not a conflict."""
    assert c.subject_type and c.subject_id
    rows = conn.execute(
        """SELECT * FROM fact WHERE subject_type = %s AND subject_id = %s AND field = %s AND status = 'published'
             AND document_id IS DISTINCT FROM %s AND (valid_from = %s OR (%s = 'R' AND tag = 'F'))
           ORDER BY valid_from DESC, id DESC LIMIT 5""",
        (c.subject_type, c.subject_id, c.field, document_id, valid_from, tag),
    ).fetchall()
    value = canonical_json(c.value)
    return [{"fact_id": r["id"], "value": r["value"], "tag": r["tag"], "source_name": r["source_name"],
             "valid_from": r["valid_from"].isoformat()} for r in rows if canonical_json(r["value"]) != value]


def candidate_key(job: dict[str, Any], c: Candidate) -> str:
    return hash_parts("agent_candidate", job["document_id"], PROMPT_VERSION, c.area, c.field, c.subject_kind,
                      c.subject_name.lower(), canonical_json(c.value), c.quote)


def store_candidate(conn: db.Conn, job: dict[str, Any], c: Candidate) -> int:
    row = conn.execute(
        """INSERT INTO agent_candidate (job_id, document_id, area, section, subject_kind, subject_mention,
                                        subject_type, subject_id, link_id, field, value, unit, as_of, quote,
                                        quote_loc, page, confidence, checks, verdict, verdict_reason, dedupe_key)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (dedupe_key) DO UPDATE SET checks = EXCLUDED.checks, verdict = EXCLUDED.verdict,
               verdict_reason = EXCLUDED.verdict_reason, subject_type = EXCLUDED.subject_type,
               subject_id = EXCLUDED.subject_id, link_id = EXCLUDED.link_id
           RETURNING id""",
        (job["id"], job["document_id"], c.area, c.section, c.subject_kind, c.subject_name or "(unnamed)",
         c.subject_type, c.subject_id, c.link_id, c.field, jsonb(c.value), c.unit, c.as_of, c.quote or "(none)",
         f"p{c.page} {c.section}" if c.page else c.section, c.page, c.confidence, jsonb(c.checks), c.verdict,
         c.verdict_reason, candidate_key(job, c)),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def _outcome(conn: db.Conn, cid: int, outcome: str, reason: str, *, fact_id: int | None = None,
             material: bool = False, codes: list[str] | None = None) -> None:
    conn.execute(
        """UPDATE agent_candidate SET outcome = %s, outcome_reason = %s, fact_id = coalesce(%s, fact_id),
                  material = %s, material_codes = %s WHERE id = %s""",
        (outcome, reason[:1000], fact_id, material, codes or [], cid),
    )


def review_reasons(c: Candidate, material_codes: list[str], conflict: list[dict[str, Any]],
                   tag: str = "F") -> list[str]:
    out = []
    if tag != "F":
        out.append("reported source (not a filing): a person confirms before it publishes")
    if c.verdict != "agree":
        out.append(f"verifier: {c.verdict or 'no verdict'}" + (f" ({c.verdict_reason})" if c.verdict_reason else ""))
    out += [f"check {k}: {v.get('detail') or 'failed'}" for k, v in c.checks.items() if not v.get("ok")]
    if c.link_status != "accepted":
        out.append("subject link proposed, not confirmed" if c.subject_id else "no matching Marden subject")
    if material_codes:
        out.append("material: " + ", ".join(material_codes))
    for x in conflict:
        out.append(f"conflict with current {x['tag']} value {canonical_json(x['value'])[:80]} from "
                   f"{x['source_name']} ({x['valid_from']})")
    return out


def publish_candidates(job: dict[str, Any], doc: Any, cands: list[Candidate]) -> dict[str, int]:
    stats = {"candidates": len(cands), "published": 0, "review": 0, "rejected": 0, "duplicate": 0}
    seen: set[int] = set()
    today = date.today()
    with db.transaction() as conn:
        tag, source_name, source_url = doc_source(conn, doc)
        facts, events = FactWriter(conn), EventWriter(conn)
        for c in cands:
            c.id = cid = store_candidate(conn, job, c)
            if cid in seen:  # the same fact twice in one document (overlapping sections): handled once
                stats["duplicate"] += 1
                continue
            seen.add(cid)
            if c.hard_fail:
                failed = [f"{k}: {v.get('detail') or 'failed'}" for k, v in c.checks.items()
                          if v["hard"] and not v["ok"]]
                _outcome(conn, cid, "rejected", "; ".join(failed))
                stats["rejected"] += 1
                continue
            material, codes = materiality(conn, c)
            valid_from = c.as_of if c.as_of and c.as_of <= today else (doc.doc_date or doc.fetched_at.date())
            conflict = conflicts(conn, c, doc.id, valid_from, tag) if c.subject_id else []
            reasons = review_reasons(c, codes, conflict, tag)
            payload = {"candidate_id": cid, "job_id": job["id"], "document_id": doc.id, "area": c.area,
                       "field": c.field, "value": c.value, "unit": c.unit, "quote": c.quote, "page": c.page,
                       "subject": {"kind": c.subject_kind, "mention": c.subject_name, "type": c.subject_type,
                                   "id": c.subject_id, "link_id": c.link_id, "link_status": c.link_status},
                       "verdict": c.verdict, "verdict_reason": c.verdict_reason,
                       "corrected_value": c.corrected_value, "material_codes": codes, "conflicts": conflict}
            title = f"{c.field} = {canonical_json(c.value)[:70]} on {c.subject_type or c.subject_kind} " \
                    f"{c.subject_id or c.subject_name[:60]}"
            if c.subject_type is None or c.subject_id is None:
                conn.execute(
                    """INSERT INTO review_item (kind, ref_type, ref_id, title, reason, payload)
                       VALUES ('fact', 'agent_candidate', %s, %s, %s, %s)
                       ON CONFLICT (kind, ref_type, ref_id) DO NOTHING""",
                    (str(cid), title[:200], "; ".join(reasons)[:2000], jsonb(payload)),
                )
                _outcome(conn, cid, "review", "; ".join(reasons), material=material, codes=codes)
                stats["review"] += 1
                continue
            written = facts.add(
                c.subject_type, c.subject_id, c.field, c.value, tag=tag, source_name=source_name,
                source_url=source_url, valid_from=valid_from, producer=f"agent:{c.area}", status="candidate",
                unit=c.unit if c.unit != "date" else "date", document_id=doc.id, quote=c.quote,
                quote_loc=f"p{c.page}" if c.page else c.section, method_version=f"{METHOD} {c.area}",
                confidence=c.confidence, material=material,
            )
            if c.verdict:
                facts.set_verifier(written.id, c.verdict)
            event_id = _event(events, c, written.id, doc, tag, source_name, source_url, valid_from, material)
            if not reasons and written.status == "candidate":
                facts.publish([written.id])
                if event_id is not None:
                    events.publish([event_id])
                _outcome(conn, cid, "published", "verified routine filed fact", fact_id=written.id,
                         material=material, codes=codes)
                stats["published"] += 1
            elif written.status == "published":  # a candidate that published itself on an earlier run keeps that
                conn.execute(
                    """UPDATE agent_candidate SET outcome = 'duplicate', outcome_reason = %s, fact_id = %s
                       WHERE id = %s AND outcome IS DISTINCT FROM 'published'""",
                    ("the same fact was already published", written.id, cid))
                stats["duplicate"] += 1
            elif written.status == "rejected":
                _outcome(conn, cid, "rejected", "a person rejected this fact before", fact_id=written.id)
                stats["rejected"] += 1
            else:
                facts.request_review(written.id, title=title[:200], reason="; ".join(reasons)[:2000], payload=payload)
                _outcome(conn, cid, "review", "; ".join(reasons), fact_id=written.id, material=material, codes=codes)
                stats["review"] += 1
    return stats


def _event(events: EventWriter, c: Candidate, fact_id: int, doc: Any, tag: str, source_name: str,
           source_url: str | None, valid_from: date, material: bool) -> int | None:
    spec = fields_for(c.area)[c.field]
    if spec.event is None or c.subject_type is None or c.subject_id is None:
        return None
    raw = c.value.get("date") if isinstance(c.value, dict) else (c.value if spec.vtype in ("date", "month") else None)
    when = valid_from
    if isinstance(raw, str) and len(raw) >= 7:
        try:
            when = date.fromisoformat(raw if len(raw) == 10 else f"{raw}-01")
        except ValueError:
            when = valid_from
    title = event_title(c)
    res = events.add(c.subject_type, c.subject_id, when, spec.event, title, tag=tag, source_name=source_name,
                     source_url=source_url, material=material, document_id=doc.id, fact_id=fact_id)
    return res.id if res.status == "candidate" else None


def release_after_link(conn: db.Conn, link_id: int, by: str) -> list[int]:
    """A person accepted a proposed subject link: candidates that waited only for it publish now.

    Only candidates from a filed document whose checks all passed, that the verifier agreed with, that are
    not material and that still contradict nothing are released; the rest keep their review items."""
    rows = conn.execute(
        """SELECT c.*, f.status AS fact_status, f.valid_from, f.tag FROM agent_candidate c
           JOIN fact f ON f.id = c.fact_id
           WHERE c.link_id = %s AND c.outcome = 'review' AND c.verdict = 'agree' AND NOT c.material
             AND f.status = 'candidate' AND f.tag = 'F'""",
        (link_id,),
    ).fetchall()
    released: list[int] = []
    facts, events = FactWriter(conn), EventWriter(conn)
    for r in rows:
        if any(not v.get("ok") for v in (r["checks"] or {}).values()):
            continue
        cand = Candidate(area=r["area"], section=r["section"] or "", subject_kind=r["subject_kind"],
                         subject_name=r["subject_mention"], subject_identifier=None, field=r["field"],
                         value=r["value"], unit=r["unit"], as_of=r["as_of"], quote=r["quote"], page=r["page"],
                         confidence=r["confidence"], subject_type=r["subject_type"], subject_id=r["subject_id"])
        if conflicts(conn, cand, r["document_id"], r["valid_from"], r["tag"]):
            continue
        facts.publish([r["fact_id"]])
        ev = [e["id"] for e in conn.execute(
            "SELECT id FROM event WHERE fact_id = %s AND status = 'candidate' AND NOT material", (r["fact_id"],))]
        events.publish(ev)
        conn.execute(
            """UPDATE review_item SET status = 'approved', decided_by = %s, decided_at = now()
               WHERE kind = 'fact' AND ref_type = 'fact' AND ref_id = %s AND status = 'open'""",
            (f"{by} (subject link accepted)", str(r["fact_id"])))
        conn.execute("UPDATE agent_candidate SET outcome = 'published', outcome_reason = %s WHERE id = %s",
                     (f"published when {by} accepted the subject link", r["id"]))
        released.append(int(r["fact_id"]))
    return released


def _withdraw(conn: db.Conn, r: dict[str, Any], reasons: list[str], by: str) -> None:
    """A published agent fact that fails the code checks now: superseded (history keeps what Marden showed and
    when), its published events rejected, read models and models marked dirty, audit logged, and the candidate
    back in the review queue, where a person can publish it again only with a quote that holds the value."""
    from marden.readmodels.deps import mark_dirty_for

    reason = "; ".join(reasons)
    fact = conn.execute("""UPDATE fact SET status = 'superseded' WHERE id = %s AND status = 'published'
                           RETURNING id, subject_type, subject_id, field""", (r["fact_id"],)).fetchone()
    if fact is not None:
        mark_dirty_for(fact["subject_type"], fact["subject_id"], fact["field"], conn=conn, reason=f"fact:{fact['id']}")
        conn.execute("INSERT INTO audit_log (user_id, action, target, meta) VALUES (%s, 'fact.withdraw', %s, %s)",
                     (by, f"fact:{fact['id']}", jsonb({"reason": reason, "candidate_id": r["id"]})))
    for ev in conn.execute("""UPDATE event SET status = 'rejected' WHERE fact_id = %s AND status = 'published'
                              RETURNING id, subject_type, subject_id""", (r["fact_id"],)).fetchall():
        mark_dirty_for(ev["subject_type"], ev["subject_id"], "@event", conn=conn, reason=f"event:{ev['id']}")
        conn.execute("INSERT INTO audit_log (user_id, action, target, meta) VALUES (%s, 'event.withdraw', %s, %s)",
                     (by, f"event:{ev['id']}", jsonb({"reason": reason, "fact_id": r["fact_id"]})))
    payload = {"candidate_id": r["id"], "job_id": r["job_id"], "document_id": r["document_id"], "area": r["area"],
               "field": r["field"], "value": r["value"], "unit": r["unit"], "quote": r["quote"], "page": r["page"],
               "subject": {"kind": r["subject_kind"], "mention": r["subject_mention"], "type": r["subject_type"],
                           "id": r["subject_id"], "link_id": r["link_id"], "link_status": "accepted"},
               "verdict": r["verdict"], "verdict_reason": r["verdict_reason"], "withdrawn_fact_id": r["fact_id"],
               "material_codes": list(r["material_codes"] or []), "conflicts": []}
    title = f"{r['field']} = {canonical_json(r['value'])[:70]} on {r['subject_type']} {r['subject_id']} (withdrawn)"
    conn.execute(
        """INSERT INTO review_item (kind, ref_type, ref_id, title, reason, payload)
           VALUES ('fact', 'agent_candidate', %s, %s, %s, %s)
           ON CONFLICT (kind, ref_type, ref_id) DO UPDATE SET status = 'open', reason = EXCLUDED.reason,
             payload = EXCLUDED.payload, decided_by = NULL, decided_at = NULL""",
        (str(r["id"]), title[:200], f"withdrawn after publishing: {reason}"[:2000], jsonb(payload)))
    _outcome(conn, r["id"], "review", f"withdrawn: {reason}", material=bool(r["material"]),
             codes=list(r["material_codes"] or []))


def _published_by_code(r: dict[str, Any]) -> bool:
    """Published by the pipeline (or released when a person accepted only the subject link), not by a person
    who approved or edited the value: a person's decision on a value stands."""
    reason = str(r.get("outcome_reason") or "")
    return str(r.get("fact_producer") or "").startswith("agent:") and (
        reason == "verified routine filed fact" or reason.startswith("published when "))


def recheck(*, by: str = f"agent:recheck@{PROMPT_VERSION}", document_id: int | None = None) -> dict[str, Any]:
    """Re-run the code checks on every agent candidate that is published or waiting in review."""
    from marden.agents.text import read_text
    from marden.agents.validators import check_candidate
    from marden.core import documents

    out: dict[str, Any] = {"checked": 0, "withdrawn": [], "rejected": [], "unreadable": []}
    with db.transaction() as conn:
        rows = conn.execute(
            """SELECT c.*, f.status AS fact_status, f.producer AS fact_producer
               FROM agent_candidate c LEFT JOIN fact f ON f.id = c.fact_id
               WHERE c.outcome IN ('published', 'review') AND (%s::bigint IS NULL OR c.document_id = %s)
               ORDER BY c.document_id, c.id""", (document_id, document_id)).fetchall()
    texts: dict[int, Any] = {}
    for r in rows:
        if r["document_id"] not in texts:
            doc = documents.get_document(r["document_id"])
            try:
                texts[r["document_id"]] = read_text(doc.read(), doc.content_type, doc.url) if doc else None
            except (ValueError, KeyError, OSError):
                texts[r["document_id"]] = None
        text = texts[r["document_id"]]
        if text is None:
            out["unreadable"].append(r["id"])
            continue
        if r["field"] not in fields_for(r["area"]):
            continue
        cand = Candidate(area=r["area"], section=r["section"] or "", subject_kind=r["subject_kind"],
                         subject_name=r["subject_mention"], subject_identifier=None, field=r["field"],
                         value=r["value"], unit=r["unit"], as_of=r["as_of"], quote=r["quote"], page=r["page"],
                         confidence=r["confidence"])
        check_candidate(cand, text)
        checks = {**cand.checks, **{k: v for k, v in (r["checks"] or {}).items() if k in ("date_order", "tranche_sum")}}
        out["checked"] += 1
        failed = {k: v for k, v in checks.items() if not v.get("ok")}
        with db.transaction() as conn:
            conn.execute("UPDATE agent_candidate SET checks = %s WHERE id = %s", (jsonb(checks), r["id"]))
            if not failed:
                continue
            reasons = [f"check {k}: {v.get('detail') or 'failed'}" for k, v in failed.items()]
            if r["outcome"] == "published" and r["fact_status"] == "published" and _published_by_code(r):
                _withdraw(conn, r, reasons, by)
                out["withdrawn"].append({"candidate_id": r["id"], "fact_id": r["fact_id"], "reasons": reasons})
            elif r["outcome"] == "review" and any(v.get("hard") for v in failed.values()):
                hard = "; ".join(f"{k}: {v.get('detail') or 'failed'}" for k, v in failed.items() if v.get("hard"))
                if r["fact_id"] and r["fact_status"] == "candidate":
                    FactWriter(conn).reject([r["fact_id"]], f"code check: {hard}", decided_by=by)
                    ev = [e["id"] for e in conn.execute(
                        "SELECT id FROM event WHERE fact_id = %s AND status = 'candidate'", (r["fact_id"],))]
                    EventWriter(conn).reject(ev, f"code check: {hard}", decided_by=by)
                conn.execute(
                    """UPDATE review_item SET status = 'rejected', decided_by = %s, decided_at = now()
                       WHERE kind = 'fact' AND status = 'open' AND ((ref_type = 'agent_candidate' AND ref_id = %s)
                             OR (ref_type = 'fact' AND ref_id = %s))""",
                    (by, str(r["id"]), str(r["fact_id"]) if r["fact_id"] else ""))
                _outcome(conn, r["id"], "rejected", hard, fact_id=r["fact_id"])
                out["rejected"].append({"candidate_id": r["id"], "reasons": [hard]})
    return out
