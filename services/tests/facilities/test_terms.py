"""Instrument terms and lenders from stored documents (marden.facilities.terms) and how instrument_detail shows
them (Terms card rows, n/d reasons, the lender group).

Every value cites a document stored with its text and a quote found in it; F only for a filed (SEC) document;
`replaces` supersedes the old fact (never deleted); `not_disclosed` says why a term stays n/d.
"""

from __future__ import annotations

from datetime import date

import pytest

from marden.agents.intake import ensure_intake_sources
from marden.core import db, documents
from marden.core.checks import ValidationError
from marden.core.facts import FactWriter
from marden.facilities.terms import apply_terms, is_filed, lender_field
from marden.readmodels.views.instrument_text import lenders, terms, terms_not_disclosed

INDENTURE = "https://www.sec.gov/Archives/edgar/data/1144879/000149315225024479/ex4-1.htm"
PRESS = "https://news.example.test/vantage-38bn"
INDENTURE_TEXT = (
    "<html><head><meta charset=\"utf-8\"></head><body><p>INDENTURE, dated as of November 20, 2025, among APLD "
    "ComputeCo LLC, a Delaware limited liability company (the \u201cIssuer\u201d)</p><p>\u201cIssue Date\u201d means "
    "November 20, 2025.</p><p>\u201cSubsidiary Guarantor\u201d means each of the Issuer\u2019s Subsidiaries as of the "
    "Issue Date</p><p>Section 4.04 Limitation on Debt 57 Section 4.06 Limitation on Liens 61</p></body></html>"
).encode()
PRESS_TEXT = (b"<html><body><p>JPMorgan Chase &amp; Co. and Mitsubishi UFJ Financial Group are among banks leading "
              b"the deal.</p></body></html>")
IID = "EX-APLD-010"


def rows(sql: str, params: tuple = ()) -> list[dict]:
    with db.transaction() as conn:
        return conn.execute(sql, params).fetchall()


@pytest.fixture
def stored():
    with db.transaction() as conn:
        ensure_intake_sources(conn)
        conn.execute("INSERT INTO instrument (id, name, kind, issuer_name) VALUES (%s, '9.25%% Notes', 'bond', 'x')",
                     (IID,))
        FactWriter(conn).add("instrument", IID, "issuer_name", "APLD COMPUTECO LLC", tag="F",
                             source_name="FINRA TRACE (finra.org)", valid_from="2026-10-20", producer="seed:test",
                             status="published")
    documents.record_document("agent_url", INDENTURE, INDENTURE_TEXT, content_type="text/html")
    documents.record_document("agent_url", PRESS, PRESS_TEXT, content_type="text/html")


def F(field, value, quote, url=INDENTURE, tag="F"):
    return {"field": field, "value": value, "tag": tag, "url": url, "quote": quote, "valid_from": "2025-11-20"}


def spec(**extra):
    return {"instruments": [{"id": IID, "facts": [
        F("issuer_name", "APLD ComputeCo LLC", "among APLD ComputeCo LLC, a Delaware limited liability company"),
        F("issue_date", "2025-11-20", "“Issue Date” means November 20, 2025."),
        F("covenants", "Article 4 covenants: limitation on debt; liens", ["Limitation on Debt", "Limitation on Liens"]),
        F(lender_field("JPMorgan Chase & Co."), {"name": "JPMorgan Chase & Co.", "role": "lead bank"},
          "JPMorgan Chase & Co. and Mitsubishi UFJ Financial Group are among banks leading", url=PRESS, tag="R"),
    ], "replaces": {"issuer_name": "legal name from the indenture"},
        "not_disclosed": {"backstop": "No guarantee named in the filings checked."}, **extra}]}


def test_apply_writes_quoted_terms_supersedes_the_old_name_and_is_idempotent(stored):
    out = apply_terms(spec())
    assert out["facts"] == 4 and out["superseded"] == 1 and out["not_disclosed"] == 1
    current = {r["field"]: r for r in rows(
        "SELECT * FROM fact_current WHERE subject_type = 'instrument' AND subject_id = %s", (IID,))}
    assert current["issuer_name"]["value"] == "APLD ComputeCo LLC" and current["issuer_name"]["tag"] == "F"
    assert current["issue_date"]["value"] == "2025-11-20" and current["issue_date"]["document_id"] is not None
    assert current[lender_field("JPMorgan Chase & Co.")]["tag"] == "R"
    assert current["terms_not_disclosed"]["tag"] == "M"
    old = rows("SELECT status FROM fact WHERE subject_id = %s AND field = 'issuer_name' AND producer = 'seed:test'",
               (IID,))
    assert [r["status"] for r in old] == ["superseded"]  # kept as history, not deleted
    assert apply_terms(spec())["superseded"] == 0
    assert len(rows("SELECT id FROM fact WHERE subject_id = %s AND producer = 'curation:terms'", (IID,))) == 5


def test_refuses_f_from_a_press_report_a_missing_quote_and_writes_nothing(stored):
    bad = spec()
    bad["instruments"][0]["facts"][3]["tag"] = "F"
    with pytest.raises(ValidationError, match="F needs a filed document"):
        apply_terms(bad)
    missing = spec()
    missing["instruments"][0]["facts"][0]["quote"] = "among APLD ComputeCo 9 LLC"
    with pytest.raises(ValidationError, match="quote not in the stored copy"):
        apply_terms(missing)
    assert rows("SELECT id FROM fact WHERE producer = 'curation:terms'") == []


def test_dry_run_checks_everything_and_writes_nothing(stored):
    assert apply_terms(spec(), dry_run=True)["dry_run"] is True
    assert rows("SELECT id FROM fact WHERE producer = 'curation:terms'") == []


def test_helpers():
    assert is_filed(INDENTURE) and not is_filed(PRESS)
    assert lender_field("JPMorgan Chase Bank, N.A.") == "lender_jpmorgan_chase_bank_n_a"


def _row(value, tag="F", source="SEC EDGAR", day=date(2025, 11, 20)):
    return {"value": value, "tag": tag, "valid_from": day, "source_name": source, "source_url": INDENTURE,
            "method_version": "terms-nd v0.1" if tag == "M" else None}


def test_terms_card_rows_n_d_reasons_and_the_lender_group():
    facts = {
        "maturity": _row("2030-12-01"),
        "issue_date": _row("2025-11-20"),
        "covenants": _row("Article 4 covenants: limitation on debt; liens"),
        "covenant_test": _row({"metric": "Tenant lease termination", "threshold": "default unless replaced"}),
        "lender_mufg": _row({"name": "MUFG Bank, Ltd.", "role": "administrative agent"}),
        "lender_blackstone": _row({"name": "Blackstone", "role": "lead lender"}, tag="R", source="Reuters"),
        "terms_not_disclosed": _row({"backstop": "No guarantee named in the filings checked.",
                                     "issue_date": "never shown: the issue date is known"}, tag="M"),
    }
    shown = terms({"issuer_name": "APLD ComputeCo LLC"}, facts)
    assert shown["Issued"]["value"] == "Nov 20, 2025" and shown["Maturity"]["value"] == "Dec 1, 2030"
    assert shown["Covenants"]["value"].startswith("Article 4") and "Key covenant test" in shown
    group = lenders(facts)
    assert [g["name"] for g in group] == ["MUFG Bank, Ltd.", "Blackstone"]  # filed first
    assert group[0]["source"]["tag"] == "F" and group[0]["role"] == "administrative agent"
    assert terms_not_disclosed(facts, shown, group) == {"Guarantees": "No guarantee named in the filings checked."}
