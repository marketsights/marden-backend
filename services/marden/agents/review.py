"""Staff decisions on the review queue, applied by the worker (the API records them in agent_decision).

    approve  fact      publish the candidate fact (and its event); a proposed subject link is accepted with it
             event     publish the event
             link      accept the link (agent_link) or approve the site link (site_link)
    reject   any       reject with the reason (audit_log); the candidate is closed
    edit     fact      a new fact with the corrected value, unit, date or subject and a quote that must be in the
                       document verbatim; it is published under the person's name, the original is rejected
Publishing goes through FactWriter / EventWriter, so read models and models are marked dirty in the same
transaction and screens and alerts update.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from marden.agents.extract import Candidate
from marden.agents.fields import all_fields
from marden.agents.publish import _event, release_after_link
from marden.agents.text import find_quote, read_text
from marden.agents.validators import (
    GENERIC_TYPES,
    check_candidate,
    number_matches,
    stated_scale,
    value_number_checks,
)
from marden.core import checks, db, documents
from marden.core.checks import ValidationError
from marden.core.facts import FactWriter
from marden.core.jsonutil import jsonb
from marden.core.records import EventWriter


def _close(conn: db.Conn, item_id: int, status: str, by: str) -> None:
    conn.execute("""UPDATE review_item SET status = %s, decided_by = %s, decided_at = now()
                    WHERE id = %s AND status = 'open'""",
                 (status, by, item_id))


def _audit(conn: db.Conn, by: str, action: str, target: str, meta: dict[str, Any]) -> None:
    conn.execute("INSERT INTO audit_log (user_id, action, target, meta) VALUES (%s, %s, %s, %s)",
                 (by, action, target, jsonb(meta)))


def _accept_link(conn: db.Conn, link_id: int | None, by: str) -> None:
    """Approving a fact on a proposed subject confirms the subject link too (and releases its other facts)."""
    if link_id:
        row = conn.execute("""UPDATE agent_link SET status = 'accepted', decided_by = %s, decided_at = now()
                              WHERE id = %s AND status = 'proposed' RETURNING id""", (by, link_id)).fetchone()
        conn.execute("""UPDATE review_item SET status = 'approved', decided_by = %s, decided_at = now()
                        WHERE kind = 'link' AND ref_type = 'agent_link' AND ref_id = %s AND status = 'open'""",
                     (by, str(link_id)))
        if row is not None:
            release_after_link(conn, link_id, by)


def _area_of(producer: str | None, payload: dict[str, Any]) -> str | None:
    if payload.get("area"):
        return str(payload["area"])
    if producer and producer.startswith("agent:"):
        return producer.split(":", 1)[1]
    return None


def edit_fact(conn: db.Conn, item: dict[str, Any], fact: dict[str, Any] | None, edit: dict[str, Any],
              by: str) -> dict[str, Any]:
    payload = item["payload"] or {}
    quote = str(edit.get("quote") or "").strip()
    if not quote:
        raise ValidationError("an edit needs the exact quote from the document")
    doc_id = (fact or {}).get("document_id") or payload.get("document_id")
    doc = documents.get_document(int(doc_id)) if doc_id else None
    if doc is None:
        raise ValidationError("the source document of this fact is not stored")
    text = read_text(doc.read(), doc.content_type, doc.url)
    if find_quote(text, quote) is None:
        raise ValidationError("the quote is not in the document (verbatim, whitespace normalized)")
    proposed = payload.get("subject") or {}
    subject_type = edit.get("subject_type") or (fact or {}).get("subject_type") or proposed.get("type")
    subject_id = edit.get("subject_id") or (fact or {}).get("subject_id") or proposed.get("id")
    field = edit.get("field") or (fact or {}).get("field") or payload.get("field")
    if not (subject_type and subject_id and field):
        raise ValidationError("an edit needs subject_type, subject_id and field")
    if "value" not in edit or edit["value"] is None:
        raise ValidationError("an edit needs a value")
    area = _edit_area(_area_of((fact or {}).get("producer"), payload), field)
    spec = all_fields().get((area, field)) if area else None
    if spec is not None:
        cand = Candidate(area=area or "", section="edit", subject_kind=subject_type, subject_name=str(subject_id),
                         subject_identifier=None, field=field, value=edit["value"], unit=spec.unit, as_of=None,
                         quote=quote, page=None, confidence=None)
        check_candidate(cand, text)
        failed = [f"{k}: {v.get('detail') or 'failed'}" for k, v in cand.checks.items() if v["hard"] and not v["ok"]]
        if failed:
            raise ValidationError("the edit fails the code checks: " + "; ".join(failed))
    else:  # a field outside the agents' vocabulary: the numbers in the value must still be in the quote
        hit, value = find_quote(text, quote), edit["value"]
        if isinstance(value, int | float) and not isinstance(value, bool):
            ok = any(number_matches(float(value), vt, quote, stated_scale(text, hit))[0] for vt in GENERIC_TYPES)
            found = {"value_numbers": {"ok": ok, "hard": True, "detail": f"{value} is not written in the quote"}}
        else:
            found = value_number_checks(value, quote, text, hit)
        failed = [f"{k}: {v.get('detail') or 'failed'}" for k, v in found.items() if v["hard"] and not v["ok"]]
        if failed:
            raise ValidationError("the edit fails the code checks: " + "; ".join(failed))
    tag = (fact or {}).get("tag") or "R"
    as_of = edit.get("as_of") or ((fact or {}).get("valid_from") or doc.doc_date or date.today())
    writer = FactWriter(conn)
    new = writer.add(subject_type, str(subject_id), field, edit["value"], tag=tag,
                     source_name=(fact or {}).get("source_name") or doc.source_id,
                     source_url=(fact or {}).get("source_url"), valid_from=checks.not_future(as_of, "as_of"),
                     producer=f"review:{by}", status="candidate", unit=edit.get("unit") or (fact or {}).get("unit"),
                     document_id=doc.id, quote=quote, quote_loc=edit.get("quote_loc"),
                     method_version=(fact or {}).get("method_version"), material=bool((fact or {}).get("material")))
    writer.publish([new.id], decided_by=by)
    events = EventWriter(conn)
    out: dict[str, Any] = {"fact_id": new.id}
    if fact is not None:
        writer.reject([fact["id"]], f"edited by {by}: fact {new.id}", decided_by=by)
        # the original's event (a hearing on the wrong date, on the wrong subject) goes with it
        stale = [r["id"] for r in conn.execute("SELECT id FROM event WHERE fact_id = %s AND status = 'candidate'",
                                               (fact["id"],)).fetchall()]
        events.reject(stale, f"edited by {by}: fact {new.id}", decided_by=by)
    if spec is not None and spec.event is not None:
        # an event-like field writes its event with the fact (spec 22 layer 5), the edited one included:
        # without it the corrected hearing or opposition step would be on no screen and alert nobody
        cand.subject_type, cand.subject_id = subject_type, str(subject_id)
        event_id = _event(events, cand, new.id, doc, tag, (fact or {}).get("source_name") or doc.source_id,
                          (fact or {}).get("source_url"), checks.not_future(as_of, "as_of"),
                          bool((fact or {}).get("material")))
        if event_id is not None:
            out["events"] = events.publish([event_id], decided_by=by)
    if payload.get("candidate_id"):
        conn.execute("""UPDATE agent_candidate SET outcome = 'published', outcome_reason = %s, fact_id = %s
                        WHERE id = %s""",
                     (f"edited and approved by {by}", new.id, payload["candidate_id"]))
    _accept_link(conn, (payload.get("subject") or {}).get("link_id"), by)
    return out


def _edit_area(area: str | None, field: str) -> str | None:
    """The area whose vocabulary the edited field is checked against: the original's, else the first area
    that has the field (a 'permits' hearing re-filed as a 'local' opposition step)."""
    fields = all_fields()
    if area is not None and (area, field) in fields:
        return area
    return next((a for a, f in fields if f == field), area)


def apply_decision(conn: db.Conn, d: dict[str, Any]) -> dict[str, Any]:
    item = conn.execute("SELECT * FROM review_item WHERE id = %s FOR UPDATE", (d["review_item_id"],)).fetchone()
    if item is None:
        raise ValidationError("review item not found")
    if item["status"] != "open":
        raise ValidationError(f"review item already {item['status']}")
    by, action, reason = d["decided_by"], d["action"], d.get("reason")
    payload = item["payload"] or {}
    out: dict[str, Any] = {"review_item_id": item["id"], "action": action}
    if action == "reject":
        checks.non_empty(reason, "reason")
    if item["kind"] == "fact":
        fact = None
        if item["ref_type"] == "fact":
            fact = conn.execute("SELECT * FROM fact WHERE id = %s", (int(item["ref_id"]),)).fetchone()
        if action == "edit" or (action == "approve" and fact is None):
            out |= edit_fact(conn, item, fact, d.get("edit") or {}, by)
        elif action == "approve":
            assert fact is not None
            _accept_link(conn, (payload.get("subject") or {}).get("link_id"), by)
            out["published"] = FactWriter(conn).publish([fact["id"]], decided_by=by)
            ev = [r["id"] for r in conn.execute("SELECT id FROM event WHERE fact_id = %s AND status = 'candidate'",
                                                (fact["id"],)).fetchall()]
            out["events"] = EventWriter(conn).publish(ev, decided_by=by)
            if payload.get("candidate_id"):
                conn.execute("UPDATE agent_candidate SET outcome = 'published', outcome_reason = %s WHERE id = %s",
                             (f"approved by {by}", payload["candidate_id"]))
        else:
            if fact is not None:
                FactWriter(conn).reject([fact["id"]], str(reason), decided_by=by)
                ev = [r["id"] for r in conn.execute("SELECT id FROM event WHERE fact_id = %s AND status = 'candidate'",
                                                    (fact["id"],)).fetchall()]
                EventWriter(conn).reject(ev, str(reason), decided_by=by)
            if payload.get("candidate_id"):
                conn.execute("UPDATE agent_candidate SET outcome = 'rejected', outcome_reason = %s WHERE id = %s",
                             (f"rejected by {by}: {reason}", payload["candidate_id"]))
    elif item["kind"] == "event" and item["ref_type"] == "event":
        ids = [int(item["ref_id"])]
        if action == "approve":
            out["published"] = EventWriter(conn).publish(ids, decided_by=by)
        elif action == "reject":
            EventWriter(conn).reject(ids, str(reason), decided_by=by)
        else:
            raise ValidationError("events are approved or rejected, not edited")
    elif item["kind"] == "link" and item["ref_type"] in ("agent_link", "site_link"):
        if action == "edit":
            raise ValidationError("links are approved or rejected, not edited")
        out |= _decide_link(conn, item, action == "approve", by)
    elif item["kind"] in ("note", "issue"):  # the operations agents' items (marden.agents.ops.decisions)
        from marden.agents.ops.decisions import decide_ops

        out |= decide_ops(conn, item, action, by, reason)
    else:
        raise ValidationError(f"no decision rule for review items of kind {item['kind']} / {item['ref_type']}")
    _close(conn, item["id"], "approved" if action in ("approve", "edit") else "rejected", by)
    _audit(conn, by, f"review.{action}", f"review_item:{item['id']}", {"reason": reason, **out})
    return out


def _decide_link(conn: db.Conn, item: dict[str, Any], approve: bool, by: str) -> dict[str, Any]:
    status = "accepted" if approve else "rejected"
    if item["ref_type"] == "agent_link":
        conn.execute("UPDATE agent_link SET status = %s, decided_by = %s, decided_at = now() WHERE id = %s",
                     (status, by, int(item["ref_id"])))
        released = release_after_link(conn, int(item["ref_id"]), by) if approve else []
        return {"link": status, "released_facts": released}
    site, _, campus = item["ref_id"].partition("|")
    row = conn.execute(
        """UPDATE site_link SET status = %s, approved_by = %s, approved_at = now()
           WHERE site_slug = %s AND campus_id = %s RETURNING site_slug, campus_id""",
        ("approved" if approve else "rejected", by, site, campus)).fetchone()
    if row is not None:
        from marden.readmodels.deps import mark_dirty_for

        mark_dirty_for("site", row["site_slug"], "site_link", conn=conn, reason=f"review:{item['id']}")
        mark_dirty_for("campus", row["campus_id"], "site_link", conn=conn, reason=f"review:{item['id']}")
    return {"site_link": "approved" if approve else "rejected"}


def apply_pending(limit: int = 50) -> list[dict[str, Any]]:
    """Apply decisions the API recorded, oldest first; a failed one is closed with its error."""
    results = []
    with db.transaction() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM agent_decision WHERE applied_at IS NULL ORDER BY id LIMIT %s", (limit,)).fetchall()]
    for decision_id in ids:
        try:
            with db.transaction() as conn:
                d = conn.execute("""SELECT * FROM agent_decision WHERE id = %s AND applied_at IS NULL
                                    FOR UPDATE SKIP LOCKED""",
                                 (decision_id,)).fetchone()
                if d is None:
                    continue
                out = apply_decision(conn, d)
                conn.execute("UPDATE agent_decision SET applied_at = now(), result = %s WHERE id = %s",
                             (jsonb({"ok": True, **out}), decision_id))
                results.append({"id": decision_id, "ok": True, **out})
        except (ValidationError, ValueError, KeyError) as exc:
            with db.transaction() as conn:
                conn.execute("UPDATE agent_decision SET applied_at = now(), error = %s, result = %s WHERE id = %s",
                             (str(exc)[:500], jsonb({"ok": False}), decision_id))
            results.append({"id": decision_id, "ok": False, "error": str(exc)[:500]})
    return results


def decide(review_item_id: int, action: str, by: str, *, reason: str | None = None,
           edit: dict[str, Any] | None = None) -> dict[str, Any]:
    """Record and apply one decision now (CLI and tests); the API records, the worker applies."""
    with db.transaction() as conn:
        row = conn.execute(
            """INSERT INTO agent_decision (review_item_id, action, decided_by, reason, edit)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (review_item_id, action, by, reason, jsonb(edit) if edit is not None else None)).fetchone()
    assert row is not None
    done = [r for r in apply_pending() if r["id"] == row["id"]]
    return done[0] if done else {"id": row["id"], "ok": False, "error": "not applied"}
