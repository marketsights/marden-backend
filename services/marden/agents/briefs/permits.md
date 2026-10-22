## Why it matters

Behind-the-meter generation (turbines, engines, fuel cells) lets a campus energize before the grid can serve
it, and the air permit is the gate: no permit, no generation, no rent. Lenders track the permit number,
status and dates, how many units of which model and MW are permitted, and the emission limits that cap
running hours. Construction documents (building permits, certificates of occupancy, contractor filings,
mechanic's liens) show whether the building is actually being finished and whether contractors are being
paid.

## Where the facts are

- State air permit notices (TCEQ Notice of Receipt and Intent / Preliminary Decision, Plain Language
  Summaries; NMED public notices and hearing notices; other states' draft permits): applicant, permit
  numbers (NSR, PSD, GHG PSD, Title V, PBR), facility name and location, units, MW, pollutants and
  amounts, comment deadlines and hearing dates.
- Permit documents and technical reviews: unit tables (OEM, model, count, rating), emission limits.
- Building and zoning: county or city permit records, plats, certificates of occupancy.
- Liens: county recorder filings (claimant, amount, date).

## How to record

- The subject is the plant (the generation facility named in the permit, e.g. "Longhorn Data Center" as the
  site of the generation) with the permit number as subject_identifier, or the site / campus for building
  permits.
- air_permit {"number", "agency", "type", "status", "date"}: status in the document's words ("initial
  application", "draft permit", "issued", "pending"). Record one fact per permit number set the document
  names together.
- permitted_units {"units": [{"oem", "model", "count", "rating_mw_each", "fuel"}]} only from a unit table or
  sentence that states them.
- emission_limits: say whether the numbers are limits or proposed totals ("proposed emissions, tons per
  year") in the keys you use.

## Traps

- A plain-language summary's "proposed emissions" are what the applicant asked for, not permit limits.
- "May change during the technical review" means the values are not final: lower confidence and say so in
  `note`.
- Coordinates and customer numbers (CN, RN) are identifiers, not facts to record (use RN as an identifier
  if it names the regulated entity).
- An application is not an issued permit.

## Examples from real filings

TCEQ Plain Language Summary, air permit 182126 / GHGPSDTX263 / PSDTX1688, stored document:
- quote "Crusoe Energy Systems LLC (CN606257079) has submitted an application for an initial permit 182126,
  GHGPSDTX263, and PSDTX1688." -> air_permit {"number": "182126 / GHGPSDTX263 / PSDTX1688", "agency":
  "TCEQ", "type": "NSR initial (PSD, GHG PSD)", "status": "initial application submitted"} on plant
  "Longhorn Data Center" (identifier 182126).
- quote "This permit will authorize additional power generation equipment for onsite consumption in data
  center buildings." -> no field (no units or MW stated).
- the pollutant table ("NOx 760.33" under "Proposed Emissions (ton/yr)") -> emission_limits {"basis":
  "proposed emissions, tons per year", "NOx": "760.33", ...}, quoting the table rows that hold every number in
  the value ("VOC 815.59 PM10 451.10 ... HAPs 49.77"). The table's heading or the sentence above it is never
  the quote: code rejects a value whose numbers are not in its quote.
