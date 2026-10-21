# Placeholder register

The designs mix real sourced facts with placeholder values. Nothing below may ship as typed text: each comes from the model or a document once the pipeline runs.

## Real values used in the designs (F or R, as of Oct 19, 2026)

Jupiter: pipeline right-of-way denied Mar 20; turbine air permits withdrawn Apr 27; S&P cut Oracle to BBB- Jul 9; rehearing denied Jul 14; FERC filings move pipeline to 2027 (Aug 17); loan quoted 89–91c Sep 19; Oracle force majeure notice Sep 24 (reported); Oracle letter to FERC Oct 13 (pipeline in service Feb 1, 2027); ~$18B loan; 2.2–2.45 GW; lease lets Oracle stop rent if completion slips past 2028.
Jupiter opposition (Albuquerque Journal, The Bond Buyer): Sep 19, 2025 county ordinance for up to $165B taxable IRBs; Oct 2025 bond suits (Empowerment Congress and residents; a county resident); Feb 2026 Open Meetings Act suit; Mar 2026 motion to dismiss denied; Jun 22, 2026 public-records suit. Jupiter equity ~$3B (Blue Owl); lenders SMBC, BNP Paribas, Goldman Sachs, MUFG, Santander and Jefferies holding allocations.
Other sites: loan sizes (Frontier $23.0B, Lighthouse $15.0B, Barn $14.0B, Abilene ~$9.4B, Childress $3.65B), GW, lead lenders, latest-evidence lines.
Markets: Oracle 5y CDS 288bp (Oct 15); CoreWeave 5y CDS ~1,194bp intraday (Oct 15); CoreWeave 9.00% 2031 ($1.75B, yield 15.6%, 9.3% in June); funds' Jun 30 marks 98.9–99.8 (N-PORT); Barn bonds marked 99.72 (Jun 30); BB HY index 6.84%; Anthropic junior 8.5% → 10%+; backstopped tier 5.75–5.9%; Microsoft 62% of CoreWeave 2024 revenue; Oracle notes $125.3B; Oracle >$7B in letters of credit.

## Placeholders (M or bracketed)

| Where | Value(s) | Real source |
| --- | --- | --- |
| Completion, Sites, Payer, Home, Site detail | P10/P50/P90 completion, cushions, rent start, odds of missing, cost per month late, cushion history, outside dates other than Jupiter's 2028 | Cushion model + lease documents |
| Completion KPI | Debt-by-cushion split, "49%" | Computed from the above |
| All screens | Marden prices, 1w changes, mark gaps, recovery values | Pricing and recovery engines |
| Instrument detail | Holder rows in [brackets], 23 funds / $412mm par, NAV hits, share of NAV, price path between real points | N-PORT / BDC 10-Q parsing; pricing engine |
| Instrument detail | Guarantees, covenants [from the indenture] | Indenture / offering memo |
| Instruments | Maturities shown as [ ], CoreWeave 9.625% 2032 size, Anthropic sizes (~$22.5B junior, ~$72B senior) to verify | Offering documents |
| Stress | All outputs (prices after, coverage, fund counts 206/212, $3.8B, $8.1B) | Stress engine |
| Risk list, Home | Rank, odds, movement, note text | Repricing model + AI agent (human-checked) |
| Track record | 14 cases "backtest running"; model versions; source run times | Backtest + collector heartbeats |
| Site detail | Satellite thumbnails (hatched placeholders), "little change since Jun" | Sentinel-2 pipeline |
| Payer | ~6.7 GW (sum of reported site GW), 206 holders | Sum / holder parsing |
| Site detail tabs | Satellite compare images and change scores; permitted on-site GW; [docket no.]; recovery inputs in brackets; lender shares (n/d); lease terms in brackets; "Incentives lost" impact | Imagery pipeline, permit count, collectors, recovery model, lease or filings |
| Completion, Payer, Stress, Home | Frontier cushion +2 and status Strained | Needs the lease outside date and power timing. Not verified: a claim (via SAVRN, citing SemiAnalysis) that Frontier's VoltaGrid plant slips to 2029. Checked sources do not say 2029: DigitalBridge's 3Q25 8-K says phased completion by 2028; Cleanview projects most power in 2027 and saw no generators on site Apr 13, 2026. If a 2029 power date is confirmed, Frontier turns Broken on every screen. |
| Account | [Portfolio manager], [Analyst], [name@fund.com], [$ per year], [date] | Account data |
| Widgets | [High-yield fund A] etc. | Holder parsing |

## Data centers and On-site power (specs 14–17)

| Where | What is shown | What replaces it |
| --- | --- | --- |
| Data centers, On-site power | A 16-campus, 10-plant seed set researched Oct 20, 2026, with KPI totals computed from it | The pipeline's full set (target ~4,200 campuses); KPIs recompute from the filtered rows |
| Campus Buildings tab, Data centers expanded row | Building in-service dates and Building 1 IT MW from Epoch AI imagery estimates (tag R) | Certificates of occupancy, utility energization records, building permits |
| Campus Overview and Buildings, Site Buildings tab | Schematic site plans | Real footprints from Overture Maps / Microsoft footprints on MapLibre |
| Campus Overview | Sentinel-2 image box | Latest Sentinel-2 tile for the campus |
| Campus Buildings tab | Change score column "—" | Weekly Sentinel-2 change model (M) once it runs |
| Data centers (Barstow) | Debt n/d; Nscale's $1.85B term loans against Barstow (Sep 3, 2026, secondary source) listed as in review | Add to the Barstow chain after a reviewer confirms the lender filing |
| On-site power (VoltaGrid ABI-1) | "Permit pending"; 700 MW stated total; J620 / J624 split n/d | TCEQ permit 182467 once issued; unit ratings from the equipment list |
| Site detail, Jupiter | 4 buildings (Baxtel); one source describes a single 3M sq ft facility | Doña Ana County permits or site plan |
