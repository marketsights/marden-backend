"""Intake (continuous feeding, priorities, per-source caps, uploads) and staff decisions on the review queue."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from agent_rows import COREWEAVE_CIK, case, seed_coreweave, store_case_document

from marden.agents import config
from marden.agents.config import BackendStatus
from marden.agents.intake import claim, ingest_bytes, queue_documents, r2_inbox, reset_stale, store_uploads
from marden.agents.pipeline import enqueue_document
from marden.agents.review import apply_pending, decide
from marden.agents.runner import sweep, work
from marden.core import db, rawstore
from marden.core.facts import FactWriter
from marden.core.jsonutil import jsonb, sha256_hex


@pytest.fixture
def backend():
    def pin(kind: str):
        config.set_backend_status(BackendStatus(kind, f"test: {kind}"))

    yield pin
    config.set_backend_status(None)


def rows(sql, params=None):
    with db.transaction() as conn:
        return conn.execute(sql, params).fetchall()


def test_not_configured_queues_nothing_and_says_so(backend):
    backend("not_configured")
    store_case_document("debt_coreweave_8k_ddtl55")
    out = sweep()
    assert out["status"] == "not_configured" and out["queued"] == 0
    assert rows("SELECT count(*) AS n FROM agent_job")[0]["n"] == 0
    assert rows("SELECT backend, detail FROM agent_status")[0] == {"backend": "not_configured",
                                                                   "detail": "test: not_configured"}
    assert work()["status"] == "not_configured"
    assert r2_inbox()["status"] == "not_configured"


def test_sweep_queues_each_document_once_with_priorities(backend):
    backend("cli")
    seed_coreweave()
    eightk = store_case_document("debt_coreweave_8k_ddtl55", doc_type="8-K")
    watched = store_case_document("gpu_coreweave_q2_2026_release", meta={"cik": COREWEAVE_CIK}, doc_type="EX-99.1")
    with db.transaction() as conn:
        conn.execute("INSERT INTO org (id, name, plan) VALUES ('o1', 'o1', 'paid')")
        conn.execute("INSERT INTO watchlist_item (org_id, kind, ref_id) VALUES ('o1', 'instrument', 'EX-CRWV-097')")
        conn.execute("UPDATE instrument SET issuer_entity_id = 'coreweave' WHERE id = 'EX-CRWV-097'")
        conn.execute("UPDATE document SET doc_date = current_date - 90 WHERE id = %s", (eightk.id,))
    old = store_case_document("power_ferc_oklo_pjm", source_id="ferc")
    with db.transaction() as conn:
        conn.execute("UPDATE document SET doc_date = current_date - 90 WHERE id = %s", (old.id,))
    assert sweep()["queued"] == 3
    assert queue_documents() == 0  # idempotent
    jobs = {r["document_id"]: r for r in rows("SELECT * FROM agent_job")}
    assert (jobs[watched.id]["priority"], jobs[watched.id]["priority_reason"]) == (90, "watched subject")
    assert (jobs[eightk.id]["priority"], jobs[eightk.id]["priority_reason"]) == (80, "current report")
    assert (jobs[old.id]["priority"], jobs[old.id]["priority_reason"]) == (20, "backfill")


def test_claim_takes_priority_first_within_per_source_caps():
    docs = [store_case_document(c) for c in ("debt_coreweave_8k_ddtl55", "gpu_coreweave_q2_2026_release",
                                             "ratings_coreweave_ddtl55_release")]
    ferc = store_case_document("power_ferc_oklo_pjm", source_id="ferc")
    ids = [enqueue_document(d.id, priority=p) for d, p in zip(docs, (50, 60, 70), strict=True)]
    ferc_job = enqueue_document(ferc.id, priority=10)
    caps = {"sec_index": 2, "ferc": 1}.__getitem__
    first = claim(caps, limit=5)
    assert first == [ids[2], ids[1], ferc_job]  # sec_index capped at 2 running, ferc fills the third slot
    assert claim(caps, limit=5) == []
    with db.transaction() as conn:
        conn.execute("UPDATE agent_job SET updated_at = now() - interval '2 hours' WHERE id = %s", (ids[2],))
    assert reset_stale(datetime.now(UTC) + timedelta(minutes=1)) == 1
    with db.transaction() as conn:
        conn.execute("UPDATE agent_job SET state = 'published' WHERE id = %s", (ids[1],))
    assert claim(caps, limit=5) == [ids[2], ids[0]]  # nothing of sec_index running any more: two slots


def test_uploads_are_stored_raw_then_queued_with_their_links(backend):
    backend("cli")
    seed_coreweave()
    data = case("permits_tceq_182126").excerpt.encode()
    with db.transaction() as conn:
        up = conn.execute(
            """INSERT INTO agent_upload (filename, content_type, kind, size, sha256, data, uploaded_by, links, filed)
               VALUES ('182126-pls-english.txt', 'text/plain', 'txt', %s, %s, %s, 'staff_user', %s, true)
               RETURNING id""", (len(data), sha256_hex(data), data, jsonb({"entity": "coreweave"}))).fetchone()
    assert store_uploads() == 1
    stored = rows("SELECT * FROM agent_upload WHERE id = %s", (up["id"],))[0]
    assert stored["status"] == "stored" and stored["data"] is None
    doc = rows("SELECT * FROM document WHERE id = %s", (stored["document_id"],))[0]
    assert doc["source_id"] == "upload" and doc["extract_state"] == "queued" and doc["meta"]["filed"] is True
    assert rawstore.get(doc["storage_key"]) == data
    sweep()
    job = rows("SELECT * FROM agent_job WHERE document_id = %s", (doc["id"],))[0]
    assert job["links"] == {"entity": "coreweave"} and job["priority"] == 70
    with pytest.raises(ValueError, match="not a PDF"):
        ingest_bytes(b"\x00\x01\x02\xff", source_id="upload", url="upload://x", filename="x.bin")


def _fact_review(conn, value, quote, *, doc_id):
    w = FactWriter(conn)
    fid = w.add("entity", "coreweave", "cash_usd", value, tag="F", source_name="SEC EDGAR", valid_from="2026-06-30",
                producer="agent:periodic", unit="USD", document_id=doc_id, quote=quote,
                method_version="agents v0.1 periodic").id
    w.set_verifier(fid, "unsupported")
    return fid, w.request_review(fid, title="cash_usd", reason="verifier: unsupported", payload={"area": "periodic"})


def test_decisions_reject_edit_with_a_verbatim_quote_and_approve(backend):
    seed_coreweave()
    doc = store_case_document("periodic_coreweave_10q_liquidity", meta={"cik": COREWEAVE_CIK})
    with db.transaction() as conn:
        f1, r1 = _fact_review(conn, 5524000000, "Cash and cash equivalents $ 5,524 $ 3,127", doc_id=doc.id)
        f2, r2 = _fact_review(conn, 3127000000, "Cash and cash equivalents $ 5,524 $ 3,127", doc_id=doc.id)
    no_reason = decide(r2, "reject", "staff_user")
    assert not no_reason["ok"] and "reason" in no_reason["error"]
    bad_quote = decide(r2, "edit", "staff_user", edit={"value": 5524000000, "quote": "Cash was $5.5 billion"})
    assert not bad_quote["ok"] and "not in the document" in bad_quote["error"]
    wrong_number = decide(r2, "edit", "staff_user", edit={"value": 5525000000,
                                                         "quote": "Cash and cash equivalents $ 5,524 $ 3,127"})
    assert not wrong_number["ok"] and "number_in_quote" in wrong_number["error"]
    edited = decide(r2, "edit", "staff_user", edit={"value": 5524000000,
                                                   "quote": "Cash and cash equivalents $ 5,524 $ 3,127"})
    assert edited["ok"]
    new = rows("SELECT * FROM fact WHERE id = %s", (edited["fact_id"],))[0]
    assert new["status"] == "published" and new["producer"] == "review:staff_user" and new["document_id"] == doc.id
    assert rows("SELECT status FROM fact WHERE id = %s", (f2,))[0]["status"] == "rejected"
    approved = decide(r1, "approve", "staff_user")
    assert approved["ok"] and approved["published"] == [f1]
    assert decide(r1, "approve", "staff_user")["error"] == "review item already approved"
    actions = {r["action"] for r in rows("SELECT action FROM audit_log WHERE user_id = 'staff_user'")}
    assert {"review.edit", "review.approve", "fact.reject"} <= actions


def test_api_decisions_wait_for_the_worker_and_site_links_are_approved():
    with db.transaction() as conn:
        conn.execute("INSERT INTO site (slug, name) VALUES ('jupiter', 'Project Jupiter (Stargate New Mexico)')")
        conn.execute("INSERT INTO campus (id, name) VALUES ('jupiter-campus', 'Project Jupiter')")
        conn.execute("INSERT INTO site_link (site_slug, campus_id) VALUES ('jupiter', 'jupiter-campus')")
        item = conn.execute(
            """INSERT INTO review_item (kind, ref_type, ref_id, title, reason) VALUES ('link', 'site_link',
               'jupiter|jupiter-campus', 'Link site jupiter', 'material') RETURNING id""").fetchone()["id"]
        conn.execute("INSERT INTO agent_decision (review_item_id, action, decided_by) VALUES (%s, 'approve', 'u1')",
                     (item,))
    done = apply_pending()
    assert done == [{"id": done[0]["id"], "ok": True, "review_item_id": item, "action": "approve",
                     "site_link": "approved"}]
    assert rows("SELECT status, approved_by FROM site_link")[0] == {"status": "approved", "approved_by": "u1"}
    assert {"site_detail", "dc_row"} <= {r["view"] for r in rows("SELECT view FROM read_model_dirty")}
