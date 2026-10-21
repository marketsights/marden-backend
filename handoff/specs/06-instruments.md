# 06 · Instruments and credit curve (`/instruments`)

**Job:** every AI-linked loan, bond and lease in V1 with today's Marden price, plus the curve that shows where each credit tier trades.
**Reference:** screens/06-instruments.html · **Endpoints:** `GET /api/instruments?type=`, `GET /api/curve`

## Layout

1. Curve card. Title states the finding ("Unsecured neocloud debt yields 3x Oracle at the same maturity"). Chart: yield (%) vs years to maturity on a log axis (2y–40y). Tiers: Oracle IG, backstopped AI debt, CoreWeave secured, CoreWeave unsecured; Anthropic junior as a single point with a June → now arrow; BB HY index as a dashed line. Dots = individual instruments; solid line = fit over observed maturities; dashed = extrapolated. Right-gutter labels with leader lines and the tier's range. Note: "Few points per tier, so read as shape, not a quote."
2. Type filter chips: All, Construction loans, Chip leases, GPU facilities, Bonds, Tenant credit.
3. Table.

## Columns

Instrument (link) · Type · Size (R/F) · Matures · Marden price (M) · Yield (R where traded) · 1w change · Mark gap (Marden price − average filed fund mark, pts) · Chain (status pill).

## Curve fitting

Per tier: least-squares fit of yield on a Nelson-Siegel shape (fixed tau ≈ 3y) using each instrument's latest price. Draw the fit only between that tier's shortest and longest maturity (solid), extend ±25% dashed. Re-fit daily.

## Acceptance

- Clicking a dot or a row opens Instrument detail.
- Instruments without a traded yield show "—" and still get a Marden price.
