# 14 · Data centers (`/data-centers`, `/data-centers/:id`)

**Job:** every US data center campus and building Marden tracks (target ~4,200 campuses), filterable, with building-level detail, its power, and the debt chain where one is financed.
**Reference:** screens/14-data-centers.html (table), 14b-data-centers-map.html, 14c–14g campus detail tabs (Stargate Abilene) · **Endpoints:** `GET /api/data-centers`, `GET /api/data-centers/summary`, `GET /api/data-centers/:id`, `GET /api/data-centers/:id/buildings`, `GET /api/data-centers/:id/records`, `GET /api/data-centers/:id/history`, `GET /api/export/data-centers.xlsx`
**Data:** built from public records only (spec 17). Every field is `Sourced<T>`; missing = n/d.

## List view (`/data-centers`)

### Layout

1. PageHeader "Data centers"; subtitle "Every US data center Marden tracks, by campus and building. Built from permits, filings and imagery." Actions: Excel, Saved views, Map / Table toggle.
2. KPI strip (from `/summary`, respecting filters):
   - Campuses tracked (count) · buildings built of total
   - MW by status: stacked bar Planned / Under construction / Operating, with totals
   - New this week: campuses first seen in the last 7 days, with the newest name
   - Financed by a Marden chain: count + debt ($B), link "Show only financed"
3. Filter chips: State · County · Status · Developer · Tenant / end user · Utility · ISO/RTO · Power source (Grid, On-site gas, Hybrid, Unknown) · MW range · First seen (date) · Has opposition · Financed only · Watchlist only. Search box (name, developer, parcel ID, permit number).
4. Table or map (toggle keeps filters).

### Table columns (one row per campus; expand a row for its buildings)

To fit 1440 px without horizontal scroll, Location shows under the campus name, Tenant under Developer, and ISO/RTO with the utility under Power (as in the design). Last change is clamped to two lines with the full text on hover.

| Column | Content | Source |
| --- | --- | --- |
| Campus | Name (link) + aliases on hover | Permits, filings |
| Location | County, state | Parcel |
| Developer | Parent entity (SPV on hover) | Permit applicant, parcel owner |
| Tenant | End user / tenant, or n/d | Lease memo, filings, press (R) |
| Status | Pill: Planned · Under construction · Operating · Paused · Cancelled | Status rules below |
| Facility MW | Number + tag | Utility filing, permit (M if from generator capacity) |
| IT MW | Number + tag | Filings |
| Buildings | built / total | Building records |
| Power | Grid utility, or On-site gas with plant link, or Hybrid | Territory join, plant records |
| ISO/RTO | ERCOT, PJM, MISO… | Boundary join |
| Online | First / expected year | Filings, CO, utility |
| Debt | Chain status pill + $B when financed, else — | Debt chains |
| Last change | Date + one line + tag | Latest fact |

Default sort: Last change, newest first. Server-side sort, filter and pagination (50 rows per page, virtualized). Expanded row: building sub-table (label, sq ft, MW, status, permit, CO date).

### Map view

- MapLibre with free vector tiles. Campus points clustered at low zoom; at zoom ≥ 13, building footprints colored by status. Click → side panel with the campus summary and "Open".
- Layers (toggle): ISO/RTO boundaries, on-site plants, financed sites only.
- The map uses the same filters and the same endpoint (GeoJSON format via `?format=geojson`).

## Campus detail (`/data-centers/:id`)

### Header

Breadcrumb (Data centers / county, state); campus name; status pill; facts line: developer · tenant · facility MW · IT MW · buildings · power source. Actions: Watch, Excel, Share.
If linked to a financed site: banner "Financed: {site} · {chain status} · ${debt}B · Open the chain →".

### Tabs

| Tab | Content |
| --- | --- |
| Overview | Facts table (every field with value, tag, source, as-of); map with footprints; latest Sentinel-2 image with month; key dates (announced, zoning approved, permits, construction start, first CO, energized). |
| Buildings | Table: label, square feet, facility MW, IT MW, status, building permit (link), CO date, first visible in imagery, change score (M). Row click highlights the footprint on the map. |
| Power | Utility, balancing authority, ISO/RTO; large-load request and queue position (if filed); on-site plant card (links to spec 15) with units by OEM and model; backup generators from the air permit (count, MW, fuel). |
| Records | Every source document: date, type chip (Permit, Zoning, Filing, Utility, Court, Press, Imagery), what it says, agency, tag, Open. Filter chips by type. |
| History | Status and capacity changes over time (event log with before → after), first-seen date, merges. |

## Status rules (fixed; shown on Track record and methods)

| Status | Requires one of |
| --- | --- |
| Planned | Zoning, site plan or rezoning filed or approved; air permit application; utility large-load request; developer filing |
| Under construction | Building permit issued and visible change in imagery, or a filing or inspection record showing construction |
| Operating | Certificate of occupancy, utility energization record, or an operator filing stating the site is live |
| Paused | Filing, permit lapse or court order halting work; no imagery change for 6 months while under construction |
| Cancelled | Withdrawn zoning or permits, or a developer or tenant statement |

A campus's status is its most advanced building's status. Press alone can move a campus to Planned only (tag R); every other status needs a record.

## States

- Loading: skeleton table rows; map shows tiles first, then points.
- Empty filters: "No data centers match. Clear filters."
- Field not found in any record: "n/d" with "Searched: {sources}" on hover.

## Acceptance

- List loads first page in under 1 second with any filter combination; map renders 5,000 points without lag.
- Every value shows its tag and opens its source.
- Counts on the KPI strip equal the filtered table totals.
- Excel exports the filtered rows (campus sheet, building sheet, sources sheet).
- Coverage rates per field are shown in the page footer ("Facility MW known for {x}% of campuses") and on Track record.
