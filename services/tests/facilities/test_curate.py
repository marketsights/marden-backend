"""Staff curation of campus financing (marden.facilities): links, financed sites and financing notes.

Every value cites a document stored raw first and a quote found verbatim in it; links are approved through
the review queue (agent_decision, audit_log, site_link.approved_by) as the 21 archive links were; the campus
row carries the financing note (types.ts DataCenterRow.financing) when no debt is linked.
"""

from __future__ import annotations

import json

import pytest

from marden import cli
from marden.agents.intake import ensure_intake_sources
from marden.core import db, documents
from marden.core.checks import ValidationError
from marden.facilities.curate import (
    NOTE_METHOD,
    add_financing_note,
    apply_spec,
    approve_link,
    propose_link,
    withdraw_link,
)
from marden.readmodels import business_today
from marden.readmodels.context import building
from marden.readmodels.registry import get_builder
from marden.readmodels.views import facilities_campus  # noqa: F401 (registers builders)

FILING = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/ex99.htm"
PRESS = "https://news.example.test/loan"
FILING_TEXT = (b"<html><body><p>The Dalton 1 campus located at 2205 Industrial South Rd in Dalton, Georgia "
               b"will feature a 30 MW data center.</p><p>The Notes will be fully and unconditionally guaranteed "
               b"by Core Scientific Dalton LLC.</p><p>Our liquidity will continue to be sufficient to fund our "
               b"operating activities.</p></body></html>")
PRESS_TEXT = (b"<html><body><p>Chirisa received $200 million in construction funding to build CoreWeave's "
              b"turnkey data center.</p></body></html>")


def rows(sql: str, params: tuple = ()) -> list[dict]:
    with db.transaction() as conn:
        return conn.execute(sql, params).fetchall()


@pytest.fixture
def stored():
    with db.transaction() as conn:
        ensure_intake_sources(conn)
        conn.execute("INSERT INTO site (slug, name) VALUES ('corz', 'Core Scientific CoreWeave sites')")
        conn.execute("INSERT INTO campus (id, name, state) VALUES ('dalton', 'CoreWeave Dalton 1 & 2', 'GA')")
        conn.execute("INSERT INTO campus (id, name, state) VALUES ('chester', 'CoreWeave Chester VA', 'VA')")
        conn.execute("INSERT INTO campus (id, name, state) VALUES ('arcola', 'Google Arcola', 'VA')")
    filing = documents.record_document("agent_url", FILING, FILING_TEXT, content_type="text/html")
    press = documents.record_document("agent_url", PRESS, PRESS_TEXT, content_type="text/html")
    return {"filing": filing.id, "press": press.id}


def _dc_row(campus: str) -> dict:
    with building(business_today()), db.transaction() as conn:
        return get_builder("dc_row").build_one(conn, campus)


def test_a_link_needs_a_stored_source_and_a_quote_in_it(stored):
    with pytest.raises(ValidationError, match="is not stored"), db.transaction() as conn:
        propose_link(conn, "corz", "dalton", url="https://unstored.test/x", quote="anything", tag="F", basis="b")
    with pytest.raises(ValidationError, match="quote not in the stored copy"), db.transaction() as conn:
        propose_link(conn, "corz", "dalton", url=FILING, quote="located at 9 Elsewhere Rd", tag="F", basis="b")
    with pytest.raises(ValidationError, match="does not exist"), db.transaction() as conn:
        propose_link(conn, "corz", "nowhere", url=FILING, quote="Dalton 1 campus", tag="F", basis="b")
    assert rows("SELECT * FROM site_link") == [] and rows("SELECT * FROM fact") == []


def test_propose_then_approve_through_the_review_queue(stored):
    quote = ["The Dalton 1 campus located at 2205 Industrial South Rd in Dalton, Georgia",
             "guaranteed by Core Scientific Dalton LLC"]
    with db.transaction() as conn:
        out = propose_link(conn, "corz", "dalton", url=FILING, quote=quote, tag="F", basis="address and guarantor")
    assert out["site_link"] == "proposed" and out["review_status"] == "open"
    fact = rows("SELECT * FROM fact WHERE id = %s", (out["evidence_fact_id"],))[0]
    assert (fact["subject_type"], fact["subject_id"], fact["field"], fact["tag"]) == (
        "campus", "dalton", "site_link_evidence", "F")
    assert fact["document_id"] == stored["filing"] and fact["source_url"] == FILING
    assert fact["quote"] == " ... ".join(quote) and fact["source_name"] == "SEC EDGAR"
    assert fact["value"] == {"site_slug": "corz", "campus_id": "dalton", "basis": "address and guarantor"}
    assert _dc_row("dalton")["financedSites"] == []  # proposed links never show

    with db.transaction() as conn:  # proposing again changes nothing
        again = propose_link(conn, "corz", "dalton", url=FILING, quote=quote, tag="F", basis="address and guarantor")
    assert again["site_link"] == "exists" and again["evidence_fact_id"] == out["evidence_fact_id"]

    note = "Address 2205 Industrial South Rd = campus address; Core Scientific Dalton LLC guarantees the notes."
    done = approve_link("corz", "dalton", by="claude-review", reason=note)
    assert done["ok"] and done["site_link"] == "approved"
    link = rows("SELECT status, approved_by, evidence_fact_id FROM site_link")[0]
    assert link == {"status": "approved", "approved_by": "claude-review", "evidence_fact_id": out["evidence_fact_id"]}
    audit = rows("SELECT action, meta FROM audit_log WHERE user_id = 'claude-review'")
    assert [a["action"] for a in audit] == ["review.approve"] and audit[0]["meta"]["reason"] == note
    assert ("dc_row", "dalton") in {(r["view"], r["key"]) for r in rows("SELECT view, key FROM read_model_dirty")}
    assert [s["siteSlug"] for s in _dc_row("dalton")["financedSites"]] == ["corz"]
    assert approve_link("corz", "dalton", by="claude-review", reason=note)["already"] == "approved"


def test_withdraw_an_approved_link_through_the_review_queue(stored):
    with db.transaction() as conn:
        propose_link(conn, "corz", "dalton", url=FILING, quote="Dalton 1 campus", tag="F", basis="address")
    approve_link("corz", "dalton", by="claude-review", reason="address match")
    assert [s["siteSlug"] for s in _dc_row("dalton")["financedSites"]] == ["corz"]
    with pytest.raises(ValidationError, match="reason"):
        withdraw_link("corz", "dalton", by="claude-review", reason="")
    with pytest.raises(ValidationError, match="no link"):
        withdraw_link("corz", "chester", by="claude-review", reason="wrong")
    out = withdraw_link("corz", "dalton", by="claude-review", reason="tax-abatement bond-lease, not debt")
    assert out["ok"] and out["site_link"] == "rejected"
    link = rows("SELECT status, approved_by FROM site_link")[0]
    assert link == {"status": "rejected", "approved_by": "claude-review"}
    assert rows("SELECT status FROM review_item WHERE ref_id = 'corz|dalton'")[0]["status"] == "rejected"
    audit = [a["action"] for a in rows("SELECT action FROM audit_log WHERE user_id = 'claude-review' ORDER BY id")]
    assert audit == ["review.approve", "review.reopen", "review.reject"]
    assert len(rows("SELECT * FROM fact WHERE field = 'site_link_evidence'")) == 1  # the evidence stays as history
    assert _dc_row("dalton")["financedSites"] == []
    assert withdraw_link("corz", "dalton", by="claude-review", reason="again")["already"] == "rejected"


def _spec() -> dict:
    return {
        "approve_by": "claude-review",
        "sites": [{"slug": "chirisa-chester", "name": "Chirisa CoreWeave Chester", "state": "VA",
                   "county": "Chesterfield", "tracking_names": ["Chirisa Technology Parks"],
                   "chain": [{"role": "tenant", "label": "CoreWeave"},
                             {"role": "lenders", "label": "Brookfield fund"}]}],
        "instruments": [{"id": "MAN-TEST-200", "name": "$200M construction loan", "kind": "construction_loan",
                         "issuer_name": "Chirisa (borrower not disclosed)", "site_slug": "chirisa-chester",
                         "facts": [{"field": "amount_usd", "value": 200000000, "tag": "R", "url": PRESS,
                                    "quote": "Chirisa received $200 million in construction funding",
                                    "valid_from": "2023-09-20", "unit": "USD"}]}],
        "links": [{"site": "chirisa-chester", "campus": "chester", "tag": "R", "url": PRESS,
                   "quote": "build CoreWeave's turnkey data center", "basis": "CoreWeave building",
                   "reason": "Same building and tenant."}],
    }


def test_apply_writes_a_financed_site_with_quoted_debt_and_is_idempotent(stored):
    out = apply_spec(_spec())
    assert (out["sites"], out["instruments"], out["facts"]) == (1, 1, 1)
    assert out["links"][0]["decision"]["site_link"] == "approved"
    assert rows("SELECT role, label FROM chain_party ORDER BY role") == [
        {"role": "lenders", "label": "Brookfield fund"}, {"role": "tenant", "label": "CoreWeave"}]
    inst = rows("SELECT site_slug, kind FROM instrument WHERE id = 'MAN-TEST-200'")[0]
    assert inst == {"site_slug": "chirisa-chester", "kind": "construction_loan"}
    amount = rows("SELECT value, tag, document_id, quote FROM fact WHERE field = 'amount_usd'")[0]
    assert (amount["value"], amount["tag"], amount["document_id"]) == (200000000, "R", stored["press"])
    sites = _dc_row("chester")["financedSites"]
    assert [(s["siteSlug"], s["debtB"]["value"]) for s in sites] == [("chirisa-chester", 0.2)]

    before = {t: rows(f"SELECT count(*) AS n FROM {t}")[0]["n"] for t in ("fact", "site_link", "review_item",
                                                                           "audit_log")}
    apply_spec(_spec())  # a rerun writes nothing new and re-approves nothing
    assert {t: rows(f"SELECT count(*) AS n FROM {t}")[0]["n"] for t in before} == before


def test_apply_refuses_an_unsourced_value_and_writes_nothing(stored):
    spec = _spec()
    spec["instruments"][0]["facts"].append({"field": "coupon", "value": "S+250", "tag": "R", "url": PRESS,
                                            "quote": "S+250 bp", "valid_from": "2023-09-20"})
    with pytest.raises(ValidationError, match="quote not in the stored copy"):
        apply_spec(spec)
    assert rows("SELECT * FROM site WHERE slug = 'chirisa-chester'") == [] and rows("SELECT * FROM fact") == []


def test_the_financing_note_is_on_the_campus_row(stored):
    with pytest.raises(ValidationError, match="names the owner"), db.transaction() as conn:
        add_financing_note(conn, "arcola", kind="owner_funded", note="n", url=FILING, quote="liquidity")
    with pytest.raises(ValidationError, match="kind"), db.transaction() as conn:
        add_financing_note(conn, "arcola", kind="unknown", note="n", url=FILING, quote="liquidity")
    with db.transaction() as conn:
        fid = add_financing_note(conn, "arcola", kind="owner_funded", owner="Alphabet", payer_id="google",
                                 note="Built on Alphabet's balance sheet; no asset-level debt found.",
                                 url=FILING, quote="Our liquidity will continue to be sufficient",
                                 searched=["EDGAR full-text search", "county land records"])
    fact = rows("SELECT tag, method_version, document_id, field FROM fact WHERE id = %s", (fid,))[0]
    assert fact == {"tag": "M", "method_version": NOTE_METHOD, "document_id": stored["filing"],
                    "field": "financing_note"}
    row = _dc_row("arcola")
    fin = row["financing"]
    assert fin["tag"] == "M" and fin["sourceUrl"] == FILING and fin["methodVersion"] == NOTE_METHOD
    assert fin["value"] == {"kind": "owner_funded", "owner": "Alphabet", "payerId": "google",
                            "note": "Built on Alphabet's balance sheet; no asset-level debt found.",
                            "searched": ["EDGAR full-text search", "county land records"]}
    assert row["financedSites"] == []
    assert _dc_row("dalton")["financing"] is None  # no note: null, never a placeholder
    with building(business_today()), db.transaction() as conn:
        detail = get_builder("dc_detail").build_one(conn, "arcola")
    assert detail["financing"] == fin and "Financing note" not in detail["facts"]  # shown once, not in the table


def test_cli_link_and_note(stored, tmp_path, capsys):
    argv = ["facilities", "link", "propose", "--site", "corz", "--campus", "dalton", "--url", FILING,
            "--quote", "The Dalton 1 campus located at 2205 Industrial South Rd", "--tag", "F", "--basis", "address"]
    assert cli.main(argv) == 0
    assert cli.main(["facilities", "link", "propose", "--site", "corz", "--campus", "dalton"]) == 2
    assert cli.main(["facilities", "link", "approve", "--site", "corz", "--campus", "dalton", "--by", "claude-review",
                     "--reason", "address match"]) == 0
    assert rows("SELECT approved_by FROM site_link")[0]["approved_by"] == "claude-review"
    assert cli.main(["facilities", "note", "--campus", "arcola", "--kind", "not_found", "--note", "None found.",
                     "--url", FILING, "--quote", "not in the filing"]) == 1  # the quote is checked
    assert cli.main(["facilities", "note", "--campus", "arcola", "--kind", "not_found", "--note", "None found.",
                     "--url", FILING, "--quote", "will feature a 30 MW data center", "--searched", "EDGAR FTS"]) == 0
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(_spec()))
    assert cli.main(["facilities", "apply", str(spec)]) == 0
    assert '"site_link": "approved"' in capsys.readouterr().out
