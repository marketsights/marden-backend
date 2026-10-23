# Mispriced v0.1

What is about to drop or bounce. Read model `mispriced` (`services/marden/readmodels/views/mispriced.py`),
GET /api/mispriced, screen MONITOR › Mispriced and the Home card "Mispriced now".

## Per instrument (V1, with a Marden price)

1. Market evidence: the newest TRACE trade (F), exchange quote (Börse Frankfurt, BondTerminal; R) or reported
   dealer quote (R) by the build day. Without any, the funds' filed marks: the par-weighted last mark of the
   holders in the `mark_gap` output, dated by the newest filing period (F).
2. Gap = Marden price − market price, points. Yield gap = Marden yield − market yield, bp, from the yield model
   at both prices on the build day: YTW where the debt can be redeemed early and both prices are at or under par,
   else YTM.
3. Events since: everything dated after the market evidence: priced changes on the instrument's site with the
   event-impact model's effect on this instrument (schedule slips, power and permit signals, dealer quotes; M);
   the site's, instrument's and issuer's records (filings, rating actions, permits, opposition, notices, news the
   event pipeline marked red, amber or green); for a tenant's own debt, the records of the sites it leases
   ("tenant news", no model effect: that debt is priced from the tenant's own credit).

## Groups

- Market above our value (downside not priced in): gap ≤ −3 points, or a negative event since the market
  evidence (model effect ≤ −0.25 points, or a red record) with the Marden price below the market.
- Market below our value (possible bounce): gap ≥ +3 points, or a positive event (effect ≥ +0.25, or a green
  record) with the Marden price above the market.
- Stale marks: the funds' filed marks are 3+ points above a trade or quote dated after them.

Rows are sorted by |gap| × size ($B).

## How far to trust a row

- "Model only: no trade or quote since …": events came after the last market price.
- "N trading days since …; the market agrees / still differs": trades after the latest event of the last 30 days.
- "Fund marks only": no trade or quote on file; the market side is the funds' marks as of their filing period.
- Assumptions: inputs of the Marden price that are model assumptions, not records (an assumed lease deadline, an
  estimated share of the site built, a default recovery, terms from a comparable issue). When the gap is 3+ points
  and the priced events since explain less than half of it, the row says it rests on those assumptions.

## Limits

The Marden price is uncalibrated against outcomes; a gap is a difference between two prices, not a forecast.
