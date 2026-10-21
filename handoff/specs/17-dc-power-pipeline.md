# 17 · Data center and on-site power pipeline (back end)

**Job:** build and keep current Marden's own dataset of every US data center (campus and building) and every behind-the-meter plant, entirely from public records, with no licensed third-party dataset. Agents find, extract and check; code computes; a person approves only what changes a financed site's status or fires an alert.

## Sources by field

| Field | Primary source | Fallback | Method |
| --- | --- | --- | --- |
| Discovery (new campus) | State air permits for backup generators and on-site plants; county zoning, site plan and rezoning agendas | Utility large-load requests and rate cases; state data center tax-exemption programs; REIT and developer SEC filings; press | Per-state and per-county collectors; classification agent flags data center filings |
| Building footprints | Overture Maps and Microsoft building footprints | County parcel and permit plans | Spatial match to permit address and parcel |
| Location | County parcel (assessor) geometry | Permit address geocoded | Code |
| Facility MW | Utility load agreement or filing; developer filing | Backup generator count × rating from the air permit (tag M) | Extraction agent + arithmetic |
| IT MW | Developer or tenant filing, permit narrative | Spec 27 estimate (facility MW ÷ PUE, range, tag M) | Extraction agent; spec 27 model |
| Square feet | Building permit, assessor record | Footprint area × stories (M) | Extraction + code |
| Status | Building permit, certificate of occupancy, utility energization | Sentinel-2 change detection | Status rules in spec 14 (Data centers) |
| Developer | Permit applicant; parcel owner | Press | Entity resolver to parent |
| Owner of record | County assessor | Deed records | Collector |
| Tenant / end user | Recorded memorandum of lease; SEC filings; utility agreements | Press (tag R) | Researcher agent per campus |
| Utility, balancing authority, ISO/RTO | Federal utility service territory and balancing authority boundaries (HIFLD, EIA) | — | Spatial join |
| Interconnection / large load | ISO queues and large-load reports (ERCOT, PJM, MISO, SPP); state utility dockets | — | Collectors + extraction |
| On-site plant units, OEM, model | State air permits and technical reviews (TCEQ, NMED, Ohio EPA, others) | EIA-860M (grid-tied plants); OEM and press | Extraction agent reads the equipment list |
| Fuel supply | FERC eLibrary (interstate), state pipeline permits (e.g. Texas RRC) | Press | Collectors + extraction |
| Opposition | County agendas and minutes; state court dockets | Press | Collector + classification |

## Data model (Postgres)

- `dc_campus` (id, name, aliases[], developer_entity_id, owner_entity_id, county, state, centroid geometry, iso, balancing_authority, utility, power_source, first_seen_at)
- `dc_building` (id, campus_id, label, footprint geometry, parcel_ids[], status, facility_mw, it_mw, sqft, permit_ids[], co_date)
- `power_plant` (id, name, operator_entity_id, county, state, location, technology, status, permitted_mw, eia_plant_id, grid_mode)
- `power_unit` (id, plant_id, oem, model, count, rating_mw_each, fuel, status, online_year, permit_ref)
- `plant_serves` (plant_id, campus_id, evidence_fact_id)
- `site_link` (financed_site_id, campus_id, building_ids[] nullable = all, evidence_fact_id, approved_by, approved_at)
- `entity` (id, name, parent_id, aliases[]) shared with the debt chains.
- Every field value lives in the shared `fact` table (value, tag, source document, quote, valid_from, learned_at); the tables above are read models rebuilt from approved facts.

## Linking financed sites to buildings

1. The Linker agent proposes links from a financed site to campuses and buildings using: tracking and SPV names (e.g. Red Chiles A–D LLC → Jupiter), parcel IDs in filings, permit applicant entities, addresses and coordinates, and plant permits that name the site.
2. Each proposal carries its evidence; code checks distance (≤ 5 km unless a document names the campus) and entity match.
3. A person approves each site link once (it can move a status, so it is material). After approval, new buildings on the same parcels link automatically.
4. The link drives: Site detail gets a Buildings tab and building-level evidence; the Completion milestones "building shells" and "first hall energized" read from building status; Data centers and On-site power rows show the chain.

## Rollout

| Phase | Coverage |
| --- | --- |
| Week 1 | Every V1 financed site linked at building level; on-site plants in TX, NM and OH from air permits |
| Weeks 2–4 | Discovery collectors for VA, TX, GA, OH, AZ, IN, PA (backup generator permits + county zoning), utility and ISO joins nationwide |
| Months 2–3 | All remaining states; tenant fill-in by the Researcher agent; building-level imagery for every campus |

## Quality and coverage

- A coverage table recomputed nightly: campuses, buildings, and fill rate per field (developer, status, facility MW, IT MW, square feet, coordinates, utility, ISO, tenant, online year), overall and by state. Shown on Track record and methods and in the Data centers footer.
- Duplicate detection nightly (same parcel, same developer, within 1 km) → merge proposals in the review queue.
- Watchdog checks: building MW sums vs campus MW; status regressions (operating → planned) blocked without a record; plants with permitted MW but no served campus; campuses with on-site power but no plant record.
- Golden set: 100 campuses checked by hand against their records, re-run on every extraction change; accuracy per field reported.

## Freshness

| Source | Cadence |
| --- | --- |
| State air permit portals | Daily |
| County agendas, building permits (top 100 counties) | Daily; others weekly |
| ISO queues and large-load reports | As published (weekly or monthly) |
| Sentinel-2 imagery | Each pass (~5 days), change score weekly |
| Footprints and parcels | Quarterly refresh |
| Federal territory and balancing authority boundaries | On release |
