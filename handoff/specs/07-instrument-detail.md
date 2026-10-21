# 07 · Instrument detail (`/instruments/:id`)

**Job:** one loan, bond or facility: what it is worth today, how far funds' marks are from that, who holds it, what it pays from, and what happens under stress.
**Reference:** screens/07-instrument-detail.html (CoreWeave 9.00% 2031) · **Endpoints:** `GET /api/instruments/:id`, `/price-history?range=3m|6m|all`, `/holders`, `/stress`

## Layout

1. Header: breadcrumb, name, alert pill when the mark gap is ≥ 5 pts ("Funds 20 pts above market"), facts line (seniority, format, size, issue date, ranking). Actions Excel, Watch.
2. KPI strip (5): Marden price + 1w change (M); Yield + level at a prior date (R); Funds' last marks range + filing and date (F); Mark gap + when next marks become public (M; highlighted red background); Recovery if default, cents per dollar (M).
3. Main column:
   - **Price chart:** Marden daily price; range bar of filed fund marks at the filing date; dashed line carrying the last marks forward to today; shaded gap labelled with its size. Range control 3M / 6M / Since issue.
   - **Holders table:** holder, filing type, par $mm, last mark, gap pts, NAV hit $mm (par × gap), share of fund NAV (bar). Sorted by NAV hit. Header line: "{n} funds report it · ${par} par". Excel.
   - **Stress results:** scenario, price after, change, holder NAV hit. "Run your own →" opens Stress.
4. Right rail: Terms (issuer, coupon, maturity, size, ranking, guarantees, covenants; link to indenture and offering memo) · Who pays the coupon (customer concentration bar with source; chains it sits on) · Market check (issuer CDS, implied default odds, tenant CDS, BB HY index) · Alerts on this note (checkboxes: price moves 2+ pts, a fund files a new mark, a chain link changes status).

## Rules

- Mark gap uses the latest filed marks per holder (N-PORT, BDC 10-Q), not interpolated.
- NAV hit = par × (Marden price − last mark) / 100; share of NAV = NAV hit ÷ fund net assets from the same filing.
- Holder names come only from filings, each linked to its filing.

## Acceptance

- The gap number in the KPI strip, the chart label and the table total use the same calculation.
- Alert checkboxes save immediately and add the instrument to the watchlist if it is not there.
