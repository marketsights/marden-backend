# Delivery record v0.1

Who keeps having construction delays. Read model `delivery` (`services/marden/readmodels/views/delivery.py`),
GET /api/delivery, screen MONITOR › Delivery record.

## Who

Each company behind a tracked site (the Completion list): the developer or sponsor and the tenant (chain
parties) and the issuer of the site's debt (the instrument's issuer, its parent, or the companies its name
names: "Abilene JV (Crusoe / Blue Owl / Primary Digital)"). One company is one row however many entities and
labels it appears under (`delivery_parties.COMPANIES`; Cipher Digital Inc. and Cipher Compute LLC are both
Cipher Digital). A label that names no listed company is its own row. An issuer SPV that names none is left out.

## What is judged

Every dated item on the company's sites, against the first target a source stated for it:

| Item | First target | Compared with |
|---|---|---|
| Milestone (Schedule tab) | `originalTarget` | `actual` when done, else the current `target` |
| Building | the state registration's estimated completion | `construction_end`, or `construction_complete` seen by the target date |
| In-service date | the first stated date in the status rule D1 | the date stated now (D1) |

Months compare window ends ("Q4 2026" is Dec 2026). Slip = later month − first month. An item still open past
its current target is late by at least the months since its first target ("at least"). Done items without a
first target or a completion date are counted as not judged.

- On time / late: done items. Open: not done (of which "behind": slip above zero).
- Average and worst slip: over the judged items (early items count as negative slips).
- Pushes in the last 90 days: a milestone marked slipped on the Schedule tab, a filed or reported schedule change
  that says a date moved later, and a later Marden projected completion (event-impact completion changes, tag M);
  at most one a day per site.
- Behind the lease deadline: sites with an outside date whose cushion (P50 rent start against it) is below zero,
  over the sites with an outside date. An assumed outside date is flagged.
- Debt: the sum of the sites' debt (Completion), tagged with the weakest tag of the parts.

## Ranking and peers

Worst first: average slip, then pushes in 90 days, then worst slip, then sites tracked. Companies with nothing to
judge come last. "vs peers": the row's average slip minus the median of the developers' averages.

## Limits

- Only the dates a source states: a site without a dated plan adds nothing.
- The Marden projected completion moves with model inputs as well as with news; those pushes are tagged M.
- No calibration: the slip is a count of months, not a probability.
