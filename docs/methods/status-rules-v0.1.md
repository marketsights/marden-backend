# Status rules v0.2 (model `status`)

Written 2026-10-21 (v0.1); v0.2 the same day: rule L2 reads the Marden price rate-neutral ("Why L2 is rate-neutral" below). Code: `services/marden/models/status.py` (run, CLI, tasks, triggers), `status_inputs.py` (what it reads), `status_rules.py` (the rules), `status_credit.py` (L2's rate-neutral price test), `status_text.py` (text and rating rules), `status_dates.py` (in-service dates), `materiality.py` (the alert engine's test). Spec: `handoff/specs/00-global.md` "Status rules" and "Materiality and alerts".

## Output

One `model_output` row per financed site and as-of day: model `status`, version `0.2` (method `status v0.2`), subject `('site', slug)`.

```
{ "chain": ChainLink[4],      // end_user, tenant, developer, lenders, in that order (types.ts ChainLink)
  "status": ChainStatus,      // 'Broken' | 'Strained' | 'Holding' | 'No build'
  "missing": string[] }       // inputs that were not on file, e.g. "lenders: Marden price (price model output)"
```

`chain` and `status` drop straight into `SiteSummary.chain` / `.status`, `CompletionRow.chain`, `InstrumentRow.chainStatus`, `FinancedLink.chainStatus` and the `status` StreamEvent (each link passes `marden.core.events` validation). Each tripped link carries `ruleCode`, a `reason` (rule, the clause that tripped it, source and public date) and `fact`: the statement as a `Sourced<string>` (tag F or R, its URL, the day it became public). The Marden price, when it trips L1/L2, is the `fact` with tag M and the price model's `methodVersion` (a spread-loss L2 fact carries `status v0.2 on price v0.1`: the loss is this model's arithmetic on the price model's spread). A green tenant link shows the rating statement it read; a green lenders link names the lowest price and, for a fixed coupon, its spread-only loss since the reference day.

## The rules (fixed)

| Link | Amber | Red |
| --- | --- | --- |
| End user | E1 its plan needs new funding | E2 contract cut, or the end user exits |
| Tenant | T1 rated BBB- or lower, or must post contested collateral | T2 BBB- or lower and power denied or withdrawn, or force majeure filed |
| Developer | D1 in-service date slips 3+ months | D2 construction halted |
| Lenders | L1 Marden price 95–98, or syndication stalled | L2 Marden price below 95 on credit alone (fixed rate: the spread move since the reference day is worth 5+ points; floating rate: the price below 95), or default notice |

Chain status = worst link: any red → Broken, any amber → Strained, else Holding. `No build` when a filed or reported statement says construction has not started. A link with no named party (`chain_party`) is `none` with party `—`; the lenders link is judged whenever the site has debt, with party `n/d` if no lender is named. When no rule trips, the link is green and `missing` lists what was not on file: green means "no rule met on the statements Marden holds", not "checked and clear".

How each rule reads the record:

- **E1**: a funding-need statement (cash burn, negative free cash flow, funding gap, needs to raise) in the end user's `funding_fact` rows, or a site statement naming the end user.
- **E2**: a contract cut, termination, cancellation or exit tied to a contract, lease, order, commitment or the project, naming the end user.
- **T1 rating**: the tenant's latest rating statement, pooled across every site where the same entity is tenant: the sites' `tenant_rating` texts and dated rating headlines (`risk_evidence`, `schedule_change`) that name the tenant. The lowest agency grade stated for the tenant itself counts (S&P / Fitch / DBRS / KBRA letters and Moody's on one notch scale; 'high yield' without a grade counts as BB+ or lower). Clauses about a bond, a backstop or a guarantor, text in brackets and grades a contract or tariff *requires* are not the tenant's rating. 'Unrated' is not 'BBB- or lower'.
- **T2 force majeure**: a notice, invocation or filing of force majeure (not a contract clause such as "except force majeure") in a site statement naming the tenant, or in the site's own lease terms.
- **T2 power denied or withdrawn**: a denial / rejection / withdrawal word within eight words of a power object (right-of-way, pipeline, gas, interconnection, permit, PPA, transmission, generation …) in one clause of a power statement, a schedule statement or an event, and not a procedural ruling (a denied motion, rehearing or complaint; a completeness or deficiency letter; a confidentiality ruling) or a voluntary switch of technology. Queue withdrawals on approved campuses / plants count.
- **D1**: a site's ready-for-service target moved 3+ months: per project, the latest dated statement (`rfs_statement`) against the first one; and the dossier's `expected_energization` against `original_energization`. Dates are read by `status_dates`: a period counts as its last month (00-global: a year alone is Dec 31), a coarser later statement that still contains the earlier date is no slip, statement dates and labels ('Jun-11-2026 call (DCD): …') are skipped, outside estimates (Epoch, Aterio, satellite tracking) are cut off, and 'no change reported' carries no target. A delivery statement that itself says "delayed N months" counts too; a fuel-supply or permit slip does not (that is the cushion model's input).
- **D2**: construction halted, paused, stopped or suspended (not "work continuing despite a pause"), a stop-work order, or an expected completion that reads 'cancelled'.
- **L2 price** (rate-neutral, `status_credit`): each of the site's instruments in the price model's latest `model_output` on or before the day. A floating-rate loan (`basis.terms.couponType` `floating`, or no terms) trips L2 when its Marden price is below 95. A fixed-rate instrument trips L2 when its **spread-only loss** is 5 points or more: the price output's cash flows (on the output's own day) priced at the reference day's Treasury par yield at the remaining maturity plus the output's Marden spread (`spreadBp`, yield minus the Treasury par yield at the same maturity) give the *credit price*; the same with the reference-day spread give the *reference price*; loss = reference price − credit price, i.e. (spread now − spread at reference) × spread duration, revalued in full. 5 points is the move that would take a par-issued instrument below 95. When several instruments trip, the link cites the largest loss (a floating loan's loss counts as 100 − price).
- **Reference day**: the instrument's issue date (its issue comp: `basis.terms.issueDate` and `issuePrice`), where the reference spread is the issue price's spread on that day's Treasury curve — the price model solves its liquidity premium so it reprices the issue price that day, so this is the model's own spread at issue; else the first day Marden priced it (its earliest stored `price` output with a spread, on or before the run day), with that output's `spreadBp`. A fixed-rate instrument with neither gets no L2 price test and `missing` says so (`lenders: … (rate-neutral L2 test)`).
- **L1 price** (unchanged from v0.1): the lowest raw Marden price among the site's instruments is 95–98, when no price L2 fired. Until the price model writes outputs, only the text rules below apply and `missing` says so.
- **L1 syndication**: syndication stalled, struggling, failed or pulled, or arrangers holding more than planned, in the site's debt notes, tranche notes or the instruments' terms. Slow syndication alone ("has taken more than six months") is not stalled.
- **L2 default notice**: a notice of default, a declared event of default, acceleration, a missed payment, or an instrument `status` of default.

### Why L2 is rate-neutral (v0.2)

A chain status is a credit signal. Under v0.1, L2 read the raw Marden price, and long fixed-rate bonds that fell only because Treasury yields rose (5-year par yield 3.71% on 2025-11-13, 4.94% on 2026-10-20; 20-year 4.69% to 5.47%) broke their chains: 11 of 29 sites were Broken on 2026-10-21, five of them (Beacon Point, River Bend, Hyperion, Core Scientific / CoreWeave, Polaris Forge 1 building 4) on rates, not credit. Holding the risk-free curve at the reference day takes the rate move out and leaves the spread move, which is what the price model attributes to credit (tenant and chain intensities, the liquidity premium held from issue, and the residual the Kalman filter reads off the instrument's own trades and marks). A floating-rate loan's coupon resets with SOFR, so its price has no rate duration and is already a credit price.

Why the issue date: it is the one day the price is known without the model (the issue price, from the issue comp) and the price model calibrates to it, so the reference spread is the model's own and needs no stored history. The first Marden price is the fallback for instruments without an issue comp (EX-XAI_VCI-388 today, first priced 2026-07-01); it depends on which days the price model has been run, and a backtest before that day has no reference.

Every text rule matches inside one clause and skips negated ("no force majeure notice", "cannot terminate", "not an event of default") and modal or conditional wording ("may terminate", "right to", "if", "except"). A news item filed under several sites counts for a site only if it names that site.

## Inputs and point in time

Inputs are published facts and events, read through `marden.models.pit`:

- Site facts: `risk_evidence`, `schedule_change`, `rfs_statement`, `tenant_rating`, lease terms (`lease_termination`, `lease_tenant`, `leases`, `structure_term`, `credit_support`), debt notes (`debt_facility`, `tranches`, `debt_items`), power text (`power_contract`, `power_detail`, `power_status`, `power_supply`, `onsite_generation`), `expected_energization`, `original_energization`. Chain parties (`chain_party`) and their entities' `funding_fact`. The site's instruments' `research_terms` and `status`. Events on the site, its chain-party entities, its instruments, and (approved links only) its campuses and plants; `catalyst` events (scheduled steps) are not inputs.
- Only tags F and R can trip a rule. Tag M rows (the archive's legacy judgments, other models' estimates) are never inputs, except the Marden price.
- A run for today reads every published row. A run for a past day is a backtest: a fact counts once public (`valid_from` plus the source's lag, e.g. N-PORT +60 days); `learned_at` is ignored because the archive and the first collector runs were loaded on 2026-10-21. One extra rule here: a dossier note in the seed archive (debt notes, rating text, lease terms, energization text) is public only from the archive's own date (`document.doc_date`, 2026-10-20), because the note was written then even when its record is older; dated items (risk evidence, schedule changes, RFS statements, events) are public on their own date.
- Licensing: with `MARDEN_SHOW_UNLICENSED_PRICES=false` rows from unlicensed sources are dropped from the inputs (by `pit`).

## When it runs

- Daily at 09:53 UTC (`models.status`, queue `models`): after the overnight collectors, before the 10:05 UTC screen rebuild.
- Within a minute of a published input (`models.run_dirty`): the model declares its inputs in `marden.models.triggers` (the site fields above and `@event`; chain-party entities' `funding_fact` and events; the instruments' fields, events and `@model:price`; approved campuses' and plants' events).
- After a run for today, each site whose output changed is marked with `mark_dirty_for('site', slug, '@model:status')`: every site screen, the lists that show chain status (sites, completion, home, payers, instruments, data-center rows) and models that read the status.
- `python -m marden model status [--as-of YYYY-MM-DD] [--site slug ...] [--json]`.

## Materiality (`marden.models.materiality`)

`is_material(event, before, after)` returns a truthy `Materiality` with rule codes, reasons, a severity and the AlertSettings types to notify. `before` / `after`: `p50` (projected completion month, cushion model), `cushion_months`, `chain` (this model's output).

| Code | Material when | Alert type | Severity |
| --- | --- | --- | --- |
| M1 | moves projected completion 3+ months (or the event states a 3+ month slip) | milestone | amber |
| M2 | turns the cushion negative | cushion | red |
| M3 | denies or withdraws a power source (same rule as T2's power test; queue withdrawals) | power | red |
| M4 | comes with a tenant notice: force majeure, termination, contract cut | tenant | red |
| M5 | triggers a lender action: default notice, acceleration, waiver, forbearance, draw stop, stalled syndication (with the debt named in the same clause) | — | red |
| E1–L2 | a chain link changes status between `before` and `after` (the link's rule code) | per link | per rule |

M1 and M2 are the 00-global codes; M3–M5 name the other three conditions of the same definition.

A scheduled step (event kind `catalyst`, e.g. "CoreWeave Q3 10-Q: leverage covenant recomputed") has not happened, so its text is not tested; it is material only through the `before` / `after` states. An 8-K item caption ("Termination of a Material Definitive Agreement", usually paired with "Entry into …" in a refinancing) is not a contract cut; an SEC "large accelerated filer" is not a lender's acceleration.

On the 9,048 published events in the local database (2026-10-21), 14 are material: 10 M4 (nine Jupiter force-majeure headlines, "Microsoft pulls out of UK Nscale data centre deal"), 4 M3 (the Green Chile lateral "rerouted after rejections", three STACK Danville gas units withdrawn from the PJM queue); none of the 8,107 docket items.

## Runs on the local database (2026-10-21, status v0.2)

Each day reads the price model's latest stored output on or before it (outputs exist for 07-01, 08-17, 09-30, 10-14 and 10-21; 09-01 reads 08-17's). The 08-17, 10-14 and 10-20 backtests were re-run as well, so the stored history (home's week-ago comparison reads 10-14) is on one method.

| As of | Broken | Strained | Holding | v0.1 rule (raw price) | Notes |
| --- | --- | --- | --- | --- | --- |
| 2026-07-01 (backtest) | 0 | 1 | 28 | 0 / 1 / 28 | Colossus 2 L1 (the xAI fixed-rate term loan EX-XAI_VCI-390 at 97.7); before S&P's cut of Oracle (2026-07-09) |
| 2026-09-01 (backtest) | 0 | 14 | 15 | 0 / 14 / 15 | T1 on the Oracle-tenant sites; D1 Abilene, Lake Mariner; L1 on nine sites at 95–98: three floating loans, six fixed-rate bonds with a spread-only loss of 2 points or less (rates) |
| 2026-09-30 (backtest) | 1 | 12 | 16 | 6 / 11 / 12 | Jupiter (T2 after the 09-24 force majeure notice; L2 floating 87.7). Beacon Point, Hyperion, Polaris Forge 1 bldg 4, River Bend and The Barn were L2 on rates only (The Barn's spread 281bp = its issue spread) |
| 2026-10-21 (today) | 6 | 12 | 11 | 11 / 10 / 8 | L2 on credit: The Barn (spread 376 vs 281bp at issue, 8.7 points), Galaxy Helios II (748 vs 564bp, 6.6 points), CoreWeave DDTL 3 and 4 (floating, 89.8 / 94.3), Jupiter (floating 85.8; T2); Southgate D2. Rates only, no longer L2: Beacon Point (3.6 points on spread), Core Scientific / CoreWeave (3.0), Polaris Forge 1 bldg 4 (4.7), River Bend (0.7), Hyperion (spread tighter, −2.0) |

Jupiter's construction loan is floating rate (SOFR+250), so its L link is the same under both rules: green on 08-17, 09-18 and 09-24 (the stored price output of 08-17, 99.4: Marden's own records, not the methodology's locked path, which is below 95 on 08-17 — pricing-v0.1 "Jupiter backtest"), L2 at 87.7 on 09-30 and 85.8 today.

## Limits (v0.2)

- Most signals are still statements, read by fixed text rules. Structured facts from the extraction agents (a `force_majeure_notice` or `rating` fact) should replace them; the rules then become field checks.
- L1's price test still reads the raw lowest price (95–98), unchanged by v0.2: a fixed-rate bond that fell 2–5 points on rates alone trips L1 (Childress's IREN notes at 96.9 with a spread 6bp tighter than at issue; Polaris Forge 2 at 95.1, 32bp tighter), while one that fell below 95 on rates alone trips neither L1 nor L2 (Beacon Point 91.9, River Bend 93.5, Hyperion 91.8 read green on price). A rate-neutral L1 (spread-only loss of 2–5 points) would close the gap; it is an owner decision.
- The reference spread is the price model's own (lp held from issue plus the filtered residual); on an instrument with no issue comp it is the first stored Marden price, which depends on the days the price model has been run.
- Site → campus links are all `proposed`, so campus and plant events (e.g. the Green Chile lateral's "rerouted after rejections") are not inputs yet.
- Backtests before 2026-10-20 see only dated items and events; the archive's dossier notes (ratings text, debt notes) appear from 2026-10-20.
