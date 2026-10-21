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
| ERCOT | `iso:ERCOT` | ERCO | HIFLD ISOs | [Resource Integration (GINR reports)](https://www.ercot.com/gridinfo/resource); large-load interconnection status reports | _queue collector fills_ | | |
| PJM | `iso:PJM` | PJM | HIFLD ISOs | [Service request status / PlanningQueues.xml](https://www.pjm.com/planning/service-requests/services-request-status) | _queue collector fills_ | | |
| MISO | `iso:MISO` | MISO | HIFLD ISOs | [GI interactive queue](https://www.misoenergy.org/planning/resource-utilization/GI_Queue/gi-interactive-queue/) | _queue collector fills_ | | |
| SPP | `iso:SPP` | SWPP, SWPW (RTO West since 2026-04-01) | HIFLD ISOs (2022: RTO West not included) | [GI active requests](https://opsportal.spp.org/Studies/GIActive) | _queue collector fills_ | | |
| CAISO | `iso:CAISO` | CISO | HIFLD ISOs | [Public queue report](https://www.caiso.com/library/public-queue-report) | _queue collector fills_ | | |
| NYISO | `iso:NYISO` | NYIS | HIFLD ISOs | [Interconnections (queue workbook)](https://www.nyiso.com/interconnections) | _queue collector fills_ | | |
| ISO-NE | `iso:ISO-NE` | ISNE | HIFLD ISOs | [IRTT public queue](https://irtt.iso-ne.com/reports/external) | _queue collector fills_ | | |

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
