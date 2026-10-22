"""A money or term value is F only when a filed document the archive cites states it (marden.seed.cited).

Pure matcher tests on filing-shaped text, the aidebt term rules on rows shaped like the archive's (no
MARDEN_SEED_DIR needed), and the stored-document lookup against the test database.
"""

from __future__ import annotations

from datetime import date

from marden.seed import instruments_facts as F
from marden.seed.cited import CitedDocs, anchor_patterns, normalize_text, scope, states

NCSRS = "https://www.sec.gov/Archives/edgar/data/2028436/000202843626000009/ck0002028436-20260630.htm"
KFGO = "https://kfgo.com/2026/09/18/oracles-18-billion-data-center-debt-under-pressure-ft-reports/"
TENK = "https://www.sec.gov/Archives/edgar/data/1341439/000119312526277521/orcl-20260531.htm"

# The fund's own schedule: four Red Chiles tranches of about $1.2mm par each; never the loan's $18B.
NCSRS_TEXT = normalize_text(
    "Project Miner/Red Chiles B (6) S+ 2.50% 6.14% 11/9/2029 1,232 1,162 1,161 "
    "Red Chiles A (6) S+ 2.50% 6.14% 11/9/2029 1,289 1,216 1,215 "
    "Red Chiles C (6) S+ 2.50% 6.14% 11/9/2029 1,228 1,158 1,158 "
    "Red Chiles D (6) S+ 2.50% 6.14% 11/9/2029 1,250 1,179 1,179 12,078 11,979 "
    "Interest on term loans is based on the Secured Overnight Financing Rate (SOFR).")

JUPITER = {
    "id": "EX-JUPITER-190", "issuer": "Red Chiles A-D, LLC (Project Jupiter SPVs)", "inst": "Construction loans",
    "cpn": "SOFR+2.50% (6.14% at 30 Jun)", "mat": "2029-11-09", "amt_m": 18000, "rank": "Senior secured",
    "status": "outstanding", "src": [NCSRS, KFGO, KFGO],
}


def _by_field(rows):
    return {r.field: r for r in rows}


# ---------------------------------------------------------------- the matcher


def test_amount_forms_filings_print():
    v = 18_000_000_000
    for text in ("about $18.0 billion of loans", "an $18bn package", "aggregate principal of $18,000,000,000",
                 "($ in millions) term loans $ 18,000 total", "(in thousands) term loans 18,000,000",
                 "<balance>18000000000.00000000</balance>"):
        assert states(normalize_text(text), "amount_usd", v), text
    for text in ("$1.8 billion", "$180 billion", "18,000 employees", "$18.05 billion", "item 18b. other"):
        assert not states(normalize_text(text), "amount_usd", v), text


def test_the_jupiter_ncsrs_states_the_terms_but_not_the_18b_size():
    terms = {"coupon": JUPITER["cpn"], "maturity": "2029-11-09", "amount_usd": 18_000_000_000}
    assert states(NCSRS_TEXT, "amount_usd", 18_000_000_000,
                  anchors=anchor_patterns(terms, "amount_usd", name="Construction loans")) is False
    assert states(NCSRS_TEXT, "coupon", JUPITER["cpn"]) is True
    assert states(NCSRS_TEXT, "coupon_spread_bp", 250.0) is True
    assert states(NCSRS_TEXT, "coupon_benchmark", "SOFR") is True
    assert states(NCSRS_TEXT, "maturity", "2029-11-09") is True


def test_a_rounded_archive_size_matches_the_filing_figure():
    text = normalize_text("the aggregate original principal amount of the 2031 notes is $2.5875 billion")
    assert states(text, "amount_usd", 2_588_000_000)  # aidebt amt_m 2588
    assert not states(text, "amount_usd", 2_600_000_000)


def test_bare_table_amounts_need_the_instrument_row():
    row = normalize_text("$2,750, 3.25%, due November 2027 2,750 3.29% $750, 4.50%, due May 2028 750 4.60% "
                         "other liabilities 750")
    terms = {"coupon": "4.50%", "maturity": "2028-05"}
    assert states(row, "amount_usd", 750_000_000, anchors=anchor_patterns(terms, "amount_usd"))
    assert not states(row, "amount_usd", 750_000_000)  # no anchor, no unit: a bare 750 states nothing
    other = {"coupon": "6.15%", "maturity": "2029-11"}
    assert not states(row, "amount_usd", 750_000_000, anchors=anchor_patterns(other, "amount_usd"))


def test_an_amount_in_another_currency_does_not_state_usd():
    text = normalize_text("0.200% Notes due 2026 CHF 275,000 $ 298,404 Dec 15, 2026")
    terms = {"coupon": "0.200%", "maturity": "2026-12"}
    assert not states(text, "amount_usd", 275_000_000, anchors=anchor_patterns(terms, "amount_usd"))
    assert states(text, "amount_usd", 298_404_000, anchors=anchor_patterns(terms, "amount_usd"))


def test_rates_dates_and_terms():
    text = normalize_text("6.87500% Senior Notes due March 15, 2032; term loan at Term SOFR plus 250 basis "
                          "points maturing in 2031; a fifteen (15) year lease; 12-year term")
    assert states(text, "coupon_rate_pct", 6.875) and not states(text, "coupon_rate_pct", 6.75)
    assert states(text, "coupon_spread_bp", 250) and states(text, "coupon_benchmark", "SOFR")
    assert states(text, "maturity", "2032-03-15") and states(text, "maturity", "2032-03")
    assert states(text, "maturity", "2031") and not states(text, "maturity", "2032-04")
    assert states(text, "lease_term_years", 15) and states(text, "term_years", 12)
    assert not states(text, "term_years", 10)
    assert states(normalize_text("<annualizedRt>5.75600000</annualizedRt>"), "coupon_rate_pct", 5.756)


def test_free_text_values_need_every_number():
    text = normalize_text("Floating rate: SOFR + 2.25% per annum; the effective rate was 7%.")
    assert states(text, "rate_text", "SOFR + 2.25%; 7% effective")
    assert not states(text, "rate_text", "SOFR + 2.25%; 9% effective")
    assert states(text, "start", "phased H2 2025") is None  # nothing checkable: left for a person


def test_nothing_to_check_is_none():
    assert states(None, "amount_usd", 1) is None
    assert states("anything", "cost_to_complete_usd", 0) is None


def test_scope_keeps_only_this_instruments_holdings():
    xml = normalize_text(
        "<invstorSec><name>Compass Datacenters Issuer III LLC</name><title>CMDC 2026-1A A21</title>"
        "<cusip>20469BAF4</cusip><balance>18080000.0</balance><annualizedRt>4.89700000</annualizedRt>"
        "</invstorSec><invstorSec><name>Other Issuer LLC</name><title>OTHR 2024-1 A2</title><cusip>111111AA1</cusip>"
        "<annualizedRt>5.25000000</annualizedRt></invstorSec>")
    assert states(xml, "coupon_rate_pct", 5.25)  # somewhere in the report ...
    assert not states(scope(xml, names=["compass"]), "coupon_rate_pct", 5.25)  # ... but not on Compass's lines
    assert states(scope(xml, ids=["20469BAF4"]), "coupon_rate_pct", 4.897)
    assert scope(xml, ids=["999999ZZ9"], names=["nobody"]) == ""
    assert scope("a 10-q", names=["compass"]) == "a 10-q"


# ---------------------------------------------------------------- the aidebt term rules


def test_terms_take_the_filing_that_states_them_and_press_for_the_rest():
    texts = {NCSRS: NCSRS_TEXT}
    rows = _by_field(F.terms_facts("EX-JUPITER-190", JUPITER, "construction_loan", "aidebt.json", texts.get))
    amount = rows["amount_usd"]
    assert (amount.value, amount.tag, amount.source_name, amount.source_url) == (
        18_000_000_000, "R", "Kfgo", KFGO)
    for field in ("coupon", "coupon_type", "coupon_spread_bp", "coupon_benchmark", "maturity"):
        assert (rows[field].tag, rows[field].source_url, rows[field].source_name) == ("F", NCSRS, "SEC EDGAR"), field
    assert not any(r.flag for r in rows.values())
    # fields that are not money or terms keep the row's primary URL
    assert (rows["seniority"].tag, rows["seniority"].source_url) == ("F", NCSRS)


def test_a_second_cited_filing_can_state_what_the_first_does_not():
    row = {**JUPITER, "src": [NCSRS, TENK, KFGO]}
    texts = {NCSRS: NCSRS_TEXT, TENK: normalize_text("Red Chiles construction loans of $18.0 billion")}
    amount = _by_field(F.terms_facts("EX-JUPITER-190", row, "construction_loan", "aidebt.json", texts.get))[
        "amount_usd"]
    assert (amount.tag, amount.source_url) == ("F", TENK)


def test_no_stating_filing_and_no_press_source_is_flagged_not_filed():
    row = {**JUPITER, "src": [NCSRS]}
    rows = _by_field(F.terms_facts("EX-JUPITER-190", row, "construction_loan", "aidebt.json", {NCSRS: NCSRS_TEXT}.get))
    amount = rows["amount_usd"]
    assert (amount.tag, amount.source_name, amount.source_url) == ("R", F.UNVERIFIED, None)
    assert amount.flag and NCSRS in amount.flag
    assert rows["coupon"].tag == "F" and not rows["coupon"].flag


def test_a_search_page_is_not_a_document():
    row = {**JUPITER, "src": ["https://www.sec.gov/edgar/search/#/forms=NPORT-P"]}
    rows = _by_field(F.terms_facts("X", row, "bond", "aidebt.json", lambda url: "anything 2.50% 11/9/2029"))
    assert {r.tag for f, r in rows.items() if f in F.CHECKED_TERMS} == {"R"}
    assert all(rows[f].source_name == F.UNVERIFIED for f in F.CHECKED_TERMS if f in rows)


def test_without_stored_text_no_term_is_filed():
    rows = _by_field(F.terms_facts("EX-JUPITER-190", JUPITER, "construction_loan", "aidebt.json"))
    assert {rows[f].tag for f in F.CHECKED_TERMS if f in rows} == {"R"}


def test_press_primary_and_unsourced_rows_are_unchanged():
    press = _by_field(F.terms_facts("X", {**JUPITER, "src": [KFGO, NCSRS]}, "bond", "aidebt.json", {}.get))
    assert {(r.tag, r.source_url) for r in press.values() if r.field in F.CHECKED_TERMS} == {("R", KFGO)}
    bare = _by_field(F.terms_facts("X", {**JUPITER, "src": []}, "bond", "aidebt.json", {}.get))
    assert {r.tag for r in bare.values() if r.field in F.CHECKED_TERMS} == {"M"}


# ---------------------------------------------------------------- stored documents


def test_cited_docs_reads_stored_bytes_and_skips_record_only_rows(conn):
    from marden.core import documents, sources

    sources.ensure_source("test_docs", "Test docs", "api", "https://www.sec.gov", None, "test source", conn=conn)
    stub = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/ex10-1.htm"
    documents.record_document("test_docs", stub, b'{"exhibit": "EX-10.1"}', content_type="application/json",
                              meta={"record_only": True}, conn=conn)
    held = documents.record_document("test_docs", NCSRS, b"<html><body><p>Red Chiles A S+ 2.50% 11/9/2029</p>"
                                     b"</body></html>", content_type="text/html", conn=conn)
    docs = CitedDocs(conn)
    assert docs.text(stub) is None
    assert "red chiles a s+ 2.50% 11/9/2029" in (docs.text(NCSRS) or "")
    assert docs.document_ids[NCSRS] == held.id
    rows = _by_field(F.terms_facts("EX-JUPITER-190", JUPITER, "construction_loan", "aidebt.json", docs))
    assert (rows["maturity"].tag, rows["maturity"].document_id) == ("F", held.id)
    assert rows["amount_usd"].tag == "R"
    assert docs.text("https://www.sec.gov/edgar/search/#/forms=NPORT-P") is None
    assert date.today()  # the lookup never fetches: only what is stored counts
