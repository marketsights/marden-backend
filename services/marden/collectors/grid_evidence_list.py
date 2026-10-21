"""The statements grid_evidence writes: what a primary document says about a subject's grid region.

Each Statement quotes one primary document (company filing, commission record, operator publication) verbatim
and says what the quote states, in the values grid_join uses: `iso` as the ISO/RTO label ('ERCOT', 'SPP',
'MISO', ...), `balancing_authority` as the EIA-930 code, `interconnection` as 'Eastern' / 'Western' / 'ERCOT',
`utility` as the HIFLD retail territory name. grid_evidence derives the balancing authority and interconnection
from a stated ISO/RTO (grid_evidence module docstring) and checks the quote against the stored document.

Add a statement only from a document a person has read: the quote must be the document's own words about that
facility (not a third-party database), and the subjects must be the facility the quote names. Two statements
that disagree on a subject's field leave its review open with both quotes attached.

Checked 2026-10-21 (the seven grid-region review items in docs/STATUS.md and their linked campuses).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

SEC = "SEC EDGAR"
EDGAR = "https://www.sec.gov/Archives/edgar/data"


@dataclass(frozen=True)
class Doc:
    url: str
    title: str
    doc_type: str
    doc_date: date  # filing date: the facts' valid_from
    source_name: str = SEC


@dataclass(frozen=True)
class Statement:
    doc: Doc
    quote: str  # verbatim (whitespace collapsed to single spaces)
    states: tuple[tuple[str, str], ...]  # (field, value) the quote states
    subjects: tuple[tuple[str, str], ...]  # (subject_type, subject_id): the facility the quote names
    loc: str  # where in the document, and which facility the quote is about


WULF_10K_2025 = Doc(f"{EDGAR}/1083301/000108330126000031/wulf-20251231.htm",
                    "TeraWulf Inc. Form 10-K for 2025", "10-K", date(2026, 2, 27))
WULF_JV_DECK = Doc(f"{EDGAR}/1083301/000110465925102858/tm2529509d1_ex99-3.htm",
                   "TeraWulf Inc. Form 8-K Exhibit 99.3, Fluidstack joint venture presentation (October 2025)",
                   "8-K exhibit", date(2025, 10, 28))
IREN_20F_2024 = Doc(f"{EDGAR}/1878848/000162828024038677/iren-20240630.htm",
                    "IREN Limited Form 20-F for the year ended June 30, 2024", "20-F", date(2024, 8, 28))
GLXY_10Q_2026Q2 = Doc(f"{EDGAR}/1859392/000185939226000091/glxy-20260630.htm",
                      "Galaxy Digital Inc. Form 10-Q for the quarter ended June 30, 2026", "10-Q", date(2026, 8, 10))
MDU_10Q_2026Q2 = Doc(f"{EDGAR}/67716/000006771626000072/mdu-20260630.htm",
                     "MDU Resources Group, Inc. Form 10-Q for the quarter ended June 30, 2026", "10-Q", date(2026, 8, 6))
CIFR_10K_2025 = Doc(f"{EDGAR}/1819989/000181998926000009/cifr-20251231.htm",
                    "Cipher Digital Inc. Form 10-K for 2025", "10-K", date(2026, 2, 24))
CIFR_10K_2024 = Doc(f"{EDGAR}/1819989/000181998925000005/cifr-20241231.htm",
                    "Cipher Mining Inc. Form 10-K for 2024", "10-K", date(2025, 2, 25))

MDU_QUOTE = (
    "Applied Digital's load is purchased from the MISO market and does not impact the power supply available to "
    "other customers. The NDPSC approved an electric service agreement to serve an additional 350 MW of data center "
    "load with Applied Digital in the Company's service territory. Approximately 60 MW of the incremental data "
    "center load is currently online, with the remaining capacity available and awaiting Applied Digital's request "
    "to increase service. Load at the second HPC building in Ellendale is expected to begin ramping in the third "
    "quarter of 2026. The third HPC building at Ellendale is scheduled to begin ramping in January 2027."
)
POLARIS = (("site", "polaris-forge-1"), ("site", "polaris-forge-1-building-4"))
HELIOS = (("site", "helios-1"), ("site", "helios-2"))

EVIDENCE: tuple[Statement, ...] = (
    Statement(
        WULF_10K_2025,
        "Abernathy HPC Campus (the “Abernathy HPC Campus”), located in Abernathy, Texas, within the Southwest Power "
        "Pool (SPP).",
        (("iso", "SPP"),),
        (("site", "abernathy"), ("campus", "fluidstack-abernathy")),
        "Item 1 (Business), HPC Platform and Development Pipeline: the Abernathy HPC Campus (Fluidstack joint venture)",
    ),
    Statement(
        WULF_JV_DECK,
        "Grid: SPP • Utility: Xcel Energy ‒ Southwestern Public Service Company service territory",
        (("utility", "SOUTHWESTERN PUBLIC SERVICE CO"),),
        (("site", "abernathy"),),
        "slide 4, Abernathy Site highlights (120 acres in Abernathy, TX)",
    ),
    Statement(
        IREN_20F_2024,
        "Electricity at Childress, Texas is sourced from ERCOT, the organization that operates Texas’ electrical grid.",
        (("iso", "ERCOT"),),
        (("site", "childress"), ("campus", "iren-childress")),
        "Item 5 (Operating and Financial Review and Prospects), electricity in Texas: IREN's Childress site",
    ),
    Statement(
        GLXY_10Q_2026Q2,
        "In total, the Electric Reliability Council of Texas (“ERCOT”) has approved over 1.6 gigawatts (“GW”) of "
        "gross power capacity at our Helios campus.",
        (("iso", "ERCOT"),),
        (*HELIOS, ("campus", "galaxy-helios"), ("building", "galaxy-helios-b1")),
        "Item 2 (MD&A), Data Centers segment: the Helios campus (Dickens County, TX), whose Phase I and Phase II "
        "leases to CoreWeave are sites helios-1 and helios-2",
    ),
    Statement(
        MDU_10Q_2026Q2,
        MDU_QUOTE,
        (("iso", "MISO"),),
        (*POLARIS, ("campus", "apld-polaris-forge-1"), ("building", "apld-polaris-forge-1-b1"),
         ("building", "apld-polaris-forge-1-b2"), ("building", "apld-polaris-forge-1-b3")),
        "Item 2 (MD&A), Outlook: Montana-Dakota serves Applied Digital's Ellendale campus (Polaris Forge 1; its "
        "second and third HPC buildings are sites polaris-forge-1 and polaris-forge-1-building-4)",
    ),
    Statement(
        MDU_10Q_2026Q2,
        MDU_QUOTE,
        (("utility", "MONTANA-DAKOTA UTILITIES CO"),),
        POLARIS,
        "Item 2 (MD&A), Outlook: Montana-Dakota's electric service agreement for Applied Digital's Ellendale HPC "
        "buildings",
    ),
    Statement(
        CIFR_10K_2025,
        "Our Stingray site (the “Stingray Site”) is located in West Texas. The Stingray Site has conditional ERCOT "
        "interconnection approval of up to 100 MW.",
        (("iso", "ERCOT"),),
        (("site", "stingray"),),
        "Item 1 (Business): the Stingray site (in Andrews, Texas per Cipher's Form 8-K of 2026-06-15)",
    ),
    Statement(
        CIFR_10K_2024,
        "As part of the transaction to acquire a wholly-owned site in Texas (the “Stingray Facility”), the Company "
        "paid a deposit to Oncor of $ 4.2 million to construct infrastructure to energize the data center to be "
        "returned to the Company if the Stingray Facility uses 100 MW by May 15, 2027.",
        (("utility", "ONCOR ELECTRIC DELIVERY COMPANY LLC"),),
        (("site", "stingray"),),
        "Financial statements, Note 9 (Security deposits): the Oncor deposit for the Stingray Facility",
    ),
)
