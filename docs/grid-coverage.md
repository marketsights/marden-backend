# Grid coverage

Which power regions Marden covers, where each region's interconnection queue (and large-load requests, where
the operator publishes them) comes from, which collector reads it, how often, and its status. Regions live in
`grid_region` (db/migrations/20261021150000_grid_region.sql): interconnections, ISO/RTOs, balancing
authorities (BAs), retail utilities, European TSOs and NERC reliability regions. Every campus, building, site
and power plant with a location gets `interconnection`, `iso`, `balancing_authority`, `utility` and
`utility_territories` facts from the spatial join (`grid_join`).

Status: **built** (collector runs and writes data) · **needs key** (free registration key required; the
collector stays inert until `<SOURCE>_API_KEY` is set) · **blocked** (terms forbid automated collection; the
source row is marked blocked) · **V2** (region registered, no collector yet).

Queue collectors in this workflow add or fill their own rows: section 2.2 has one row per US ISO/RTO with the
official queue page; the collector, cadence and status cells are left for the collector that reads it.
Collectors that are not queues (capacity auctions, load forecasts, large-load reports) go in section 3.

Last checked 2026-10-21.

## 1. Boundaries, balancing authorities and the spatial join

| Source | What it gives | Collector | Cadence | Status |
| --- | --- | --- | --- | --- |
| [EIA-930 reference tables](https://www.eia.gov/electricity/930-content/EIA930_Reference_Tables.xlsx) | All 81 BAs EIA-930 reports (72 US, 7 Canada, 2 Mexico): code, name, region, active, activation and retirement dates. Region gives the interconnection. | `eia930_ref` | monthly (2nd, 11:13 UTC) | built |
| [EIA-861 Balancing_Authority_<year>.xlsx](https://www.eia.gov/electricity/data/eia861/) (annual zip) | EIA BA ids -> codes (the crosswalk from HIFLD control areas to EIA-930 codes); adds Alaska and Hawaii BAs | `eia861_ba` | monthly (2nd, 11:19 UTC) | built |
| HIFLD Control Areas (DOE/ORNL; HIFLD Open modified 2025-04-08) | 71 BA boundaries, all 71 matched to an EIA code (61 by EIA id, 10 by name) | `hifld_grid` | monthly (2nd, 11:29 UTC) | built |
| HIFLD Independent System Operators (DOE; modified 2022-12-15) | footprints of the 7 US ISO/RTOs | `hifld_grid` | monthly | built |
| HIFLD NERC Regions (DHS HSIP; modified 2022-12-07) | 16 NERC subregions; their union gives the Eastern, Western and ERCOT interconnection boundaries (US portion) | `hifld_grid` | monthly | built |
| HIFLD Electric Retail Service Territories (DOE/ORNL; modified 2025-02-19) | 2,931 retail utility territories | `hifld_grid` | monthly | built |
| Spatial join (PostGIS, no network) | `interconnection`, `iso`, `balancing_authority`, `utility`, `utility_territories` facts on campuses, buildings, sites and plants (tag F, source HIFLD) | `grid_join` | daily (10:47 UTC) and after each `hifld_grid` load | built |
| Registry (code) | interconnections, the 7 US ISO/RTOs, 8 Canadian operators, 40 ENTSO-E TSOs + observer Moldelectrica + NESO, 4 European synchronous areas | `grid_registry` | on deploy | built (registration only) |

HIFLD files come from [HIFLD Next](https://hifld.publicenvirodata.org), which keeps the final HIFLD Open
release of each layer unchanged and versioned: DHS decommissioned HIFLD Open in August 2025, the original
services now answer "Invalid URL", and the EIA Energy Atlas "Balancing Authorities" layer answers "Token
Required". Terms quotes are on each collector's `source.terms_note`.

## 2. Regions

### 2.1 Interconnections and synchronous areas

| Region | `grid_region` id | Boundary | Queue source | Collector | Cadence | Status |
| --- | --- | --- | --- | --- | --- | --- |
| Eastern Interconnection | `interconnection:eastern` | union of NERC subregions (US part) | via the ISO/RTO and BA rows below | — | — | built (boundary) |
| Western Interconnection | `interconnection:western` | union of WECC subregions (US part) | via the ISO/RTO and BA rows below | — | — | built (boundary) |
| ERCOT Interconnection | `interconnection:ercot` | Texas RE (ERCOT) subregion | ERCOT (2.2) | — | — | built (boundary) |
| Québec Interconnection | `interconnection:quebec` | none | Hydro-Québec TransÉnergie (2.4) | — | — | V2 |
| Continental Europe synchronous area | `interconnection:continental-europe` | none | each TSO (2.5) | — | — | V2 |
| Nordic synchronous area | `interconnection:nordic` | none | each TSO (2.5) | — | — | V2 |
| Great Britain synchronous area | `interconnection:great-britain` | none | NESO (2.5) | — | — | V2 |
| Ireland and Northern Ireland synchronous area | `interconnection:ireland` | none | EirGrid, SONI (2.5) | — | — | V2 |

### 2.2 US ISO/RTOs

| ISO/RTO | `grid_region` id | EIA-930 BA | Boundary | Queue / large-load source | Collector | Cadence | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ERCOT | `iso:ERCOT` | ERCO | HIFLD ISOs | Generation: [GIS Report](https://www.ercot.com/mp/data-products/data-product-details?id=PG7-200-ER) (EMIL PG7-200-ER, monthly xlsx: Large Gen, Small Gen, Inactive, Cancellation sheets; INR ids, interconnecting entity, county, POI, fuel, MW, projected COD, milestones) read through the ERCOT Public API (`api.ercot.com/api/public-reports/archive/pg7-200-er`). Large loads: the separate large-load interconnection status collector | `ercot_gis` (generation) | weekly check (Tue 13:17 UTC); ERCOT posts monthly | **needs key**: www.ercot.com/robots.txt disallows `/misapp` and `/misdownload` (the only paths that list and download the GIS Report), so the report is read only through the Public API, which needs a free account registered by a person (bots may not register) plus its subscription key: set `ERCOT_API_KEY`, `ERCOT_API_USERNAME`, `ERCOT_API_PASSWORD`. Until then each run records "needs key" and makes no request; 0 entries on 2026-10-21. Each report is downloaded once (API terms: the same report at most 3 times in 12 months). The parser follows the published layout and has not yet read a live file |
| PJM | `iso:PJM` | PJM | HIFLD ISOs | [Serial queues: PlanningQueues.xml](https://www.pjm.com/planning/service-requests/serial-service-request-status) (A–AJ, LTF, upgrades) + [Cycle export, XML / All](https://www.pjm.com/planning/m/cycle-service-request-status) (TC1, TC2, Cycle 1; cycle rows supersede "moved to TC1/TC2" serial rows). Large loads: zone totals only, see `pjm_load_forecast` (3); the per-customer Large Load Registry is pending FERC (ER26-3515) | `pjm_queue` | weekly (Tue 11:23 UTC); PJM regenerates daily | built: 10,147 entries (1,792 cycle + 8,355 serial), 0 matched to our campuses/sites/plants on 2026-10-21. Data Miner 2 (free key, api.pjm.com) has no queue feed and is not used |
| MISO | `iso:MISO` | MISO | HIFLD ISOs | [GI interactive queue](https://www.misoenergy.org/planning/resource-utilization/GI_Queue/gi-interactive-queue/) (JSON `/api/giqueue/getprojects`); MISO's large-load material is on the same site | `miso_queue` | weekly attempt (Mon 11:13 UTC) | **unreachable**: every www.misoenergy.org URL, robots.txt and legal pages included, answers the declared crawler with HTTP 403 `cf-mitigated: challenge` (Cloudflare managed challenge). Not bypassed; each run records the 403 and writes nothing. MISO's free-key APIs (data-exchange.misoenergy.org, public-api.misoenergy.org) have no queue or large-load data |
| SPP | `iso:SPP` | SWPP, SWPW (RTO West since 2026-04-01) | HIFLD ISOs (2022: RTO West not included) | [GI request summary](https://opsportal.spp.org/Studies/GISummary) (all requests incl. withdrawn; [active only](https://opsportal.spp.org/Studies/GIActive)) via its CSV download (`/Studies/GenerateSummaryCSV`, "Last Updated" date). No project or customer names are published. No large-load list: the HILL/CHILL process posts meeting material only | `spp_queue` | weekly (Fri 11:53 UTC); SPP regenerates daily | built: 3,078 entries (627 active, 22 suspended, 339 in commercial operation, 2,056 withdrawn or terminated, 34 without a status) from the 2026-10-20 list; 0 matched (no names to match). **Terms:** SPP's Terms & Conditions require "prior, express written authorization" for any commercial use, which Marden does not have; `SPP` rows are flagged `unlicensed-commercial` in `marden.core.licensing` pending SPP's authorization or the owner's decision |
| CAISO | `iso:CAISO` | CISO | HIFLD ISOs | [Public queue report](https://www.caiso.com/library/public-queue-report) (`/documents/publicqueuereport.xlsx`: Grid GenerationQueue, Completed and Withdrawn sheets; project names, no interconnection customer). No load requests: large loads connect through PG&E, SCE and SDG&E, not the CAISO queue | `caiso_queue` | weekly (Fri 12:41 UTC); CAISO regenerates daily | built: 2,278 entries (260 active, 1 suspended, 252 completed, 1,765 withdrawn) from the 2026-10-21 report; 0 matched (none of our sites, campuses or plants is in CAISO territory). Cluster 15 requests are not in the report as of 2026-10-21 |
| NYISO | `iso:NYISO` | NYIS | HIFLD ISOs | [Interconnections (queue workbook)](https://www.nyiso.com/interconnections): Interconnection Queue, Cluster Projects, **Load Projects (large loads: data centers, crypto, fabs, hydrogen; fuel LOAD)**, In Service, Withdrawn sheets | `nyiso_queue` | weekly check (Wed 11:37 UTC); NYISO posts monthly | built: 2,220 entries from the 2026-09-30 workbook, 74 large loads; 2 matched to Lake Mariner (1670, 1732), written as `large_load_queue_nyiso` on the site |
| ISO-NE | `iso:ISO-NE` | ISNE | HIFLD ISOs | [IRTT public queue](https://irtt.iso-ne.com/reports/external) (HTML table; the Excel export needs an IRTT login and is not used). No large-load requests are published in the queue | `isone_queue` | weekly (Thu 11:47 UTC); page is live | built: 1,751 entries (units sharing an old queue position get `<QP>/<name>` ids), 0 matched |

### 2.3 US balancing authorities outside the ISO/RTOs

Every active US BA in EIA-930 that is not an ISO/RTO BA (the eight ISO/RTO BAs are in 2.2). Interconnection
requests in these areas go to the transmission provider under its OATT and are posted on its OASIS; there is
no common feed, so each is V2 until a utility-docket or OASIS collector exists. Retired BAs (kept as rows with
their retirement date so the join can flag stale boundaries): AEC, EEI, GLHB, GRIF, GRMA, HGMA, NSB, OVEC, WACM
(2026-04-01), WAUE, WAUW (2026-04-01), WWA.

| BA | Name (EIA-930) | Interconnection | Boundary | Queue / large-load source | Collector | Cadence | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| AECI | Associated Electric Cooperative, Inc. | Eastern | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| AVA | Avista Corporation | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| AVRN | Avangrid Renewables, LLC | Western | yes | none: generation-only BA (no load to connect) | — | — | V2 |
| AZPS | Arizona Public Service Company | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| BANC | Balancing Authority of Northern California | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| BHBA | Black Hills Energy | Western | no (BA started after the 2025-04-08 layer) | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| BPAT | Bonneville Power Administration | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| CHPD | Public Utility District No. 1 of Chelan County | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| CPLE | Duke Energy Progress East | Eastern | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| CPLW | Duke Energy Progress West | Eastern | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| DEAA | Arlington Valley, LLC | Western | yes | none: generation-only BA (no load to connect) | — | — | V2 |
| DOPD | PUD No. 1 of Douglas County | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| DUK | Duke Energy Carolinas | Eastern | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| EPE | El Paso Electric Company | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| FMPP | Florida Municipal Power Pool | Eastern | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| FPC | Duke Energy Florida, Inc. | Eastern | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| FPL | Florida Power & Light Co. | Eastern | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| GCPD | Public Utility District No. 2 of Grant County, Washington | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| GRID | Gridforce Energy Management, LLC | Western | yes | none: generation-only BA (no load to connect) | — | — | V2 |
| GVL | Gainesville Regional Utilities | Eastern | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| GWA | NaturEner Power Watch, LLC | Western | yes | none: generation-only BA (no load to connect) | — | — | V2 |
| HST | City of Homestead | Eastern | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| IID | Imperial Irrigation District | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| IPCO | Idaho Power Company | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| JEA | JEA | Eastern | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| LDWP | Los Angeles Department of Water and Power | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| LGEE | Louisville Gas and Electric Company and Kentucky Utilities Company | Eastern | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| NEVP | Nevada Power Company | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| NWMT | NorthWestern Corporation | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| PACE | PacifiCorp East | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| PACW | PacifiCorp West | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| PGE | Portland General Electric Company | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| PNM | Public Service Company of New Mexico | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| PSCO | Public Service Company of Colorado | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| PSEI | Puget Sound Energy, Inc. | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| SC | South Carolina Public Service Authority | Eastern | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| SCEG | Dominion Energy South Carolina, Inc. | Eastern | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| SCL | Seattle City Light | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| SEC | Seminole Electric Cooperative | Eastern | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| SEPA | Southeastern Power Administration | Eastern | yes | none: generation-only BA (no load to connect) | — | — | V2 |
| SIKE | Sikeston Board Of Municipal Utilities | Eastern | no (BA started after the 2025-04-08 layer) | none: generation-only BA (no load to connect) | — | — | V2 |
| SOCO | Southern Company Services, Inc. - Trans | Eastern | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| SPA | Southwestern Power Administration | Eastern | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| SRP | Salt River Project Agricultural Improvement and Power District | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| TAL | City of Tallahassee | Eastern | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| TEC | Tampa Electric Company | Eastern | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| TEPC | Tucson Electric Power | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| TIDC | Turlock Irrigation District | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| TPWR | City of Tacoma, Department of Public Utilities, Light Division | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| TVA | Tennessee Valley Authority | Eastern | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| WALC | Western Area Power Administration - Desert Southwest Region | Western | yes | transmission provider OASIS (generator and large-load requests) | — | — | V2 |
| YAD | Alcoa Power Generating, Inc. - Yadkin Division | Eastern | yes | none: generation-only BA (no load to connect) | — | — | V2 |

Alaska and Hawaii BAs from EIA-861 (no interconnection; boundaries from HIFLD Control Areas): AMPL, CEA, HECO.

### 2.4 Canada (registered, no collector)

| Operator | `grid_region` id | EIA-930 BA | Interconnection | Queue / large-load source | Collector | Cadence | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Alberta Electric System Operator (AESO) | `iso:AESO` | AESO | Western | AESO connection project list (to identify) | — | — | V2 |
| Independent Electricity System Operator (IESO, Ontario) | `iso:IESO` | IESO | Eastern | IESO connection assessment application status (to identify) | — | — | V2 |
| Hydro-Québec TransÉnergie | `ba:HQT` | HQT | Québec | TransÉnergie OASIS (to identify) | — | — | V2 |
| BC Hydro | `ba:BCHA` | BCHA | Western | BC Hydro OASIS / interconnection queue (to identify) | — | — | V2 |
| SaskPower | `ba:SPC` | SPC | Eastern | SaskPower OASIS (to identify) | — | — | V2 |
| Manitoba Hydro | `ba:MHEB` | MHEB | Eastern | Manitoba Hydro OASIS (to identify) | — | — | V2 |
| NB Power (New Brunswick System Operator) | `ba:NBSO` | NBSO | Eastern | NB Power OASIS (to identify) | — | — | V2 |
| Nova Scotia Power | `ba:nova-scotia-power` | (not in EIA-930) | Eastern | Nova Scotia Power OASIS (to identify) | — | — | V2 |

### 2.5 Europe (registered, no collector)

ENTSO-E's 40 member TSOs (36 countries), observer member Moldelectrica, and NESO (Great Britain's system
operator, not an ENTSO-E member since 2021). Ids are `tso:<abbreviation slug>`; the list is from
[ENTSO-E Member Companies](https://www.entsoe.eu/about/inside-entsoe/members/).

| TSO | Country | `grid_region` id | Synchronous area | Queue / connection source | Collector | Cadence | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| NESO (National Energy System Operator; National Grid Electricity Transmission owns the network) | GB | `tso:neso` | Great Britain | [TEC register](https://www.neso.energy/data-portal/transmission-entry-capacity-tec-register) | — | — | V2 |
| EirGrid | IE | `tso:eirgrid` | Ireland | to identify | — | — | V2 |
| SONI | GB (Northern Ireland) | `tso:soni` | Ireland | to identify | — | — | V2 |
| TenneT TSO B.V. | NL | `tso:tennet-nl` | Continental Europe | to identify | — | — | V2 |
| TenneT TSO GmbH | DE | `tso:tennet-de` | Continental Europe | to identify | — | — | V2 |
| Amprion | DE | `tso:amprion` | Continental Europe | to identify | — | — | V2 |
| 50Hertz | DE | `tso:50hertz` | Continental Europe | to identify | — | — | V2 |
| TransnetBW | DE | `tso:transnetbw` | Continental Europe | to identify | — | — | V2 |
| RTE | FR | `tso:rte` | Continental Europe | to identify | — | — | V2 |
| Elia | BE | `tso:elia` | Continental Europe | to identify | — | — | V2 |
| Energinet | DK | `tso:energinet` | Continental Europe (DK1) and Nordic (DK2) | to identify | — | — | V2 |
| Statnett | NO | `tso:statnett` | Nordic | to identify | — | — | V2 |
| Svenska kraftnät | SE | `tso:svenska-kraftnat` | Nordic | to identify | — | — | V2 |
| Fingrid | FI | `tso:fingrid` | Nordic | to identify | — | — | V2 |
| Red Eléctrica (REE) | ES | `tso:ree` | Continental Europe | to identify | — | — | V2 |
| Terna | IT | `tso:terna` | Continental Europe | to identify | — | — | V2 |
| PSE | PL | `tso:pse` | Continental Europe | to identify | — | — | V2 |
| Swissgrid | CH | `tso:swissgrid` | Continental Europe | to identify | — | — | V2 |
| The other 23 ENTSO-E TSOs and Moldelectrica: OST (AL), APG and VUEN (AT), NOS BiH (BA), ESO (BG), Cyprus TSO (CY), ČEPS (CZ), Elering (EE), IPTO (GR), HOPS (HR), MAVIR (HU), Landsnet (IS), Litgrid (LT), Creos Luxembourg (LU), AST (LV), CGES (ME), MEPSO (MK), REN (PT), Transelectrica (RO), EMS (RS), ELES (SI), SEPS (SK), Ukrenergo (UA), Moldelectrica (MD, observer) | — | `tso:<abbr>` | Continental Europe (Cyprus and Iceland isolated) | to identify | — | — | V2 |

## 3. Other grid collectors

Collectors that are not queues (capacity auctions, load forecasts, large-load reports, rates) add a row here.

| Region | Source | What it gives | Collector | Cadence | Status |
| --- | --- | --- | --- | --- | --- |
| PJM | [Load forecast page](https://www.pjm.com/planning/resource-adequacy-planning/load-forecast-dev-process) → Total Load Adjustments Breakdown (Table B-9b) | large-load adjustments to summer peak (MW) by zone and area, 2026–2046; written as `pjm_zone_large_load_adjustments` on our PJM sites and campuses whose utility names the zone (zone, not campus, values) | `pjm_load_forecast` | monthly (3rd, 12:29 UTC); published each January | built: cloudhq (DOM), qts-thunder (DOM, AEP/AEPOHIO, utility presumed) |
| PJM | [RPM page](https://www.pjm.com/markets-and-operations/rpm) → each auction's "Results XLS" workbook and "Report PDF" | Reliability Pricing Model Base Residual and 1st/2nd/3rd Incremental Auctions, delivery years 2020/2021 on: Resource Clearing Price ($/MW-day) and cleared MW by LDA (RTO, MAAC, EMAAC, SWMAAC, PS, PSNORTH, DPLSOUTH, PEPCO, ATSI, ATSI-CLEVELAND, COMED, BGE, PL, DAYTON, DEOK, DOM, JCPL as modeled), adjusted zonal capacity price ($/MW-day), zonal UCAP obligation and LDA by transmission zone (AE ... RECO), on `capacity:pjm:<zone>:<period>` (period `2027-2028` for a BRA, `2026-2027-3ia` for an IA); valid_from = the results posting date from the report; `capacity_auction` events on V1 sites in a PJM zone | `pjm_rpm` | monthly (2nd, 13:53 UTC); BRAs annual, IAs a few times a year | built: 20 auctions (2020/21 BRA to 2028/29 BRA), 4,092 facts on 2026-10-21; latest 2028/2029 BRA (posted 2026-07-14) $325.00/MW-day in every LDA and zone. Pre-2020/21 results are legacy .xls files with separate Base and CP products and are skipped. No V1 site is in PJM (cloudhq, not V1, maps to DOM) |
| NYISO | [ICAP market results](https://www.nyiso.com/installed-capacity-market) on the public ICAP AMS (icappublic.nyiso.com): Capability Period (strip), monthly and spot auction summaries, plus the season list JSON | capacity clearing prices ($/kW-month as `clearing_price_native`, converted to $/MW-day as `clearing_price`) and awarded MW by locality NYCA, G-J (Lower Hudson Valley), NYC (J), LI (K), on `capacity:nyiso:<locality>:<period>` series; `capacity_auction` events on linked V1 sites (a site's `capacity_zone` is its load zone A-K, mapped to its locality) | `nyiso_icap` | monthly (2nd, 11:17 UTC); strips post early April/October, monthly auctions mid-month, spot auctions month-end | built: Summer 2021 to Winter 2026-27, 157 pages, 7,560 facts on 2026-10-21 (latest: Winter 2026-27 strip NYCA $3.50/kW-month, NYC $8.02; Nov 2026 monthly NYCA $2.72) |
| ISO-NE | [FCA results report](https://www.iso-ne.com/static-assets/documents/2018/05/fca-results-report.pdf), [FCA capacity obligations](https://www.iso-ne.com/static-assets/documents/2018/02/fca_obligations.xlsx), results press releases linked from [key stats](https://www.iso-ne.com/about/key-stats/markets) | Forward Capacity Auction payment rate by capacity zone ($/kW-month as `clearing_price_native`, x 1000 / 30.4167 as `clearing_price` $/MW-day), cleared MW (sum of the zone's CSOs, checked against ISO-NE's Total Capacity Acquired) and the zone's load zones, FCA 15 on, zones ROP, SENE, NNE, MAINE as modeled, on `capacity:isone:<zone>:<commitment period>`; valid_from = the press release date; `capacity_auction` events on V1 sites in an ISO-NE zone | `isone_fca` | monthly (4th, 14:53 UTC); one FCA a year until the prompt/seasonal auction (first period 2028/2029, no results yet) | built: FCA 15-18, 112 facts on 2026-10-21; latest FCA 18 (2027/2028, announced 2024-02-09) $3.580/kW-month = $117.70/MW-day in ROP, NNE and MAINE. The ISO Express FCA tree is not used (CAPTCHA on bulk download). No V1 site is in ISO-NE |
| MISO | PRA Results Posting decks on cdn.misoenergy.org (one per planning year, "<Season> PRA Results by Zone" tables) | seasonal clearing price ($/MW-day, ACP) and committed MW (offer cleared + FRAP) by LRZ 1-10 on `capacity:miso:lrz<N>:<planning year>-<season>`; `capacity_auction` events on linked V1 sites | `miso_pra` | monthly (3rd, 11:29 UTC); results post once a year (late April) | partial: 2024-25 and 2025-26 loaded (400 facts). The 2026-27 deck URL (2026 PRA Results Posting 20260428754715.pdf) answers 403 AccessDenied on 2026-10-21, so every run is recorded as failed; the 2026 fact sheet is stored and queued for extraction (its prices are an image). www.misoenergy.org is behind a Cloudflare challenge (2.2), so new decks cannot be discovered automatically: a person adds each year's URL to `miso_pra.POSTINGS`. The 2023-24 deck (first seasonal PRA) is not on the CDN under any URL found |
| All | our sites, campuses and plants: `iso` (grid_join) or `power_iso`, `utility` / `power_utility` | `capacity_market` (pjm, isone, miso, nyiso or none), `capacity_zone` (PJM transmission zone via the utility name, ISO-NE capacity zone of the latest FCA by state, MISO LRZ and NYISO zone A-K where our facts name them) and `capacity_market_reason` for regions with no capacity auction; tag M (capacity_zones-0.1); a market field naming several markets (a multi-site portfolio) decides over grid_join's single boundary point: 'none' when none of those markets runs a capacity auction, else unassigned; no grid fact = unassigned | `capacity_zones` | weekly (Mon 09:41 UTC) and after each capacity collector run | built: 25 of 29 sites assigned on 2026-10-21 (V1: 7 MISO, 1 NYISO, 16 none, coreweave-ddtl-4 unassigned: no grid facts) |
| ERCOT | — | no capacity auction: ERCOT is an energy-only market (no capacity obligation on load); sites get `capacity_market` = 'none' and the reason, no `capacity_zone` | `capacity_zones` | weekly | n/a (no market to collect) |
| CAISO | — | no capacity auction: resource adequacy is bilateral (CPUC and local regulatory authority RA programs; CAISO's Capacity Procurement Mechanism is a backstop, not a market price); `capacity_market` = 'none' | `capacity_zones` | weekly | n/a (no market to collect) |
| SPP | — | no capacity auction: load-serving entities meet SPP's planning reserve margin (Resource Adequacy Requirement, Tariff Attachment AA) with owned or bilaterally contracted capacity; `capacity_market` = 'none' | `capacity_zones` | weekly | n/a (no market to collect) |
| Non-RTO (TVA, Southeast, West outside CAISO) | — | no organized capacity market; `capacity_market` = 'none' | `capacity_zones` | weekly | n/a |

## 4. Known gaps

- **Boundary vintages.** The ISO/RTO footprints are from 2022 and the control areas from 2025-04-08, so SPP RTO
  West (EIA-930 BA SWPW, 2026-04-01), Black Hills (BHBA, 2026-04-01) and Sikeston (SIKE, 2025-06-01) have no
  boundary yet, and WACM/WAUW still have theirs. The join turns a point's `balancing_authority`, and its
  `iso` when the retirement is newer than the ISO layer, into a candidate with a review item whenever the BA
  is retired per EIA-930. New HIFLD Next versions are picked up by the monthly run.
- **NERC Regions** were sourced in 2010 (validated 2022-06-03) and still show FRCC and SPP RE; they are used only
  to derive interconnection boundaries. Seams are checked against EIA-930: if a BA containing the point belongs
  to another interconnection, the `interconnection` fact waits for review (Lubbock-area sites, the Texas
  panhandle).
- **Overlaps.** ISO and BA polygons overlap on seams (ERCOT/SPP in the Texas panhandle, MISO/SPP in North
  Dakota): each candidate is written and one review item opened per subject and field. Retail territories are
  mostly drawn at county resolution, so co-ops, municipals and IOUs overlap: `utility` is published only when one
  territory contains the point; otherwise `utility_territories` lists them (on 2026-10-21, 1 of 21 located
  financed sites had a single territory).
- **Outside the US** no boundaries are loaded; Canadian and European rows are registrations only.
- **Queues without names** (SPP; CAISO has project names but no customer) cannot meet queue-match@0.1, so
  entries in our counties stay unmatched even when they sit next to a site: e.g. SPP ERAS-2025-002 (217 MW
  combustion turbine, "Abernathy", TX, at Xcel's Tuco 345 kV, IA pending) near Abernathy, and SPP entries in
  Muskogee County, OK (Core Scientific Muskogee). Linking those needs POI or coordinate evidence.
- **Queue matching** (`iso_queue_match`, queue-match@0.1). Queues publish no coordinates, so an entry links to
  one of our sites, campuses or plants only on state + county + a distinctive name of ours (site, alias,
  SPV, developer, tenant) in the entry's project name or customer; the evidence is in the link fact. Entries
  filed under other names stay unmatched, e.g. NYISO 0849 "Somerset Load" (Somerset Operating Company, 250 MW,
  in service at Kintigh 345 kV, the Lake Mariner substation). MISO, home to Hyperion, River Bend, Lighthouse,
  The Barn and Polaris Forge, is not reachable (2.2).
