"""Procedural docket filings are left out of site / plant evidence lists (marden.core.relevance)."""

from __future__ import annotations

import pytest

from marden.core.relevance import is_procedural, procedural_reason

PROCEDURAL = [
    ("Sidley Austin LLP submits request to update the service list under QF20-670, et. al.", "service list update"),
    ("Sustainable FERC Project submits request to update the service list under EL26-58 et al.", "service list update"),
    ("Iowa Office of Consumer Advocate submits request to update the official service list under EL26-70.",
     "service list update"),
    ("[Mailing / Service List] 6630-TE-113 Service List (6 KB) (Marisa Horning)", "service list update"),
    ("[Filing] Service List Update for 1803 Electric Cooperative, Inc. (Service List Update for 1803.pdf)",
     "service list update"),
    ("Office of the Attorney General, Division of Consumer Counsel - Notice of Removal of C. Meade Browder, Jr. "
     "as Counsel and Update to Service Lists.", "service list update"),
    ("Certificate of Service", "proof of service"),
    ("Affidavit of Service by Regular & Electronic Mail - Notice of Filing and Notice of Public Hearing (7)",
     "proof of service"),
    ("Return Receipt (1)", "proof of service"),
    ("Email to/from NDNA re Notice of Publication (2)", "notice of publication"),
    ("[PRJ] PUC RULES AND PROJECTS: Request for control number", "control number request"),
    ("Entry of Appearance by New Energy Economy", "appearance of counsel"),
    ("[Request for Intervention / Notice of Appearance] Notice of Appearance for Microsoft Corporation (145 KB)",
     "appearance of counsel"),
    ("Data Center Coalition - Notice of Removal of Counsel of Cassidy C. Galindo.", "appearance of counsel"),
    ("(doc-less) Motion to Intervene of Avista Corporation under EL26-67 et al.", "intervention"),
    ("(doc-less) Notice of Intervention of Missouri Public Service Commission under EL26-70.", "intervention"),
    ("(doc-less) Out-of-Time Motion to Intervene of Crusoe Technologies LLC under EL26-67.", "intervention"),
    ("Errata to Order Filed August 5, 2026", "errata"),
    ("[Motion] Joint Request to Correct Rebuttal-CI-Bernhardt (153 KB) (Charlene Beals)", "errata"),
    ("[Motions] Independent Power Producers of New York, Inc.: Request for Extension of Time", "extension of time"),
    ("New York Independent System Operator, Inc. submits Informational Report in compliance with the Commission's "
     "06/18/2026 Order under EL26-69.", "informational filing"),
    ("[Comments] City of New York: Cover Letter", "cover letter"),
    ("145PGRR-31 Tract Comments 032726", "comment in a generic proceeding"),
    ("Comments of Grid Strategies under RM26-4.", "comment in a generic proceeding"),
    ("(doc-less) Motion to Intervene of Association of Businesses Advocating Tariff Equity under EL26-70.",
     "intervention"),
    ("Motion to Intervene and Initial Comments of Global Capacity Alliance in response to FERC's 06/18/2026 Show "
     "Cause Order under RM26-4 et al.", "intervention"),
]

SUBSTANTIVE = [
    "Order Dismissing Request for Rehearing and Clarification re Midcontinent Independent System Operator, Inc. "
    "et al. under EL26-70.",
    "Order Instituting Proceeding Under Section 206 etc. re PJM Interconnection, L.L.C. et al. under EL26-67. "
    "For errata see accession number 20260813-3025.",
    "Motion of Transwestern Pipeline Company, LLC to Dismiss Protests filed by Public Citizen, Inc. et al. "
    "under CP26-80.",
    "Comments of Sierra Club in opposition of the re the proposed Green Chile Gas Pipeline Project under CP26-80.",
    "Entry of Appearance and Request to Move Hearing Date Forward by New Mexico Building and Construction Trades "
    "Council",
    "Motion to Intervene and Protest of Public Citizen under CP26-80.",
    "Motion to Intervene and Comments of Clean Wisconsin under 6630-TE-113.",
    "Notice of Hearing on Motions to Dismiss",
    "Notice of Denial of Rehearing by Operation of Law re Midcontinent Independent System Operator, Inc. "
    "under EL26-70.",
    "PJM Interconnection, L.L.C. submits tariff filing per 35.13(a)(2)(iii): Compliance filing re large load "
    "co-location",
    "Request for extension of time to complete construction of the Green Chile lateral under CP26-80.",
    "[Decision Matrix] Cover Letter and Draft Decision Matrix for Comment (917 KB) (Jana Thompson)",
    "Second Amended Scheduling Order",
    "145PGRR-01 Batch Zero Process for Large Load Interconnections 030426",
    "Monthly Construction Report - July 2026 (1)",
    "[Comments] Public Comment by Teresa Santulli (4 KB) (Public Comment)",  # the site's own docket: kept
    "Withdrawal of the application for a certificate of corridor compatibility",
    "",
]


@pytest.mark.parametrize(("title", "reason"), PROCEDURAL)
def test_procedural_filings_have_a_reason(title: str, reason: str) -> None:
    assert procedural_reason(title) == reason


@pytest.mark.parametrize("title", SUBSTANTIVE)
def test_substantive_filings_are_kept(title: str) -> None:
    assert procedural_reason(title) is None


def test_comments_in_a_shared_proceeding_are_generic_unless_they_name_the_site() -> None:
    title = "[COM] Danny Proctor: Comments on data centers"
    assert procedural_reason(title) is None  # on its own docket a comment may be about the site
    assert procedural_reason(title, generic=True) == "comment in a generic proceeding"
    assert procedural_reason(title, generic=True, named=True) is None


def test_a_reviewer_approved_material_event_is_never_dropped() -> None:
    title = "Sidley Austin LLP submits request to update the service list under QF20-670, et. al."
    assert is_procedural(title)
    assert not is_procedural(title, material=True)


@pytest.mark.parametrize(("title", "url"), [
    ("Saline, Township (Washtenaw, MI)", "https://salinetownship.org/"),
    ("Zoning Board of Appeals", "https://salinetownship.org/zoning-board-of-appeals/"),
    ("Committee Application Form",
     "https://www.donaana.gov/about_us/apply_for_committees_boards/committee_application_form.php"),
])
def test_a_snapshotted_web_page_is_not_a_filing(title: str, url: str) -> None:
    assert procedural_reason(title, url=url) == "web page, not a filing"
    assert procedural_reason(title) is None  # without its address the title alone proves nothing


@pytest.mark.parametrize(("title", "url"), [
    ("Agenda", "https://salinetownship.org/wp-content/uploads/2026/09/Special-Meeting-July-17-2026-Agenda-1.pdf"),
    ("Minutes", "https://salinetownship.org/wp-content/uploads/2026/10/September-2026-Minutes-.pdf"),
    ("Notice of Hearing on Motions to Dismiss", "https://www.env.nm.gov/opf/"),
    ("Four more buildings filed",
     "https://www.datacenterdynamics.com/en/news/applications-filed-for-four-more-buildings-in-cedar-creek-texas/"),
    ("PSCW: Refiled", "https://www.wpr.org/news/atc-reapplies-transmission-project-port-washington-data-center"),
    ("KBRA presale release", "https://www.kbra.com/publications/XpRkXkFp"),
    ("Planning and Zoning Commission Regular Meeting, October 22, 2026",
     "https://donaanaconm.api.civicclerk.com/v1/Meetings/GetMeetingFileStream(fileId=5469,plainText=false)"),
])
def test_dated_documents_on_a_body_site_are_kept(title: str, url: str) -> None:
    assert procedural_reason(title, url=url) is None
