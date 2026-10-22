"""A staff edit of an event-like field publishes the event with the corrected fact (spec 22 layer 5).

Real excerpt: the Doña Ana County BOCC amended agenda of October 13, 2026 (document 9882), whose item
"Presentation and Update by Project Jupiter Representatives" is an opposition step on Project Jupiter. Without
the event an edited hearing or opposition step would reach no screen (the site's opposition tab and the alert
engine read `event`, not the fact), and the original's candidate event would wait for ever.
"""

from __future__ import annotations

from agent_rows import store_case_document

from marden.agents.config import METHOD
from marden.agents.review import decide
from marden.core import db
from marden.core.facts import FactWriter
from marden.core.records import EventWriter

QUOTE = "Presentation and Update by Project Jupiter Representatives"
BODY = "Doña Ana County Board of County Commissioners"


def rows(sql, params=None):
    with db.transaction() as conn:
        return conn.execute(sql, params).fetchall()


def _candidate(field: str, value, area: str, event_kind: str, event_date: str) -> tuple[int, int, int, int]:
    """A material candidate fact on site jupiter with its candidate event, in review (as publish writes them)."""
    doc = store_case_document("local_dona_ana_bocc_2026_10_13", source_id="agent_url")
    with db.transaction() as conn:
        conn.execute("INSERT INTO site (slug, name) VALUES ('jupiter', 'Project Jupiter (Stargate New Mexico)') "
                     "ON CONFLICT (slug) DO NOTHING")
        w = FactWriter(conn)
        fid = w.add("site", "jupiter", field, value, tag="F", source_name="donaanaconm.api.civicclerk.com",
                    valid_from="2026-10-13", producer=f"agent:{area}", document_id=doc.id, quote=QUOTE,
                    method_version=f"{METHOD} {area}", material=True).id
        w.set_verifier(fid, "agree")
        eid = EventWriter(conn).add("site", "jupiter", event_date, event_kind, f"{field}: {value}", tag="F",
                                    source_name="donaanaconm.api.civicclerk.com", material=True,
                                    document_id=doc.id, fact_id=fid).id
        item = w.request_review(fid, title=field, reason="material: alert_event",
                                payload={"area": area, "document_id": doc.id, "field": field})
    return doc.id, fid, eid, item


def test_an_edited_opposition_step_publishes_its_event_and_rejects_the_original_one():
    wrong = {"kind": "presentation", "body": BODY, "date": "2026-10-31", "outcome": "on the agenda"}
    doc_id, fid, old_event, item = _candidate("opposition_event", wrong, "local", "opposition", "2026-10-31")
    fixed = {**wrong, "date": "2026-10-13"}
    out = decide(item, "edit", "staff_user", edit={"value": fixed, "quote": QUOTE})
    assert out["ok"], out
    new = rows("SELECT * FROM fact WHERE id = %s", (out["fact_id"],))[0]
    assert new["status"] == "published" and new["value"] == fixed and new["document_id"] == doc_id
    events = rows("SELECT * FROM event WHERE fact_id = %s", (new["id"],))
    assert len(events) == 1 and out["events"] == [events[0]["id"]]
    ev = events[0]
    assert (ev["kind"], str(ev["event_date"]), ev["status"], ev["subject_id"]) == (
        "opposition", "2026-10-13", "published", "jupiter")
    assert ev["document_id"] == doc_id and ev["tag"].strip() == "F"
    assert rows("SELECT status FROM event WHERE id = %s", (old_event,))[0]["status"] == "rejected"
    assert rows("SELECT status FROM fact WHERE id = %s", (fid,))[0]["status"] == "rejected"
    # the site's screens are marked for the new event, so the opposition tab and alerts see it
    assert rows("SELECT count(*) AS n FROM read_model_dirty WHERE key = 'jupiter'")[0]["n"] > 0
    assert {"review.edit", "event.reject"} <= {r["action"] for r in rows("SELECT action FROM audit_log")}


def test_a_permits_hearing_refiled_as_an_opposition_step_takes_the_local_fields_event():
    """The field changes area ('permits' hearing_date -> 'local' opposition_event): the edit is checked
    against the local vocabulary and writes the local field's event kind."""
    _doc, _fid, old_event, item = _candidate("hearing_date", "2026-10-13", "permits", "permit", "2026-10-13")
    value = {"kind": "presentation", "body": BODY, "date": "2026-10-13", "outcome": "on the agenda"}
    out = decide(item, "edit", "staff_user", edit={"value": value, "quote": QUOTE, "field": "opposition_event"})
    assert out["ok"], out
    ev = rows("SELECT * FROM event WHERE fact_id = %s", (out["fact_id"],))[0]
    assert (ev["kind"], ev["status"]) == ("opposition", "published")
    assert rows("SELECT status FROM event WHERE id = %s", (old_event,))[0]["status"] == "rejected"


def test_an_edit_that_fails_the_checks_writes_no_event():
    _doc, _fid, old_event, item = _candidate("hearing_date", "2026-10-31", "local", "opposition", "2026-10-31")
    out = decide(item, "edit", "staff_user", edit={"value": "2026-10-31", "quote": QUOTE})
    assert not out["ok"]  # the date's month and year are not in the quote
    assert rows("SELECT status FROM event WHERE id = %s", (old_event,))[0]["status"] == "candidate"
    assert rows("SELECT count(*) AS n FROM event WHERE status = 'published'")[0]["n"] == 0
