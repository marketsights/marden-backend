"""Opposition tab: a court case's DATE is its own (filed / entered / decided), never the day Marden recorded
it passed off as a filing date; WHAT is plain words with the case numbers on a sub-line; WHERE the court;
STATUS as a source states it, n/d with the reason; latest ruling, next meeting and 'if the ordinances are
voided' from the proceeding and site facts (facilities/proceedings.py).

Real rows: The Barn (Saline Township, MI). The archive recorded the Court of Appeals proceeding on
2026-10-17; the Attorney General's claims of appeal were filed 2026-04-17 (AG brief, received by the Court
2026-08-06); the consent judgment was entered 2025-10-15 and a resident's motion to intervene denied
2026-02-20 (Planet Detroit, 2026-02-20); the township calendar lists the Planning Commission on 2026-11-10.
"""

from __future__ import annotations

from datetime import date

from marden.core import db
from marden.core.facts import FactWriter
from marden.readmodels.context import building
from marden.readmodels.registry import get_builder
from marden.readmodels.views import sites_tabs  # noqa: F401 (registers the builders)
from marden.readmodels.views.sites_tabs import FOUND_NOTE, NO_STATUS

COA = "Michigan Court of Appeals Nos. 380334, 380362, 380364, 380878, 380462 (appeals of MPSC U-21990)"
COA_URL = "https://www.courts.michigan.gov/c/courts/coa/case/380334"
BRIEF = ("https://www.michigan.gov/ag/-/media/Project/Websites/AG/releases/2026/August/"
         "U-21990_AG_Appeal-Brief_380362_380364_380878.pdf")
PD = "https://planetdetroit.org/2026/02/judge-denies-data-center-intervention/"
CAL = "https://salinetownship.org/calendar/?month=11&yr=2026"
CJ = ("Washtenaw County Trial Court (Odyssey Public Access) consent judgment Related Digital v. Saline Twp; "
      "Haushalter mandamus (case nos. not found)")


def _fact(conn, subject: tuple[str, str], field: str, value, *, tag: str = "R", url: str = PD,
          source: str = "Planetdetroit", day: str = "2026-10-17") -> None:
    FactWriter(conn).add(subject[0], subject[1], field, value, tag=tag, source_name=source, source_url=url,
                         valid_from=day, producer="curation:facilities", status="published",
                         method_version="opposition-impact v0.1" if tag == "M" else None)


def _proceeding(conn, eid: str, name: str, url: str, kind: str = "court") -> None:
    for field, value in (("linked_sites", ["the-barn"]), ("proceeding", name), ("proceeding_kind", kind),
                         ("list_url", url)):
        _fact(conn, ("entity", eid), field, value, tag="F", url=url, source="Michigan")


def _tab() -> dict:
    b = get_builder("site_tab:opposition")
    assert b is not None
    with building(date.today()), db.transaction() as conn:
        return b.build_one(conn, "the-barn")


def _site(conn) -> None:
    conn.execute("""INSERT INTO site (slug, name) VALUES ('the-barn', 'Saline "The Barn" (Stargate Michigan)')""")


def test_a_case_with_no_dated_source_says_found_not_filed():
    with db.transaction() as conn:
        _site(conn)
        _proceeding(conn, "docket-mi-coa", COA, COA_URL)
    item = _tab()["items"][0]
    assert item["date"] == "2026-10-17" and item["dateKind"] == "found" and item["dateNote"] == FOUND_NOTE
    assert item["status"] == "n/d" and item["statusNote"] == NO_STATUS
    assert item["where"] == "Michigan Court of Appeals" and item["text"] == COA
    assert item["evidence"]["sourceUrl"] == COA_URL


def test_a_curated_case_shows_its_own_date_plain_words_status_ruling_and_impact():
    with db.transaction() as conn:
        _site(conn)
        _proceeding(conn, "docket-mi-coa", COA, COA_URL)
        coa = ("entity", "docket-mi-coa")
        _fact(conn, coa, "summary", "Appeals of the Michigan PSC's approval of DTE's special contracts for the site",
              tag="F", url=BRIEF, day="2026-08-06")
        _fact(conn, coa, "case_numbers", "Mich. Ct. App. Nos. 380334; 380362, 380364, 380878; 380462", tag="F",
              url=BRIEF, day="2026-08-06")
        _fact(conn, coa, "court", "Michigan Court of Appeals", tag="F", url=BRIEF, day="2026-08-06")
        _fact(conn, coa, "case_date", {"d": "2026-04-17", "kind": "filed", "note": "AG claims of appeal"}, tag="F",
              url=BRIEF, source="Michigan", day="2026-08-06")
        _fact(conn, coa, "status", "Pending: appellants' briefs filed", tag="F", url=BRIEF, day="2026-08-06")
        _fact(conn, coa, "status_note", "No decision appears in the public sources found.", day="2026-08-07")
        _proceeding(conn, "docket-washtenaw-cc", CJ, "https://tcweb.ewashtenaw.org/PublicAccess/default.aspx")
        cj = ("entity", "docket-washtenaw-cc")
        _fact(conn, cj, "summary", "Related Digital's zoning suit, settled by the consent judgment", day="2026-02-20")
        _fact(conn, cj, "court", "Washtenaw County Circuit Court", day="2026-02-20")
        _fact(conn, cj, "case_date", {"d": "2025-10-15", "kind": "entered"}, day="2026-02-20")
        _fact(conn, cj, "status", "Settled by consent judgment", day="2026-02-20")
        _fact(conn, cj, "rulings", [{"d": "2026-02-20", "what": "Resident's motion to intervene denied as untimely",
                "short": "Intervention denied"}],
              day="2026-02-20")
        _proceeding(conn, "docket-saline-twp-pc", "Saline Township Planning Commission",
                    "https://salinetownship.org/planning-commission/", kind="zoning")
        _fact(conn, ("entity", "docket-saline-twp-pc"), "next_dates",
              [{"d": "2099-11-10", "what": "Saline Township Planning Commission meeting, 7:00 pm"}], tag="F", url=CAL,
              source="Salinetownship", day="2026-10-22")
        _fact(conn, ("site", "the-barn"), "opposition_if_voided",
              "Little direct effect on the build: the campus stands on the consent judgment.", tag="M",
              url="https://www.clickondetroit.com/", source="Clickondetroit", day="2026-10-22")
    tab = _tab()
    coa_row, cj_row = tab["items"]
    assert coa_row["date"] == "2026-04-17" and coa_row["dateKind"] == "filed" and coa_row["dateNote"]
    assert coa_row["text"].startswith("Appeals of the Michigan PSC") and coa_row["detail"].startswith("Mich. Ct. App.")
    assert coa_row["where"] == "Michigan Court of Appeals" and not coa_row["text"].startswith(coa_row["where"])
    assert coa_row["status"] == "Pending: appellants' briefs filed" and coa_row["statusTone"] == "amber"
    assert coa_row["statusEvidence"]["sourceUrl"] == BRIEF and coa_row["statusNote"]
    assert coa_row["evidence"]["sourceUrl"] == BRIEF  # the row cites the document that dates the case
    assert (cj_row["date"], cj_row["dateKind"], cj_row["status"]) == ("2025-10-15", "entered",
                                                                      "Settled by consent judgment")
    assert "detail" in cj_row and cj_row["detail"] == CJ  # the archive's name, case words and all, on the sub-line
    assert tab["openCases"]["value"]["value"] == 1  # a settled case is not open
    ruling = tab["latestRuling"]
    assert ruling["value"]["value"] == "Intervention denied" and ruling["value"]["asOf"] == "2026-02-20"
    assert ruling["note"] == ("Resident's motion to intervene denied as untimely. "
                              "Feb 20, 2026 · Washtenaw County Circuit Court")
    hearing = tab["nextHearing"]
    assert hearing["value"]["value"] == "2099-11-10" and hearing["value"]["sourceUrl"] == CAL
    assert hearing["note"] == "Saline Township Planning Commission meeting, 7:00 pm"
    voided = tab["ifVoided"]
    assert voided["value"]["value"] == "Little direct effect on the build" and voided["value"]["tag"] == "M"
    assert voided["note"] == "The campus stands on the consent judgment."
