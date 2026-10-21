# 04 · Site detail (`/sites/:slug`)

**Job:** everything about one site: who pays whom, where the chain breaks, whether it finishes before the tenant can walk, the evidence behind that, and the debt that depends on it.
**Reference:** screens/04-site-detail.html (Schedule) and 04b–04h (other tabs), all for Jupiter · **Endpoints:** `GET /api/sites/:slug`, `GET /api/sites/:slug/schedule`, `/evidence`, `/power`, `/opposition`, `/events`, `/debt`, `/lease`, `/buildings`

## Header (all tabs)

- Breadcrumb (Completion / county, state), site name, status pills (chain status; cushion), facts line (GW, debt with tag, tracking/SPV names). Actions: Excel, Share, Watch/Watching (primary).
- **Payment chain strip:** 4 cards in a row joined by arrows: End user → Tenant → Developer·SPV → Lenders. Each card: role label, name, one fact with tag, status pill, rule code. Red cards get a red border. Click a card → that link's detail drawer (terms, filings, why it has this status).
- Tabs: Schedule · Evidence (count) · Power · Buildings (count) · Opposition (count) · Material events (count) · Debt and holders · Lease terms. The Buildings tab and building-linked milestones are in spec 16 (screens/04h-site-buildings.html).

## Schedule tab (designed)

Main column:
1. **Cushion card:** hero number (−2 mo, M) with one sentence; projected completion (P50 + P10/P90); lease outside date (tag; when only the year is known, say which date the cushion assumes, e.g. Dec 31, 2028); odds of missing it (M, labelled model v0, with value on Jan 1); cost of each month late (M: construction interest; who pays).
2. **Milestones (MilestoneGantt):** standard milestones for every site in this order: land and zoning; air permit; utility or large-load agreement; gas supply / grid interconnection in service; on-site generation in service; building shells; first hall energized; rent start. Columns: milestone, timeline ('25–'29, today dashed, outside year shaded), latest evidence + tag, status pill (Done, Denied, Withdrawn, Slipped, Not started, Early works, Projected, At risk). Legend above.
3. **Evidence behind the schedule:** 3 latest monthly Sentinel-2 thumbnails with captions (before/after slider opens on click); table of the 4 latest documents (date, what, source, tag, Open).

Right rail:
1. **Cushion history** step chart (weekly, Jan 1 → today), dots + date labels at each change, dashed zero line, sentence linking the first flag to the price move.
2. **Debt that needs this site finished:** loan size, dealer quote, Marden price, recovery if tenant walks; holders sentence; "Open the loan →".
3. **Material events:** latest 5 (date, dot by severity, what, rule hit); "All 8".

## Other tabs (all designed)

| Tab | Reference | Content |
| --- | --- | --- |
| Evidence | screens/04b-site-evidence.html | Type filter chips with counts (Permits, Dockets, Press and ratings, County, Imagery). Satellite before/after compare (two month pickers, draggable divider, change score by month, M). Records table: date, type chip, what it says, source, tag + Open. Excel. |
| Power | screens/04c-site-power.html | KPI strip: power needed, permitted on-site generation (count of filed permits, M), gas supply in-service date, grid connection. Chart: planned in-service date by source (pipeline, turbines, fuel cells) with each change marked and today line. Watched dockets list. In-service date history table (recorded, source, what changed, record). |
| Opposition | screens/04d-site-opposition.html | KPI strip: open court cases, latest ruling, next hearing, impact if ordinances are voided (M). Table of votes, moratoria, lawsuits and petitions: date, type chip, what, court/body, status pill, source. Court-docket records replace press sources as they arrive. |
| Material events | screens/04e-site-events.html | Lane chart: status of each chain link (end user, tenant, developer, lenders) and the chain over the year, colored by status, hover for the reason. Full event log: date, severity dot, event + tag, link, rule hit, what it changed (status change, cushion change). |
| Debt and holders | screens/04f-site-debt.html | Capital stack bar at the SPV (loan, equity). Instruments that depend on the site: size, dealer quote, Marden price, recovery. Recovery inputs card (shell value, equipment, re-letting time, claims ahead of the loan). Lender table: lender, role, share, source; line stating whether any fund reports the loan. |
| Lease terms | screens/04g-site-lease.html | Terms table (parties, rent start, outside date highlighted, force majeure, rent amount, term, termination rights, parent guarantee) with source tags; unknown terms shown in brackets. "What the terms mean for lenders" card: finished before the outside date, finished after it, force majeure accepted; today's position. |

The tab bar is the same on all eight pages; counts on Evidence, Opposition and Material events come from `/api/sites/:slug` `tabCounts`.

## Acceptance

- The four chain cards always reflect the status rules; hovering the rule code shows the rule text.
- Every milestone date shows its source document on hover/click.
- "Odds of missing" and "cost of each month late" show M tags and link to the method.
