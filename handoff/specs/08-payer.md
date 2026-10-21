# 08 · Payer (`/payers/:slug`)

**Job:** what one tenant or end user is on the hook for, when rent starts on each site, and what happens to the lenders if it slips. Payers: OpenAI, Oracle, Microsoft, Anthropic, CoreWeave (tabs).
**Reference:** screens/08-payer.html (Oracle) · **Endpoint:** `GET /api/payers/:slug`

## Layout

1. Header: breadcrumb Payers / role, payer name, one-line description; actions Excel, Watch. Payer tabs below.
2. KPI strip (5): site debt that depends on its rent; capacity leased (GW); rating (agency, date); 5y CDS; its own notes outstanding (+ number of funds holding them).
3. Main: **GW by projected rent-start year** (stacked bars, a segment per site, colored by cushion; title states the finding). **Every site it leases** table: site, GW, rent start (P50), outside date, cushion, debt, status.
4. Rail: Who pays this payer (next link up the chain, its status, its funding gap) · Credit facts (collateral posted, notices, next results) · "If {payer} is cut to …" stress summary linking to Stress.

## Acceptance

- Totals match the sum of the sites table.
- For an end user (e.g. OpenAI), the table lists every tenant and site it pays for; the chart groups by tenant.
