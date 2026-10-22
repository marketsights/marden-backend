# Event impact v0.1: did this event change the value of the debt?

Model `event_impact` (`services/marden/models/event_impact*.py`), version 0.1. Read models `site_debt_effects`
(the "Effect on debt" line on a site's Material events and Evidence tabs) and `instrument_moves` ("What moved
it" on Instrument detail).

## Question

For an event on a site (a permit denial, a force-majeure notice, a schedule slip, a dealer quote), how much did
it change the value of each instrument financing the site, on the day the price model first saw it?

## Method: a counterfactual Marden price

The Marden price engine (`pricing-v0.1.md`) reads three kinds of event input for a site's debt:

| Input | Where the engine reads it | Records |
| --- | --- | --- |
| Power and permit signals | `price_signals`: permit denials (x1.75 completion-failure rate each, cap 6x), power delays of 3+ months and force-majeure notices citing power (+0.25 force-majeure odds each) | the site's events and its power, permit, risk and schedule facts, dated by the day each became public |
| Projected completion | `price_chain`: the cushion model's P50, from its latest run on or before the day | the cushion model's runs (`cushion-v0.1.md`) |
| Observations of the debt | `price_marks`: dealer quotes, trades and fund marks move the residual spread | `price_obs` |

Each dated change in those inputs since Jan 1 is priced twice on the day it takes effect, with the engine's own
functions and the same day's inputs (`event_impact_price.DayModel` builds them exactly as `price.run_day`):

- **with** the change: the engine's price that day (it equals the stored Marden price of that day when the
  engine ran that day with today's code and inputs; when not, the output keeps the stored value and the screen
  says it was recomputed);
- **without** it: the same, with that change's records left out (signals), the previous cushion projection
  (completion), or the quote left out (observations).

Effect = with minus without, in points. The yields at both prices are worked by the yield model
(`yield-v0.1.md`): yield to worst where the debt can be redeemed early (a call, an extension, a loan's
prepayment at par), else yield to maturity; floating-rate debt also its discount margin.

A signal day is also priced with each kind of signal left out on its own, so a single record (a force-majeure
notice among the day's reports of denials) has its own effect. A day whose records only restate what the engine
already counted is not a change: the engine counts each development once.

Alongside: the observed market move, the instrument's last trade before the day and first trade after it (within
14 days; TRACE prints of the event's own day are left out, since the time the news broke is not known), for the
site's debt and the V1 bonds of its tenant and end user. The engine prices those bonds from their own trades, so
the site's records are not an input to them and only the market move is shown.

## Records to effects

A record on the site's tabs stands for a priced change when it is one of the change's own records (same link),
when it is the cushion move the change is (same day), when it reads as the same kind of signal by the status
model's text rules within 7 days after or 3 days before the day the engine counted it, or (a developer slip of 3+
months) when the next cushion run within 45 days moved the completion. Otherwise it says why it changed nothing:

- no priced debt is linked to the site;
- procedural filing;
- status only (the end user's funding; a tenant record, since the engine reads the tenant through its bond prices);
- already priced in (the engine counted the same kind of signal earlier, from the named records);
- not priced on this day (the engine first counted it later, from a later report);
- not an input (the engine does not read this kind of record);
- read, but the price moved less than 0.05 points.

## Limits (v0.1)

- Only the inputs above are isolated. Recovery (the site collateral waterfall) and the tenant's bond-implied CDS
  are not split by event; a move they cause shows on the Moves page as spreads.
- The cushion model runs on some days only; a completion change is dated by the run that moved it, not by the
  record that caused it.
- Effects are not additive across changes: each is priced against the inputs of its own day.
- Events of entities and instruments that are not on a site's tabs are not matched to changes.
