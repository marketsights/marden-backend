"""air_permits parsers and the match rule, on records captured on 2026-10-21 (data/air_permits):

- `cr_rows_20261021.json`: TCEQ Central Registry Files rows (data.texas.gov) of six regulated entities as served:
  Longhorn Data Center (RN112029061, Taylor), ABI-1 EGU (RN112247101, Shackelford), Abilene Power Station
  (RN100542422, AEP, Taylor), Pacifico's B & W Ranch (RN102987831, Bastrop) and El Paso Electric's Newman and
  Copper stations (El Paso).
- `ro_rn112029061_trimmed.html.gz`: the TCEQ Records Online result page for RN112029061, cut to the rows of
  permits 177263, 182126 (+ PSDTX1688, GHGPSDTX263) and 182267; markup as served.
- `tr_177263_387309.pdf.gz`, `fa_177263_387309.pdf.gz`: the technical review and final-action letter of the
  Longhorn standard permit amendment (project 387309); `tr_181009_396274.pdf.gz`: Fermi's PSD technical review.
- `nmed_post_7565.json`, `nmed_legal_notice_10883.pdf.gz`: NMED's YGI Microgrid notice post and legal notice.
- `aqrr_plant_11491.html`, `aqrr_25-350_pages1-3.pdf.gz`: the Iowa DNR plant page of Orient Energy Center and the
  first three pages of its project 25-350 permit collection.
"""

from __future__ import annotations

import gzip
import json
from datetime import date
from pathlib import Path

from marden.collectors import air_permits_ia as ia
from marden.collectors import air_permits_match as am
from marden.collectors import air_permits_nm as nm
from marden.collectors import air_permits_text as text
from marden.collectors import air_permits_tx_parse as tx

DATA = Path(__file__).parent / "data" / "air_permits"


def _pdf(name: str) -> str:
    raw = (DATA / name).read_bytes()
    return text.pdf_text(gzip.decompress(raw) if name.endswith(".gz") else raw)


def _cr_facilities() -> list[am.Facility]:
    rows = json.loads((DATA / "cr_rows_20261021.json").read_text())
    return tx.facilities(rows, url="https://data.texas.gov/resource/5eqq-7nad.json?$where=x", document_id=1,
                         system="tceq_cr_open_data", dataset="5eqq-7nad")


def test_technical_review_states_units_ratings_and_the_received_date():
    tr = tx.technical_review(_pdf("tr_177263_387309.pdf.gz"))
    assert tr["type"] == "Standard"
    assert tr["header"]["Registration Number"] == "177263" and tr["header"]["County"] == "Taylor"
    assert tr["received"] == date(2025, 1, 11) and tr["received_quote"] == "Project Received Date January 11, 2025"
    units = [(u["count"], u["model"], u["oem"], u["rating_mw_each"]) for u in tr["units"]]
    assert units == [(5, "Titan 350", "Solar", 38.0), (5, "LM2500", "GE Vernova", 34.1)]
    assert all("from six to five" in u["quote"] for u in tr["units"])  # each value has its verbatim sentence


def test_final_action_letter_gives_expiry_application_date_and_the_units_sentence():
    fa = tx.final_action(_pdf("fa_177263_387309.pdf.gz"))
    assert fa["letter_date"] == date(2025, 1, 22)
    assert fa["expires"] == date(2034, 8, 21) and fa["expires_quote"].endswith("August 21, 2034")
    assert fa["applied"] == date(2025, 1, 11)
    assert fa["statement"] == ("We understand that this registration is for emissions associated with the 10 natural "
                               "gas simple-cycle turbines located at the Longhorn Data Center.")


def test_psd_review_lists_turbines_and_emission_totals_only_as_stated():
    tr = tx.technical_review(_pdf("tr_181009_396274.pdf.gz"))
    assert tr["type"] == "NSR" and tr["header"]["County"] == "Carson" and tr["received"] == date(2025, 8, 1)
    by_quote = {u["quote"]: u for u in tr["units"]}
    sgt = next(u for q, u in by_quote.items() if "90 Siemens Energy SGT-800" in q)
    assert (sgt["count"], sgt["model"], sgt["oem"]) == (90, "SGT-800", "Siemens Energy")
    assert any(u["rating_mw_each"] == 45.0 for u in tr["units"])  # 'each with a rated gross nominal capacity of 45 MW'
    totals = {e["pollutant"]: e["value"] for e in tr["emissions"]}
    assert totals["NOX"] == 1741.75 and totals["CO2e"] == 23964782.74 and totals["PM2.5"] == 1229.25


def test_central_registry_rows_group_into_facilities_with_permits_and_customers():
    fac = {f.key: f for f in _cr_facilities()}
    longhorn = fac["RN112029061"]
    assert (longhorn.name, longhorn.county, longhorn.state) == ("LONGHORN DATA CENTER", "Taylor", "TX")
    numbers = {p.number: p for p in longhorn.permits}
    assert {"177262", "177263", "182126", "PSDTX1688", "GHGPSDTX263", "O4721"} <= set(numbers)
    assert "4844100274" not in numbers  # an air account number, not a permit
    assert numbers["182126"].status == "pending" and numbers["177263"].status == "issued"
    assert numbers["O4721"].type == "Title V" and numbers["PSDTX1688"].type == "PSD"
    assert "Crusoe Technologies Llc" in (numbers["182126"].applicant or "")
    assert numbers["177263"].url.endswith("ref_num_txt=RN112029061&additional_id_text=177263")
    assert longhorn.generation == "reg_ent_desc_txt: ELECTRICITY GENERATION FOR DATA CENTER"


def test_records_online_rows_form_permit_families_with_dated_actions():
    rows = tx.ro_rows(gzip.decompress((DATA / "ro_rn112029061_trimmed.html.gz").read_bytes()))
    assert rows and rows[0].url.startswith("https://records.tceq.texas.gov/cs/idcplg?IdcService=TCEQ_EXTERNAL_SEARCH")
    fams = {f.number: f for f in tx.families(rows)}
    assert fams["182126"].ids == ["182126", "GHGPSDTX263", "PSDTX1688"]  # one PSD permit under project 400428
    assert "177263" in fams and fams["177263"].ids == ["177263"]
    psd = {(a.kind, a.day) for a in tx.actions(fams["182126"])}
    assert ("notice", date(2026, 8, 5)) in psd and ("draft", date(2026, 8, 11)) in psd
    assert [(a.kind, a.day) for a in tx.actions(fams["182267"])] == [("withdrawal", date(2026, 4, 20))]
    assert tx.row_signature(rows) == tx.row_signature(tx.ro_rows(gzip.decompress(
        (DATA / "ro_rn112029061_trimmed.html.gz").read_bytes())))


def _profile(pid: str, name: str, county: str | None, operator: str, campus: list[str] | None = None,
             **facts: object) -> am.PlantProfile:
    counties = [("taylor",), ("bastrop",), ("el", "paso"), ("shackelford",), ("carson",)]
    row = {"id": pid, "name": name, "state": "TX", "county": county, "campus_counties": campus or [],
           "facts": {"operator": operator, **facts}, "prior_permit": None}
    return am.profile(row, counties, {})


def test_match_needs_name_generation_and_location_and_reports_the_rest():
    plants = [
        _profile("n-abilene-onsite", "Stargate Abilene on-site gas generation & expansion", "Taylor",
                 "Crusoe / Lancium (Blue Owl & Primary Digital JV owners)"),
        _profile("p-098", "GW Ranch", None, "Pacifico Energy"),
        _profile("n-epe-meta", "El Paso Electric new gas unit for Meta El Paso data center", None, "El Paso Electric",
                 campus=["El Paso"]),
    ]
    out = {m.facility.key: m for m in am.match_all(_cr_facilities(), plants)}
    ok = out["RN112029061"]
    assert ok.plant and ok.plant.id == "n-abilene-onsite"
    assert ok.evidence["name"] == {"phrase": "crusoe", "from": "plant operator"}
    assert ok.evidence["county"] == {"county": "Taylor", "from": "plant county"}
    assert out["RN100542422"].plant is None  # AEP's Abilene Power Station: same county, no name of ours
    bastrop = out["RN102987831"]  # Pacifico, but in Bastrop County and not the GW Ranch project
    assert bastrop.plant is None and am.candidate(bastrop)
    assert bastrop.reasons == ["no location agreement (county, cited permit number or project name)"]
    for rn in ("RN100211309", "RN100215490"):  # two El Paso Electric stations in the county: which one is not guessed
        assert out[rn].plant is None and "2 facilities of the operator" in out[rn].reasons[0]


def test_name_phrases_keep_names_and_drop_notes():
    assert am.name_phrases("Crusoe (affiliate GN DC1 LLC per state filings)") == [("crusoe",)]
    assert ("cpv",) in am.name_phrases("Competitive Power Ventures (CPV)")
    assert am.name_phrases("Bullock Data Center, LLC") == [("bullock",)]
    # the representative ('rep. Hugo Reed and Associates') and a lower-case note are not the operator
    texas_solar = "Texas Solar Works LLC (rep. Hugo Reed and Associates), build"
    assert am.name_phrases(texas_solar) == [("texas", "solar", "works")]
    assert am.project_phrase("PROJECT LLANO") == ("project", "llano")
    assert am.project_phrase("ABILENE POWER STATION") is None  # a town alone is not a project name
    assert am.same_name("Orient Energy Center", "ORIENT ENERGY CENTER")


def test_nmed_legal_notice_and_post_actions():
    ln = nm.legal_notice(_pdf("nmed_legal_notice_10883.pdf.gz"))
    assert (ln["number"], ln["county"], ln["applicant"], ln["facility"]) == (
        "10883", "Doña Ana", "Yucca Growth Infrastructure, LLC", "YGI Microgrid")
    assert ln["generation"] == "This application is for a microgrid power generation facility."
    assert ln["units"][0]["model"] == "Bloom Energy Server" and ln["units"][0]["oem"] == "Bloom Energy"
    nox = next(e for e in ln["emissions"] if e["pollutant"] == "NOx")
    assert nox["value"] == 37.2 and nox["quote"] == "Nitrogen Oxides (NOx) at 37.2 tons per year (tpy)"
    post = json.loads((DATA / "nmed_post_7565.json").read_text())
    acts = {(a.kind, a.day) for a in nm.notice_actions(nm.links(post))}
    assert {("application", date(2026, 4, 27)), ("completeness", date(2026, 5, 27)), ("draft", date(2026, 7, 29)),
            ("hearing", date(2026, 9, 14))} <= acts


def test_iowa_plant_page_and_permit_collection():
    head, rows = ia.plant_page((DATA / "aqrr_plant_11491.html").read_bytes())
    assert head == {"County Name": "Adair", "City Name": "Creston", "Plant Name": "Orient Energy Center",
                    "Plant Number": "01-09-001"}
    assert [(r.permit, r.project, r.issued) for r in rows][:2] == [("26-A-117", "25-350", date(2026, 4, 30)),
                                                                   ("26-A-118", "25-350", date(2026, 4, 30))]
    assert rows[-1].permit == "26-A-246" and rows[-1].project == "26-153" and rows[-1].equipment == "CT-3 Stack"
    c = ia.collection(_pdf("aqrr_25-350_pages1-3.pdf.gz"))
    assert (c["company"], c["project"], c["process"]) == ("Orient Energy Center", "25-350",
                                                          "Combustion Turbines Units 1 and 2")
    assert c["domains"] == ["midamerican"]  # the owner, from its contacts' addresses
    assert [(u["emission_unit"], u["capacity"]) for u in c["units"]] == [
        ("EU-1", "2,315 MMBtu/hr, natural gas"), ("EU-2", "2,315 MMBtu/hr, natural gas")]
    assert c["limits"] == [{"pollutant": "NOx", "value": 235.42, "unit": "tons/yr",
                            "basis": "combined, 12-month rolling", "quote": "Nitrogen Oxides (NOx) 235.42"}]
