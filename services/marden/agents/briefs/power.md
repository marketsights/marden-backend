## Why it matters

A data center without power is a stranded building. Lenders track whether the load is approved, when it can
energize, who pays for the network upgrades, and every delay, denial and withdrawal along the way; a power
denial or withdrawal turns a chain red. Commission and FERC orders are filed facts with exact dates, and the
ISO / utility large-load reports give the MW that are actually energized.

## Where the facts are

- FERC orders and letters (eLibrary issuances): the caption (parties, docket number), the order title
  ("Order Denying Complaint", "Order Accepting ... Interconnection Agreement", "Deficiency Letter"), the
  issue date, the ordering paragraphs ("we deny the complaint").
- FERC and state filings: interconnection agreements (MW, in-service and milestone dates, network upgrades
  and who funds them), large-load tariffs and ESAs.
- State commission dockets: certificates of convenience and necessity (CCN), ESA approvals, hearing
  schedules, staff recommendations.
- ISO / utility large-load and queue reports (ERCOT LLWG / TAC reports, PJM, MISO, SPP): MW approved to
  energize, observed load, queue MW by status and zone.

## How to record

- docket_order {"docket", "order", "date", "outcome"} on the party the order is about (the complainant or
  applicant entity, or the plant / site when the order names the project). outcome is one word in the
  order's terms: granted, denied, dismissed, accepted, rejected, deficient, approved.
- Queue facts are on the queue entry (queue_project) named by its queue id when printed.
- energized_mw on the site / campus / entity the report states it for (an ISO-wide total is on the ISO's
  entity, e.g. "ERCOT").
- MW in MW (1.2 GW is 1200). Dates as written; a hearing or deadline in the future is still a fact (put it in
  a milestone or docket_order date).

## Traps

- What a party alleges or requests is not what the order decides: record the decision, not the complaint.
- Generation MW of a project (e.g. a 750 MW mixed project) is not load MW of a data center.
- Report dates and data dates differ ("current as of May 20, 2026"); use the data date for as_of.
- Do not correct a document's apparent typos (a February 2026 report that says "February 2025"): quote it as
  written and lower confidence.
- Charts without numbers in the text carry no facts you can quote.

## Examples from real filings

FERC, Oklo Inc. v. PJM Interconnection, L.L.C., Docket No. EL26-101-000, stored document:
- quote "In this order, we deny the complaint and dismiss as moot the waiver request" -> docket_order
  {"docket": "EL26-101-000", "order": "Order Denying Complaint", "date": "2026-09-24", "outcome": "denied"}
  on entity "Oklo Inc.".
- quote "Oklo’s 750 megawatt (MW) mixed technology project (Project)" describes generation in PJM's queue:
  not a data-center load.

ERCOT Large Load Interconnection Status Update (February 2026 TAC report), stored document:
- quote "Of the 8876 MW that have received Approval to Energize, ERCOT has observed a non-simultaneous
  monthly peak consumption of 3998 MW in February 2025" -> energized_mw 8876 on entity "ERCOT" (approved to
  energize), with a note that the observation month is printed as February 2025.
