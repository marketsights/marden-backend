from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from marden.core.checks import ValidationError, latest_today
from marden.core.facts import FactWriter, current_facts, fact_dedupe_key

URL = "https://www.tceq.texas.gov/permitting/air"


def _add(writer: FactWriter, **overrides):
    args = {
        "subject_type": "site",
        "subject_id": "abilene",
        "field": "facility_mw",
        "value": 1200,
        "tag": "F",
        "source_name": "TCEQ",
        "source_url": URL,
        "valid_from": date(2026, 9, 1),
        "producer": "collector:tceq",
    }
    args.update(overrides)
    positional = [args.pop(k) for k in ("subject_type", "subject_id", "field", "value")]
    return writer.add(*positional, **args)


def _dirty(conn) -> set[tuple[str, str]]:
    return {(r["view"], r["key"]) for r in conn.execute("SELECT view, key FROM read_model_dirty")}


def test_add_twice_writes_one_row(conn):
    w = FactWriter(conn)
    first = _add(w)
    second = _add(w)
    assert first.created and not second.created
    assert first.id == second.id and first.dedupe_key == second.dedupe_key
    assert conn.execute("SELECT count(*) AS n FROM fact").fetchone()["n"] == 1


def test_equal_numbers_share_a_dedupe_key(conn):
    w = FactWriter(conn)
    a = _add(w, value=2200)
    b = _add(w, value=2200.0)
    c = _add(w, value=Decimal("2200.00"))
    assert a.id == b.id == c.id


def test_each_key_part_makes_a_new_fact(conn):
    w = FactWriter(conn)
    base = _add(w)
    variants = [
        {"value": 1201},
        {"tag": "R"},
        {"source_url": URL + "?p=2"},
        {"valid_from": date(2026, 9, 2)},
        {"producer": "seed:aidebt"},
        {"field": "it_mw"},
        {"subject_id": "jupiter"},
    ]
    ids = {base.id} | {_add(w, **v).id for v in variants}
    assert len(ids) == len(variants) + 1


def test_dedupe_key_uses_source_name_without_url():
    k1 = fact_dedupe_key("site", "a", "f", 1, "R", "Reuters", date(2026, 1, 1), "seed:x")
    k2 = fact_dedupe_key("site", "a", "f", 1, "R", "Bloomberg", date(2026, 1, 1), "seed:x")
    assert k1 != k2 and len(k1) == 64


def test_dict_values_are_canonical(conn):
    w = FactWriter(conn)
    a = _add(w, field="lenders", value={"lead": "JPMorgan", "others": ["MUFG"]})
    b = _add(w, field="lenders", value={"others": ["MUFG"], "lead": "JPMorgan"})
    assert a.id == b.id
    stored = conn.execute("SELECT value FROM fact WHERE id = %s", (a.id,)).fetchone()["value"]
    assert stored == {"lead": "JPMorgan", "others": ["MUFG"]}


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"tag": "X"}, "tag must be"),
        ({"tag": "M"}, "method_version"),
        ({"producer": "model:cushion@0.1", "tag": "F"}, "model producers"),
        ({"producer": "extractor:8k@1", "document_id": 1}, "quote"),
        ({"producer": "extractor:8k@1", "quote": "1,200 MW"}, "document_id"),
        ({"valid_from": latest_today() + timedelta(days=1)}, "in the future"),
        ({"valid_from": "2026-13-01"}, "ISO date"),
        ({"field": "FacilityMW"}, "snake_case"),
        ({"subject_type": "galaxy"}, "subject_type"),
        ({"value": None}, "needs a value"),
        ({"value": float("nan")}, "not JSON"),
        ({"producer": "tceq"}, "producer"),
        ({"source_name": " "}, "source_name"),
        ({"confidence": 1.5}, "confidence"),
        ({"status": "superseded"}, "status"),
        ({"material": True, "status": "published"}, "material"),
    ],
)
def test_validators_refuse(conn, overrides, message):
    with pytest.raises(ValidationError, match=message):
        _add(FactWriter(conn), **overrides)
    assert conn.execute("SELECT count(*) AS n FROM fact").fetchone()["n"] == 0


def test_extracted_fact_needs_verification_before_publish(conn):
    conn.execute("INSERT INTO source (id, name, kind, terms_note) VALUES ('sec_test', 'SEC', 'api', 'test')")
    doc = conn.execute(
        """INSERT INTO document (source_id, url, sha256, storage_key)
           VALUES ('sec_test', 'https://www.sec.gov/x', repeat('a', 64), 'raw/sec_test/aa/x') RETURNING id"""
    ).fetchone()
    w = FactWriter(conn)
    with pytest.raises(ValidationError, match="published after verification"):
        _add(w, producer="extractor:8k@1", quote="1,200 MW", document_id=doc["id"], status="published")
    fact = _add(w, producer="extractor:8k@1", quote="1,200 MW", document_id=doc["id"])
    with pytest.raises(ValidationError, match="need a person"):
        w.publish([fact.id])
    w.set_verifier(fact.id, "agree")
    assert w.publish([fact.id]) == [fact.id]


def test_valid_from_today_is_accepted(conn):
    assert _add(FactWriter(conn), valid_from=date.today()).created


def test_published_add_marks_dependents_dirty(conn):
    w = FactWriter(conn)
    _add(w, status="published")
    dirty = _dirty(conn)
    assert {("site_detail", "abilene"), ("sites", "all"), ("completion", "all")} <= dirty


def test_candidate_does_not_mark_until_published(conn):
    w = FactWriter(conn)
    fact = _add(w)
    assert _dirty(conn) == set()
    assert w.publish([fact.id]) == [fact.id]
    assert ("site_detail", "abilene") in _dirty(conn)
    assert w.publish([fact.id]) == []  # already published: no change
    row = conn.execute("SELECT status FROM fact WHERE id = %s", (fact.id,)).fetchone()
    assert row["status"] == "published"


def test_material_publish_needs_a_person_and_closes_review(conn):
    w = FactWriter(conn)
    fact = _add(w, material=True)
    review_id = w.request_review(fact.id, title="Abilene 1.2 GW", reason="material: changes cushion band")
    assert w.request_review(fact.id, title="again", reason="again") == review_id
    with pytest.raises(ValidationError, match="need a person"):
        w.publish([fact.id])
    assert w.publish([fact.id], decided_by="user_cody") == [fact.id]
    review = conn.execute("SELECT status, decided_by FROM review_item WHERE id = %s", (review_id,)).fetchone()
    assert review == {"status": "approved", "decided_by": "user_cody"}


def test_reject_records_reason_and_skips_published(conn):
    w = FactWriter(conn)
    cand = _add(w)
    pub = _add(w, value=1300, status="published")
    assert w.reject([cand.id, pub.id], "quote does not support the value", decided_by="user_cody") == [cand.id]
    statuses = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM fact")}
    assert statuses == {cand.id: "rejected", pub.id: "published"}
    audit = conn.execute("SELECT action, target, meta FROM audit_log").fetchall()
    assert audit == [{"action": "fact.reject", "target": f"fact:{cand.id}",
                      "meta": {"reason": "quote does not support the value"}}]
    with pytest.raises(ValidationError):
        w.reject([cand.id], "")


def test_current_facts_reads_published_only(conn):
    w = FactWriter(conn)
    _add(w, value=1100)
    _add(w, value=1200, status="published", valid_from=date(2026, 9, 2))
    cur = current_facts(conn, "site", "abilene")
    assert cur["facility_mw"]["value"] == 1200
