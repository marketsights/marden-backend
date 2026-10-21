# 15 · On-site power (`/power`, `/power/:id`)

**Job:** every behind-the-meter power plant built or permitted to serve a US data center: units by OEM and model, capacity, air permit, fuel supply, the data center it serves, and the debt that depends on it.
**Reference:** screens/15-onsite-power.html, 15b-onsite-power-map.html, 15c-plant-detail.html (Crusoe Abilene generation) · **Endpoints:** `GET /api/power-plants`, `GET /api/power-plants/summary`, `GET /api/power-plants/:id`, `GET /api/export/power-plants.xlsx`
**Data:** state air permits and technical reviews first, then EIA-860M, FERC and state pipeline records (spec 17).

## List view (`/power`)

### Layout

1. PageHeader "On-site power"; subtitle "Power plants built to run data centers, from air permits and filings." Actions: Excel, Saved views, Map / Table toggle.
2. KPI strip (respects filters):
   - Plants tracked · units
   - MW by status: Permitted / Under construction / Operating / Withdrawn
   - MW by technology: Simple-cycle turbine · Reciprocating engine · Fuel cell · Combined cycle · Other
   - Permits filed or decided in the last 30 days
3. Chart: MW by OEM (horizontal bars, stacked by status), click a bar to filter. Answers "who supplies the equipment" (GE Vernova, Solar Turbines, INNIO Jenbacher, Siemens Energy, Caterpillar, Bloom, others).
4. Filter chips: State · Status · Technology · OEM · Model · Developer / operator · Data center served · Permit agency · Permit type · Grid mode (Islanded, Grid-tied, Unknown) · Financed only · Watchlist only. Search (plant, permit number, OEM model).
5. Table.

### Table columns (one row per plant; expand for units)

| Column | Content | Source |
| --- | --- | --- |
| Plant | Name (link) | Permit |
| Serves | Data center campus (link) | Permit narrative, filings |
| Operator | Developer / power provider (e.g. VoltaGrid, Crusoe) | Permit applicant |
| Location | County, state | Permit |
| Technology | Turbine / engine / fuel cell | Permit equipment list |
| Units | Count, with OEM · model breakdown on hover | Permit equipment list |
| Permitted MW | Sum of unit ratings | Code from permit (F) |
| Status | Pill: Permit pending · Permitted · Under construction · Operating · Withdrawn · Denied | Permit and records |
| Air permit | Agency + number + type (Standard, Minor, Major, Title V) + date | Agency portal (F) |
| Fuel supply | Pipeline / interconnect and in-service date | FERC, state pipeline permits |
| Debt | Chain status + $B when the served site is financed | Debt chains |
| Last change | Date + one line + tag | Latest fact |

Expanded row (units): OEM, model, count, rating per unit (MW), total MW, fuel, status, online year, permit reference.

## Plant detail (`/power/:id`)

### Header

Breadcrumb (On-site power / county, state); plant name; status pill; facts line: permitted MW · units · technology · operator · serves {campus}. Actions: Watch, Excel, Share. Financed banner as on campus detail.

### Sections

1. **Units table:** OEM · model · count · rating each · total MW · status · online year · permit reference. Totals row.
2. **Air permit card:** agency, permit or registration number, type, status, filed date, decided date, link to the record; permitted emission limits listed only if stated in the permit; amendments in order.
3. **Fuel supply:** pipeline or interconnect, operator, regulator (FERC or state), docket or permit number, planned and latest in-service dates with each change.
4. **Data center served:** campus card (status, MW, buildings) with link; the share of campus load the plant can cover (permitted MW ÷ facility MW, tag M).
5. **History:** permits filed, amended, withdrawn or decided; unit changes; status changes, each with its record.

## Status rules

| Status | Requires |
| --- | --- |
| Permit pending | Application filed with the air agency |
| Permitted | Permit or registration issued |
| Under construction | Construction record, OEM delivery report, or imagery change at the plant footprint |
| Operating | Agency compliance record, EIA operating status, or operator filing |
| Withdrawn / Denied | Agency record |

## Real examples to seed and test against

Checked Oct 20, 2026. Corrections to the earlier draft are marked.

| Plant | Units | Permitted MW | Permit | Source |
| --- | --- | --- | --- | --- |
| Crusoe Abilene generation (Stargate Abilene) | 5 Solar Turbines Titan 350 (38.0 MW each) + 5 GE LM2500 (34.1 MW each) | 360.5 (sum of unit ratings) | TCEQ EGU Standard Permit (minor), Registration 177263: filed Aug 30, 2024 for 6 + 6 units; amendment received Jan 11, 2025, approved Jan 22, 2025 (5 + 5 units, 8,760 hours each). **Correction:** Jan 22, 2025 is the amendment approval, not the first approval. | TCEQ technical review (project 387309) |
| Crusoe Abilene expansion (buildings 9–10) | 29 GE LM2500 + 12 "Aero DT35" (maker n/d), 34.1 MW each; 18 Cat 3516 diesel backup, 3 MW each | 1,398 (M: 41 × 34.1; no source states a total) | TCEQ 182126 / PSDTX1688 / GHGPSDTX263, major (PSD). **Correction:** filed Nov 7, 2025, not Jul 2026; draft permit noticed Aug 5, 2026; not issued | TCEQ notice of receipt; Aterio |
| VoltaGrid ABI-1 (Vantage Frontier) | 210 INNIO Jenbacher J620 / J624 (197 primary, 13 backup). **Correction:** the 160 / 50 split was not found in any source | ~700 stated (GEM lists 714 for unit 1) | TCEQ 182467 / PSDTX1692 / GHGPSDTX266, major (PSD), filed Dec 12, 2025; preliminary decision noticed Sep 22, 2026; **not issued, so status is Permit pending** (press said "approved"; no permit found) | TCEQ notices; Construction Owners |
| Jupiter microgrid (Project Jupiter) | Up to 2.45 GW of Bloom Energy solid-oxide fuel cells (~7,000 units), filed by Yucca Growth Infrastructure; replaces the gas turbine permits withdrawn Apr 27, 2026 | 2,450 stated | NMED application pending; redesign filed Oct 15, 2026; permit hearing moved to Dec 7, 2026 | ConstructConnect; mgrid |

Each value above must be re-pulled from the agency record by the pipeline before it ships; the table is the test set, not typed content.

## Acceptance

- Every unit row traces to the permit page or equipment list it came from.
- Permitted MW = sum of unit ratings, computed in code; disagreements with a stated plant total are flagged to the review queue.
- The OEM chart and KPI strip equal the filtered table totals.
- A new or decided permit for a watched site's plant fires a power alert within one day of the agency posting it.
