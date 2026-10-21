# V1 site gaps after the seed load

Written by `python -m marden seed all --write-docs` on 2026-10-21 from published facts
(`fact_current`) and the identity tables. One row per V1 site. A gap is a critical field the seed archive
does not carry for the site: nothing is written for it (the UI shows n/d), and it is a target for the
collector or extraction agent in the summary (DATA_DICTIONARY section 4). The checks are listed in
`services/marden/seed/run_all_gaps.py`; there is no site-level debt total in the seed (aiwcredit
`cases[].amt_m` has no source), so `debtB` needs a sum a model makes from the instruments.

## Summary

| Field | V1 sites missing it (of 25) | Fill with |
|---|---|---|
| outside_date | 24 | Extraction agent on lease and credit agreements (8-K EX-10, lessee 10-K/10-Q lease notes; spec 25); press is R |
| debt | 1 | SEC EDGAR full-text search and 8-K / credit-agreement extraction; county IRB and PILOT records for project bonds |
| lead_lenders | 16 | Credit-agreement extraction (lender schedules in EX-10 exhibits), issuer 8-K releases (F), press (R) |
| power_source | 2 | State utility dockets, ISO/RTO large-load and queue reports, state air-permit portals (on-site generation) |
| coordinates | 4 | County parcel (assessor) geometry, else the geocoded permit address (spec 17) |
| campus_link | 4 | Linker agent proposing a campus from coordinates and names; a person approves it (spec 17) |

## Per site

| Site | Case | Outside date | Debt instruments (with size) | Lead lenders | Power source | Gaps |
|---|---|---|---|---|---|---|
| abernathy | FLASH_ABERNATHY | n/d | 1 (1) | Noteholders including BlackRock, Lord Abbett,... (placeholder) | Grid | outside_date, lead_lenders |
| abilene | ABILENE | n/d | 2 (2) | JPMorgan Chase | Hybrid | outside_date |
| barber-lake | CIFR_BARBER | n/d | 1 (1) | Noteholders (placeholder) | Grid | outside_date, lead_lenders |
| beacon-point | HUT_BP | n/d | 1 (1) | n/d | Grid | outside_date, lead_lenders |
| black-pearl | CIFR_BLACKPEARL | n/d | 1 (1) | Noteholders (placeholder) | Grid | outside_date, lead_lenders, coordinates, campus_link |
| cedar-creek | EDGE_CEDAR | n/d | 1 (1) | n/d | n/d | outside_date, lead_lenders, power_source |
| childress | IREN_CHILDRESS | 2027-04 (approx., 'beginning of Q2 of calendar 2027' grace-p... | 2 (2) | n/d | Grid | lead_lenders |
| colossus-2 | XAI_VCI | n/d | 3 (0) | Apollo | Hybrid | outside_date, debt |
| core-scientific-coreweave | CORZ_CW | target commencement + 120 days (per order; dates not disclosed) | 1 (1) | n/d | Grid | outside_date, lead_lenders |
| coreweave-ddtl-3 | CRWV_DDTL3 | n/d | 1 (1) | n/d | Grid | outside_date, lead_lenders, coordinates, campus_link |
| coreweave-ddtl-4 | CRWV_DDTL4 | Delayed Data Center Date = [*] after last Access Date at eac... | 1 (1) | MUFG; Morgan Stanley; Goldman Sachs and JPMorgan | n/d | outside_date, power_source, coordinates, campus_link |
| frontier | VANTAGE_TX | n/d | 2 (2) | JPMorgan Chase and MUFG | On-site gas | outside_date |
| helios-1 | GLXY_H1 | n/d | 2 (1) | Deutsche Bank AG New York Branch; GLAS USA | Grid | outside_date |
| helios-2 | GLXY_H2 | n/d | 1 (1) | n/d | Grid | outside_date, lead_lenders |
| hyperion | BEIGNET | n/d | 1 (1) | Bondholders under the 144A offering (not named) (placeholder) | Grid | outside_date, lead_lenders |
| jupiter | JUPITER | n/d | 1 (1) | About 20 banks (placeholder) | On-site gas | outside_date, lead_lenders |
| lake-mariner | WULF_LM | n/d | 1 (1) | Noteholders (placeholder) | Grid | outside_date, lead_lenders |
| lighthouse | VANTAGE_WI | n/d | 1 (1) | JPMorgan Chase and MUFG | Grid | outside_date |
| meta-el-paso | SOPAIPILLA | n/d | 1 (1) | Bondholders under the 144A offering (not named) (placeholder) | Hybrid | outside_date, lead_lenders |
| polaris-forge-1 | APLD_PF1 | n/d | 1 (1) | MUFG (agent) | Grid | outside_date |
| polaris-forge-1-building-4 | APLD_PF1B4 | n/d | 1 (1) | Goldman Sachs | Grid | outside_date |
| polaris-forge-2 | APLD_PF2 | n/d | 1 (1) | n/d | Grid | outside_date, lead_lenders |
| river-bend | HUT_RB | n/d | 1 (1) | n/d | Grid | outside_date, lead_lenders |
| stingray | CIFR_STINGRAY | n/d | 1 (1) | Noteholders (placeholder) | Grid | outside_date, lead_lenders, coordinates, campus_link |
| the-barn | SALINE | n/d | 1 (1) | Bank of America | Grid | outside_date |
