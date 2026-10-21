"""Document text, quote finding, sections and the code validators (spec 22 layers 1 and 3)."""

from __future__ import annotations

import io
import zipfile
from datetime import date

import pymupdf

from marden.agents.extract import Candidate, parse
from marden.agents.sections import section_text, split
from marden.agents.text import find_quote, normalize, pdf_pages, read_docx, read_html, read_pdf, read_plain, sniff
from marden.agents.validators import check_candidate, check_group, number_matches, numbers_in

LEFT, RIGHT, NBSP, SHY = chr(0x201C), chr(0x201D), chr(0xA0), chr(0xAD)


def cand(field, value, quote, area="debt", kind="instrument", name="DDTL 5.5 Facility"):
    return Candidate(area=area, section="all", subject_kind=kind, subject_name=name, subject_identifier=None,
                     field=field, value=value, unit=None, as_of=None, quote=quote, page=None, confidence=0.9)


def test_html_pages_hidden_headers_and_cells():
    html = (b'<html><body><div style="display:none"><ix:header>crwv-20260807FALSE0001769628</ix:header></div>'
            b'<p>Item 1.01 Entry into a Material Definitive Agreement.</p>'
            b'<hr style="page-break-after:always"/><table><tr><td>DDTL 1.0 Facility</td><td>$ 1,300</td></tr></table>'
            b'</body></html>')
    doc = read_html(html)
    assert "crwv-20260807" not in doc.text
    assert doc.page_count == 2
    hit = find_quote(doc, "DDTL 1.0 Facility $ 1,300")
    assert hit is not None and hit.page == 2


def test_quotes_survive_typography_whitespace_and_pdf_line_breaks():
    doc = read_plain(f"providing for a $2.6{NBSP}billion  delayed draw\nterm loan facility (the {LEFT}DDTL 5.5 "
                     f"Facility{RIGHT}). The con- tract is pre{SHY}paid.".encode())
    assert find_quote(doc, 'providing for a $2.6 billion delayed draw term loan facility (the "DDTL 5.5 Facility")')
    assert find_quote(doc, "The contract is prepaid.").mode == "dehyphenated"
    assert find_quote(doc, "a $2.7 billion delayed draw") is None
    assert normalize(f"a{NBSP}\n b") == "a b"


def test_docx_and_pdf_readers_keep_pages():
    buf = io.BytesIO()
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = (f'<w:document xmlns:w="{w}"><w:body><w:p><w:r><w:t>ORDER DENYING COMPLAINT</w:t></w:r></w:p>'
            f'<w:p><w:r><w:br w:type="page"/><w:t>Docket No. EL26-101-000</w:t></w:r></w:p></w:body></w:document>')
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", body)
    docx = buf.getvalue()
    assert sniff(docx) == "docx"
    d = read_docx(docx)
    assert d.page_count == 2 and find_quote(d, "Docket No. EL26-101-000").page == 2
    pdf = pymupdf.open()
    for text in ("Plain Language Summary", "Number 182126, GHGPSDTX263 and PSDTX1688"):
        page = pdf.new_page()
        page.insert_text((72, 72), text)
    data = pdf.tobytes()
    p = read_pdf(data)
    assert p.page_count == 2 and not p.scanned
    assert find_quote(p, "Number 182126, GHGPSDTX263 and PSDTX1688").page == 2
    assert read_pdf(pdf_pages(data, 2, 2)).page_count == 1


def test_sections_split_on_headings_with_page_markers():
    pages = [f"ARTICLE {n}\nSection {n}.01 Terms.\n" + ("word " * 400) for n in ("I", "II", "III")]
    doc = read_plain("\f".join(pages).encode())
    secs = split(doc, 2500)
    assert len(secs) >= 3 and all(s.end - s.start <= 2500 for s in secs)
    assert secs[0].title.startswith("ARTICLE I")
    assert section_text(doc, secs[-1]).startswith(f"[page {secs[-1].first_page}]")


def test_number_matching_scales_units_and_table_headers():
    assert numbers_in("ratio of at least 1.35x") == [(1.35, "x")]
    assert number_matches(2.6e9, "usd", "a $2.6 billion delayed draw term loan")[0]
    assert number_matches(550, "bp", "an applicable margin of 5.50% per annum")[0]
    assert number_matches(25, "mw", "Phase 1: 25,000 kW")[0]
    assert number_matches(1500, "mw", "to reach 1.5 GW")[0]
    assert number_matches(1.3e9, "usd", "DDTL 1.0 Facility Mar 2028 15% $ 1,300", 1e6) == (True, False)
    assert number_matches(1.3e9, "usd", "DDTL 1.0 Facility Mar 2028 15% $ 1,300") == (False, True)
    assert number_matches(2.7e9, "usd", "a $2.6 billion facility") == (False, False)


def test_candidate_checks_reject_unquoted_or_wrong_values_and_flag_soft_problems():
    doc = read_plain(b"The maturity date of the DDTL 5.5 Facility is September 1, 2031. "
                     b"(dollars in millions) Cash and cash equivalents $ 5,524 $ 3,127")
    ok = cand("maturity", "2031-09-01", "The maturity date of the DDTL 5.5 Facility is September 1, 2031.")
    check_candidate(ok, doc)
    assert not ok.hard_fail and not ok.soft_fail and ok.page == 1
    wrong_year = cand("maturity", "2032-09-01", "The maturity date of the DDTL 5.5 Facility is September 1, 2031.")
    check_candidate(wrong_year, doc)
    assert wrong_year.checks["year_in_quote"]["ok"] is False and wrong_year.hard_fail
    invented = cand("maturity", "2031-09-01", "The maturity date is September 1, 2031.")
    check_candidate(invented, doc)
    assert invented.checks["quote_found"]["ok"] is False and invented.hard_fail
    scaled = cand("cash_usd", 5524000000, "Cash and cash equivalents $ 5,524 $ 3,127", area="periodic", kind="entity")
    check_candidate(scaled, doc)
    assert not scaled.hard_fail and "implied_scale" not in scaled.checks
    bad_kind = cand("cash_usd", 5524000000, "Cash and cash equivalents $ 5,524 $ 3,127", area="periodic")
    check_candidate(bad_kind, doc)
    assert bad_kind.checks["subject_kind"]["ok"] is False
    future = cand("closing_date", "2026-08-07", "The maturity date of the DDTL 5.5 Facility is September 1, 2031.")
    future.as_of = date(2099, 1, 1)
    check_candidate(future, doc, today=date(2026, 10, 21))
    assert future.as_of is None


def test_group_checks_date_order_and_tranche_sums():
    doc = read_plain(b"Dated as of August 7, 2026. Matures September 1, 2025. Facility of $2.6 billion. "
                     b"Tranche A $1.0 billion and Tranche B $1.0 billion.")
    c1 = cand("closing_date", "2026-08-07", "Dated as of August 7, 2026.")
    c2 = cand("maturity", "2025-09-01", "Matures September 1, 2025.")
    size = cand("amount_usd", 2600000000, "Facility of $2.6 billion.")
    tr = cand("tranches", {"tranches": [{"name": "A", "amount_usd": 1e9}, {"name": "B", "amount_usd": 1e9}]},
              "Tranche A $1.0 billion and Tranche B $1.0 billion.")
    for c in (c1, c2, size, tr):
        check_candidate(c, doc)
    check_group([c1, c2, size, tr])
    assert c1.checks["date_order"]["ok"] is False and c2.checks["date_order"]["ok"] is False
    assert size.checks["tranche_sum"]["ok"] is False and "2,000,000,000" in size.checks["tranche_sum"]["detail"]


def test_parse_builds_values_by_field_type():
    out = {"facts": [
        {"subject_kind": "instrument", "subject_name": "DDTL 5.5 Facility", "subject_identifier": None,
         "field": "arrangers", "value_number": None, "value_text": None, "value_date": None, "value_bool": None,
         "value_json": '["JPMorgan Chase Bank, N.A.", "MUFG Bank, Ltd."]', "as_of": "2026-08-07",
         "quote": "q", "page": 1, "confidence": 0.95, "note": None},
        {"subject_kind": "instrument", "subject_name": "x", "subject_identifier": None, "field": "maturity",
         "value_number": None, "value_text": "September 1, 2031", "value_date": None, "value_bool": None,
         "value_json": None, "as_of": None, "quote": "q", "page": None, "confidence": 2, "note": None},
        {"subject_kind": "instrument", "subject_name": "x", "subject_identifier": None, "field": "not_a_field",
         "value_number": 1, "value_text": None, "value_date": None, "value_bool": None, "value_json": None,
         "as_of": None, "quote": "q", "page": None, "confidence": 1, "note": None},
    ], "subjects": [], "summary": "s"}
    cands, _, _ = parse("debt", "all", out)
    assert [c.field for c in cands] == ["arrangers", "maturity"]
    assert cands[0].value == ["JPMorgan Chase Bank, N.A.", "MUFG Bank, Ltd."] and cands[0].as_of == date(2026, 8, 7)
    assert cands[1].value is None and cands[1].problems and cands[1].confidence == 1.0
