# 03 · Completion monitor (`/completion`)

**Job:** rank every financed site by schedule cushion: months between projected completion and the lease outside date (after which the tenant can walk or stop rent). Sites most likely to miss sit on top.
**Reference:** screens/03-completion.html · **Endpoint:** `GET /api/completion?endUser=&tenant=&holder=&state=&driver=&cushion=&watchlist=`

## Layout

1. PageHeader: "Completion monitor"; actions Excel, Saved views.
2. Four KPI cards: Past outside date (count, debt, names; red left accent); Under 3 months; Slipped this week (count, months, record with tag); Debt by cushion (stacked bar: <0 red, 0–3 amber, 6–12 grey, 12+ slate; "{x}% sits on sites with under 3 months" M).
3. Filter chips: End user, Tenant, Lender or holder, State, Driver, Cushion band; checkbox "Watchlist only". Right: "Tightest first · hover any bar or square for detail".
4. Table (one row per site), sorted by cushion ascending; sites with no lease date last.

## Columns

| Column | Content | Source |
| --- | --- | --- |
| Site | Name (link to Site detail) + state | Site |
| Chain | ChainGlyph with tooltips | Status rules |
| Timeline | TimelineBar: P10–P90 band, P50 tick, outside-date line, year ticks | Cushion model (M); outside date from lease (F/R) |
| Cushion | CushionChip, months | outside date − P50 (M) |
| Δ wk | Change in cushion vs 7 days ago | Snapshots |
| Driver | Power, Build, Delivery, Permits, Funding | Event that moved the projection most |
| Last evidence | **Date** + one line + tag (wraps to 2 lines max) | Latest material event |
| Debt $B | Sum of instruments that need this site finished | Instruments (R/F) |
| Lenders | Lead lenders, shortened ("SMBC +19") | Instruments |

## Interactions

- Row click → Site detail, Schedule tab.
- Chain squares and timeline show tooltips (see components).
- Excel exports the filtered table plus a sources sheet.

## Acceptance

- Cushion = outside date − P50, whole months, recomputed weekly and on any material event.
- A site without a lease outside date shows "No lease date" in the timeline and "—" for cushion, sorted last.
- Non-US sites and sites with no build are excluded from this screen (they appear on Sites).

## Buildings column (spec 16)

A "Bldgs" column after Site shows built / total buildings from the linked campus (n/d when no building records). Hover shows the source.
