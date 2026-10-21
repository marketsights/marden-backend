# 16 · Linking financed sites to buildings and plants (changes to existing screens)

**Job:** every financed site in a debt chain points to its real buildings and on-site plant, so the schedule, evidence and power views read from building-level records, and every data center or plant row shows the debt that depends on it.
**Reference:** screens/04h-site-buildings.html (Jupiter Buildings tab); Buildings column on screens/03-completion.html and 05-sites.html · **Data:** `site_link` and `plant_serves` (spec 17). Links are approved once by a person; new buildings on linked parcels attach automatically.

## Site detail (spec 04)

- **New tab "Buildings"** after Power (tab bar: Schedule · Evidence · Power · Buildings · Opposition · Material events · Debt and holders · Lease terms). Content: mini map with footprints colored by status; buildings table (label, square feet, MW, status, permit, certificate of occupancy, first visible in imagery); "Open campus →" to Data centers detail.
- **Schedule tab:** milestones "Building shells" and "First hall energized" read from building status and records (count of shells up, first certificate of occupancy or energization); evidence shows the building record.
- **Power tab:** the on-site plant card links to the plant detail (spec 15) and shows units by OEM and model and the permit status.
- **Header facts line:** add "{n} buildings".

## Completion monitor (spec 03) and Sites (spec 05)

- Add a "Buildings" column: built / total (e.g. "2 / 8").
- Status and cushion unchanged; building records become evidence for the milestones.

## Data centers (spec 14) and On-site power (spec 15)

- Debt column shows the linked site's chain status and debt; the row's campus detail shows the financed banner.

## Watchlist and alerts (spec 11)

- Watched sites inherit building and plant alerts: new building, building status change, plant permit filed, decided, amended or withdrawn.

## Global (spec 00)

- Sidebar BROWSE section becomes: Sites · Data centers · On-site power · Instruments · Payers.
- Search returns campuses, buildings (by label and parcel ID) and plants as well.

## Acceptance

- All V1 financed sites have an approved link to at least one campus, and to a plant where one exists.
- Opening any financed site shows its buildings; opening any linked campus or plant shows the debt chain.
- No link exists without an approved evidence record.
