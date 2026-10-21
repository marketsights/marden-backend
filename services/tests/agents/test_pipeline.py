"""One document through the agents with recorded model answers: route, extract, check, verify, link, publish.

Covers spec 22's accuracy layers end to end: hard check failures are rejected, disagreement goes to review,
facts on a proposed subject wait until a person accepts the link (then the routine ones publish and mark
read models dirty), exact identifiers publish by themselves, material and conflicting values wait, reruns
cost nothing and write nothing twice, and budgets stop a run honestly.
"""

from __future__ import annotations

import copy
from datetime import date

from agent_rows import (
    COREWEAVE_CIK,
    agree_all,
    make_llm,
    no_links,
    recorded,
    route_to,
    seed_coreweave,
    store_case_document,
)

from marden.agents.config import AgentSettings
from marden.agents.pipeline import enqueue_document, run_job
from marden.agents.review import decide
from marden.core import db
from marden.core.facts import FactWriter

DEBT = "debt_coreweave_8k_ddtl55"


def rows(sql, params=None):
    with db.transaction() as conn:
        return conn.execute(sql, params).fetchall()


def link_ddtl(evidence: str = "providing for a $2.6 billion delayed draw term loan facility"):
    return lambda req: {"links": [{
        "mention": "DDTL 5.5 Facility", "subject_type": "instrument", "subject_id": "EX-CRWV-097", "relation": "is",
        "related_type": None, "related_id": None, "evidence": evidence, "confidence": 0.9}]}


def test_debt_8k_end_to_end_with_review_and_link_release():
    seed_coreweave()
    doc = store_case_document(DEBT, meta={"cik": COREWEAVE_CIK})
    job = enqueue_document(doc.id)
    answers = {"router": route_to("debt"), "extractor": lambda r: recorded(DEBT), "verifier": agree_all,
               "linker": link_ddtl()}
    llm, backend = make_llm(answers)
    out = run_job(job, llm)
    assert out.state == "needs_review"
    assert backend.calls == ["router", "extractor:debt", "verifier", "linker"]
    cands = rows("SELECT * FROM agent_candidate WHERE job_id = %s ORDER BY id", (job,))
    assert len(cands) == 18 and {c["subject_id"] for c in cands} == {"EX-CRWV-097"}
    assert all(c["outcome"] == "review" and "link proposed" in c["outcome_reason"] for c in cands)
    facts = rows("SELECT * FROM fact WHERE document_id = %s", (doc.id,))
    assert len(facts) == 18 and {f["status"] for f in facts} == {"candidate"}
    amount = next(f for f in facts if f["field"] == "amount_usd")
    assert amount["value"] == 2600000000 and amount["tag"] == "F" and amount["producer"] == "agent:debt"
    assert amount["method_version"] == "agents v0.1 debt" and amount["verifier"] == "agree"
    assert "2.6 billion" in amount["quote"]
    link = rows("SELECT * FROM agent_link WHERE subject_id = 'EX-CRWV-097'")
    assert len(link) == 1 and link[0]["status"] == "proposed" and link[0]["method"] == "agent"
    item = rows("SELECT id FROM review_item WHERE kind = 'link' AND ref_type = 'agent_link' AND ref_id = %s",
                (str(link[0]["id"]),))[0]
    assert rows("SELECT count(*) AS n FROM review_item WHERE kind = 'fact' AND status = 'open'")[0]["n"] == 18

    result = decide(item["id"], "approve", "staff_user")
    assert result["ok"] and len(result["released_facts"]) == 18
    assert {f["status"] for f in rows("SELECT status FROM fact WHERE document_id = %s", (doc.id,))} == {"published"}
    dirty = rows("SELECT view FROM read_model_dirty WHERE key = 'EX-CRWV-097'")
    assert "instrument_detail" in {d["view"] for d in dirty}
    assert rows("SELECT count(*) AS n FROM review_item WHERE kind = 'fact' AND status = 'open'")[0]["n"] == 0

    # A rerun is free (every call cached; the mention now resolves through the accepted link, so the linker
    # model is not asked again) and writes nothing twice.
    llm2, backend2 = make_llm(answers)
    again = run_job(job, llm2)
    assert backend2.calls == [] and llm2.cache_hits == 3 and again.state == "published"
    assert len(rows("SELECT id FROM fact WHERE document_id = %s", (doc.id,))) == 18
    assert len(rows("SELECT id FROM agent_candidate WHERE job_id = %s", (job,))) == 18


def test_filer_identifier_publishes_routine_facts_and_holds_material_or_conflicting_ones():
    seed_coreweave()
    case_id = "periodic_coreweave_10q_liquidity"
    doc = store_case_document(case_id, meta={"cik": COREWEAVE_CIK}, doc_type="10-Q")
    answer = recorded(case_id)  # the excerpt never names the company: the model calls it the Registrant
    llm, _ = make_llm({"router": route_to("periodic"), "extractor": lambda r: answer, "verifier": agree_all,
                       "linker": no_links})
    out = run_job(enqueue_document(doc.id), llm)
    assert out.state == "published", out
    fact = rows("SELECT * FROM fact WHERE document_id = %s", (doc.id,))[0]
    assert fact["status"] == "published" and fact["subject_id"] == "coreweave" and fact["value"] == 5524000000
    link = rows("SELECT method, status FROM agent_link WHERE subject_id = 'coreweave'")[0]
    assert link == {"method": "identifier", "status": "accepted"}



def test_hard_failures_are_rejected_and_disagreements_and_ratings_wait():
    seed_coreweave()
    case_id = "ratings_coreweave_ddtl55_release"
    doc = store_case_document(case_id, meta={"cik": COREWEAVE_CIK})
    answer = copy.deepcopy(recorded(case_id))
    invented = copy.deepcopy(answer["facts"][0])
    invented["quote"] = "The facility received ratings of Baa1 from Moody's"
    invented["value_text"] = "Moody's Baa1"
    answer["facts"].append(invented)

    def verifier(req):
        out = agree_all(req)
        out["verdicts"][0]["verdict"] = "disagree"
        out["verdicts"][0]["corrected_value"] = "Ba2"
        return out

    issued = "The DDTL 5.5 Facility was issued through CoreWeave Financing DDTL V-V, LLC."
    llm, _ = make_llm({"router": route_to("ratings"), "extractor": lambda r: answer, "verifier": verifier,
                       "linker": link_ddtl(issued)})
    out = run_job(enqueue_document(doc.id), llm)
    assert out.state == "needs_review"
    cands = rows("SELECT * FROM agent_candidate WHERE document_id = %s ORDER BY id", (doc.id,))
    rejected = [c for c in cands if c["outcome"] == "rejected"]
    assert len(rejected) == 1 and "quote_found" in rejected[0]["outcome_reason"] and rejected[0]["fact_id"] is None
    held = [c for c in cands if c["outcome"] == "review"]
    assert held and all(c["material"] for c in held if c["field"] in ("rating", "rating_action"))
    assert any("verifier: disagree" in c["outcome_reason"] for c in held)
    events = rows("SELECT kind, status, material FROM event WHERE document_id = %s", (doc.id,))
    assert events and {e["kind"] for e in events} == {"rating"} and {e["status"] for e in events} == {"candidate"}


def test_conflicting_value_for_the_same_date_goes_to_review():
    seed_coreweave()
    case_id = "periodic_coreweave_10q_liquidity"
    with db.transaction() as conn:
        FactWriter(conn).add("entity", "coreweave", "cash_usd", 5600000000, tag="F", source_name="SEC XBRL",
                             source_url="https://data.sec.gov/test", valid_from=date(2026, 6, 30),
                             producer="collector:sec_xbrl", status="published", unit="USD")
    doc = store_case_document(case_id, meta={"cik": COREWEAVE_CIK}, doc_type="10-Q")
    llm, _ = make_llm({"router": route_to("periodic"), "extractor": lambda r: recorded(case_id),
                       "verifier": agree_all, "linker": no_links})
    out = run_job(enqueue_document(doc.id), llm)
    assert out.state == "needs_review"
    cand = rows("SELECT outcome, outcome_reason FROM agent_candidate WHERE document_id = %s", (doc.id,))[0]
    assert cand["outcome"] == "review"
    assert "conflict with current F value 5600000000 from SEC XBRL" in cand["outcome_reason"]


def test_budget_stops_a_run_and_nothing_is_published():
    doc = store_case_document(DEBT)
    llm, backend = make_llm({"router": route_to("debt"), "extractor": lambda r: recorded(DEBT),
                             "verifier": agree_all, "linker": no_links}, AgentSettings(run_tokens=6000))
    out = run_job(enqueue_document(doc.id), llm)
    assert out.state == "budget" and "budget" in out.detail
    assert backend.calls == ["router"]
    assert rows("SELECT count(*) AS n FROM fact WHERE document_id = %s", (doc.id,))[0]["n"] == 0
