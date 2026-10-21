# Stress and contagion v0.1 (model v0)

Written 2026-10-21. Code: `services/marden/models/stress.py` (results), `stress_run.py` (runs, saved scenarios, custom jobs, CLI), `stress_scenarios.py` (shocks), `stress_price.py` (in-process re-pricing), `stress_inputs.py`, `contagion.py`. SQL: `db/migrations/20261021210000_stress_run.sql`. Model `stress`, version `0.1`. Specs 09, 18, 22, 24.

## How a shock is priced

`stress_price.Book.build(day)` loads what the price engine loads for that day (`pricing-v0.1.md`):

- Treasury curves, instruments and comps, observations;
- the issuers' bond-implied intensity curves, the tier fit, chain signals and GPU half-lives.

It resolves each instrument's chain once. `reprice(id, shock)` prices with the engine's own functions, using the same calibration (issue price or first observation) and the same Kalman-filtered residual.

- **now** equals the stored `price` output exactly (checked in the tests).
- **after** applies the shock on the as-of day only, so the history the liquidity premium was calibrated on does not move:
  - an intensity multiplier per entity (tenant, obligor, guarantor or issuer leg);
  - a later completion per site (the cushion P50 the chain reads);
  - an optional spread add-on.
- Instruments the engine prices off the tier curve keep the spread that reproduces their tier price under today's intensities.

The book is cached per day in the worker. A nightly or triggered run replaces it, so custom requests take seconds after the first.

## Scenarios (default parameters are the saved ones)

- **end_user_funding_miss** {endUser: openai, cutPct: 30}.
  - Each payee of the end user's payment edges loses cutPct% of the remaining contract: the edge's schedule discounted at the 10-year Treasury par yield, or the stated total when it has no schedule.
  - The loss comes off the payee's equity value, one dollar of value per dollar of revenue (the leases and GPU costs behind it stay; an upper bound).
  - CreditGrades (the engine's structural model, same constants) turns the lower share price into the intensity multiplier.
  - The end-user link of every site it uses turns red (E2).
  - On 2026-10-21: Oracle loses $77.7B PV of the reported $300B contract. Its share price goes 131.14 → 105.45 and its multiplier is 1.186. CoreWeave's multiplier is n/d (no XBRL debt per share near its share count).
- **tenant_downgrade** {tenant: oracle, rating: BB+}.
  - Multiplier = exp(β × notches), where β is the slope of log bond-implied intensity on rating notch across the issuers the engine prices that day.
  - At least two issuers with different ratings are needed, else n/d.
  - On 2026-10-21: two issuers (Oracle BBB-, λ 0.0403; CoreWeave B+, λ 0.176), β = 0.369 per notch. BBB- → BB+ gives ×1.446.
  - The tenant link turns amber (T1).
- **power_delay** {months: 6}.
  - Every V1 site still waiting on a power milestone has those milestones moved by `months`. The cushion simulation is re-run in-process with the stored run's seed and inputs, so the base equals the stored cushion.
  - The P50 rent start moves by the result, and the engine reads the later completion.
  - M2 when the cushion crosses below zero; M1 when P50 slips 3+ months.
  - Sites whose power milestones carry no planned date cannot be moved and are listed in `missing`.
- **Severe** adds the peak-2022 OAS of the tenant's rating index over today's (`ice_bofa_us_<bucket>_oas`). While that series is not collected (pending ICE permission), severe prices and losses are n/d and say so.

## StressResult

- `kpis`:
  - exposedB: the face of the sized debt on the scenario's sites.
  - valueLostB = Σ size × (now − after) / 100; valueLostPct is that as a share of exposedB.
  - tenantNotesLossB: the same over the tenant's own notes, shown as one aggregated last row. Omitted when there are none.
  - loansBelow95: names that fall from 95 or above to below 95.
  - fundsHit: funds (mark-gap holder lines) holding a row that loses value.
- `flow`: one entry per site, with its debt, size-weighted price change and the severity of the rule it trips.
- `rows`: worst first. rentCover is omitted because no filing discloses rent.

Other outputs:

- ('instrument', id) `{rows: [{scenario, priceAfter, change, holderNavHitMm}]}` for InstrumentDetail.stress. holderNavHitMm = Σ holder par × change / 100.
- ('entity', tenant or end user) `{stressSummary}` for the payer page.
- The nightly run fills `stress_saved.result` and `ran_at`.

Results on 2026-10-21 (base):

| Scenario | Exposed | Value lost | Notes loss |
| --- | --- | --- | --- |
| Tenant downgrade, Oracle → BB+ | $74.45B | $0.966B (1.30%) | $5.538B |
| OpenAI cuts 30% | $76.67B | $0.409B (0.53%) | $2.51B |
| Power delay 6 months | $60.25B | $0.376B (0.62%) | none |

In the power delay, Jupiter's P50 moves +6 months, Frontier's +5 and Beacon Point's +2.

## Contagion (ContagionResult: "company X pays only N%")

The walk covers three hops over payment edges (leases, compute and chip purchases, power purchases, loans, revenue shares).

1. Each payee loses (1 − N%) of the edge's next annual payment.
2. Each payee's cover is its LTM operating cash flow against its own next-year payments on the map: coverageAfter = (cash flow − loss) ÷ payments.
3. A payee whose loss exceeds its spare cash flow pays 1 − (loss − spare) ÷ payments on its own edges, which is the next hop.
4. A payee without filed cash flow cannot be judged and passes nothing on.

Instruments are priced two ways, and the lower price wins:

- **Cash cut**: debt repaid from a cut stream, and the notes of a company paying less, are worth f × price, floored at the recovery model's low.
- **Weaker credit**: each hit company's lost contract PV comes off its equity value. CreditGrades gives a multiplier, and the engine re-prices its notes and its tenant site debt.

On 2026-10-21, OpenAI paying 50% costs Oracle $30.0B in 2027. Oracle covers it from cash flow (coverage stays above 1 on the payments mapped), and its notes and site debt re-price at the weaker intensity. Saved: OpenAI, Anthropic, Oracle, Microsoft, CoreWeave and Nvidia at 50%.

## stress_run(jsonb): the API contract

- A saved request is answered from the stored output at once (`status: 'done'`).
- A custom request queues one `models.stress_custom` job per distinct request (subject `custom:<md5>`, queueing lock `stress:<subject>`) and answers `{status: 'queued' | 'running', jobId, subject}`. The worker stores the result for the day, and the same call then returns it.
- The API polls for up to 20 s (`MARDEN_STRESS_WAIT_MS`), then answers 202 with the pending body.

## Runs

- Nightly 06:17 UTC.
- Within a minute of a new price or cushion output, or a change to an edge's amounts.
- A past `--as-of` is a backtest: the book, ratings and facts are as public that day. On 2026-07-01 no instrument size and no rating was public yet, so exposure is 0 and the downgrade multiplier is n/d.
