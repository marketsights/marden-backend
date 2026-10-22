# Moves v0.1: which instruments' prices and yields moved, and why

Model `moves` (`services/marden/models/moves*.py`), version 0.1. Read models `moves` (the Moves page, the Home
"Biggest moves this week" card) and `instrument_moves` (the changes under the price on Instrument detail).

## Windows

Each window ends on the as-of day and starts on: the weekday before (1D), 7 days before (1W), one month before
(1M), the last day of the previous quarter (QTD), Dec 31 of the previous year (YTD).

## Which prices are compared

- **Traded** when the instrument traded inside the window: the TRACE print of the last trade day, against the
  last trade day on or before the window's start. The prices keep their stored tag (TRACE prints are filed, F).
- Otherwise the **Marden** daily price (M) at the end against the last one on or before the start.
- A window whose history begins inside it compares from the first price on record and is marked partial, with its
  real start date. A start price several days older than the window's start is called out. When the latest price
  is on or before the window's start there is no move, and the window says why. Nothing is filled in.

## Yields

Worked by the yield model (`yield-v0.1.md`) at both prices: yield to worst where the debt can be redeemed early
(a call, an extension, a loan's prepayment at par), else yield to maturity; floating-rate debt also its discount
margin. Changes in basis points.

## What drove a move

A Marden-priced move is split into:

- **events**: each priced change in the site's event inputs dated inside the window, at its counterfactual effect
  (`event-impact-v0.1.md`);
- **rates** (fixed coupon only): the end price less the price at the end yield without the window's move in the
  Treasury par yield at the instrument's maturity (spread unchanged). A floating coupon resets with SOFR, so rates
  are not split out;
- **spreads**: the rest (the credit inputs, such as the tenant's or issuer's bond-implied CDS, the recovery, and
  the residual from observations).

The main driver is the largest of these. A traded move is driven by the trades; the priced events inside the
window are listed with it.

## Limits (v0.1)

- Marden daily prices exist from Jul 1, 2026 (backfilled each business day); TRACE from Jan 2, 2026. Earlier
  windows are partial and say so.
- Event effects are priced on their own day and are not additive; the split is an attribution, not an identity.
