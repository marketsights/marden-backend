# 05 · Sites (`/sites`)

**Job:** directory of every site Marden covers, including sites with no lease date or no build, with chain status.
**Reference:** screens/05-sites.html · **Endpoint:** `GET /api/sites?status=&endUser=&state=`

## Layout

- PageHeader "Sites"; action Excel.
- Status filter chips with counts (All, Broken, Strained, Holding, No build) + End user and State dropdown chips. Right: "Sorted by cushion · hover a square for the reason".
- Table.

## Columns

Site (name link + county, state) · End user → tenant · Developer · GW (R) · Chain (ChainGlyph) · Status (pill = worst link) · Cushion (chip, M) · Rent start (P50, M) · Debt $B (R/F) · Lead lenders.

## Acceptance

- Same status and cushion values as Completion and Site detail (one source).
- Sorting by any column; default cushion ascending, no-lease and no-build last.
- V1 is US only; non-US sites are hidden (footer note says so).

## Buildings column (spec 16)

A "Bldgs" column after GW shows built / total buildings from the linked campus (n/d when no building records).
