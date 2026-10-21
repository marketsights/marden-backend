# Status rules v0.1 (model `status`)

Written 2026-10-21. Code: `services/marden/models/status.py` (run, CLI, tasks, triggers), `status_inputs.py` (what it reads), `status_rules.py` (the rules), `status_text.py` (text and rating rules), `status_dates.py` (in-service dates), `materiality.py` (the alert engine's test). Spec: `handoff/specs/00-global.md` "Status rules" and "Materiality and alerts".

## Output

One `model_output` row per financed site and as-of day: model `status`, version `0.1`, subject `('site', slug)`.

```
{ "chain": ChainLink[4],      // end_user, tenant, developer, lenders, in that order (types.ts ChainLink)
  "status": ChainStatus,      // 'Broken' | 'Strained' | 'Holding' | 'No build'
  "missing": string[] }       // inputs that were not on file, e.g. "lenders: Marden price (price model output)"
```

`chain` and `status` drop straight into `SiteSummary.chain` / `.status`, `CompletionRow.chain`, `InstrumentRow.chainStatus`, `FinancedLink.chainStatus` and the `status` StreamEvent (each link passes `marden.core.events` validation). Each tripped link carries `ruleCode`, a `reason` (rule, the clause that tripped it, source and public date) and `fact`: the statement as a `Sourced<string>` (tag F or R, its URL, the day it became public). The Marden price, when it trips L1/L2, is the `fact` with tag M and the price model's `methodVersion`. A green tenant link shows the rating statement it read; a green lenders link names the price when there is one.

## The rules (fixed)

| Link | Amber | Red |
| --- | --- | --- |
| End user | E1 its plan needs new funding | E2 contract cut, or the end user exits |
| Tenant | T1 rated BBB- or lower, or must post contested collateral | T2 BBB- or lower and power denied or withdrawn, or force majeure filed |
| Developer | D1 in-service date slips 3+ months | D2 construction halted |
| Lenders | L1 Marden price 95–98, or syndication stalled | L2 Marden price below 95, or default notice |

Chain status = worst link: any red → Broken, any amber → Strained, else Holding. `No build` when a filed or reported statement says construction has not started. A link with no named party (`chain_party`) is `none` with party `—`; the lenders link is judged whenever the site has debt, with party `n/d` if no lender is named. When no rule trips, the link is green and `missing` lists what was not on file: green means "no rule met on the statements Marden holds", not "checked and clear".

How each rule reads the record:

- **E1**: a funding-need statement (cash burn, negative free cash flow, funding gap, needs to raise) in the end user's `funding_fact` rows, or a site statement naming the end user.
- **E2**: a contract cut, termination, cancellation or exit tied to a contract, lease, order, commitment or the project, naming the end user.
- **T1 rating**: the tenant's latest rating statement, pooled across every site where the same entity is tenant: the sites' `tenant_rating` texts and dated rating headlines (`risk_evidence`, `schedule_change`) that name the tenant. The lowest agency grade stated for the tenant itself counts (S&P / Fitch / DBRS / KBRA letters and Moody's on one notch scale; 'high yield' without a grade counts as BB+ or lower). Clauses about a bond, a backstop or a guarantor, text in brackets and grades a contract or tariff *requires* are not the tenant's rating. 'Unrated' is not 'BBB- or lower'.
- **T2 force majeure**: a notice, invocation or filing of force majeure (not a contract clause such as "except force majeure") in a site statement naming the tenant, or in the site's own lease terms.
- **T2 power denied or withdrawn**: a denial / rejection / withdrawal word within eight words of a power object (right-of-way, pipeline, gas, interconnection, permit, PPA, transmission, generation …) in one clause of a power statement, a schedule statement or an event, and not a procedural ruling (a denied motion, rehearing or complaint; a completeness or deficiency letter; a confidentiality ruling) or a voluntary switch of technology. Queue withdrawals on approved campuses / plants count.
- **D1**: a site's ready-for-service target moved 3+ months: per project, the latest dated statement (`rfs_statement`) against the first one; and the dossier's `expected_energization` against `original_energization`. Dates are read by `status_dates`: a period counts as its last month (00-global: a year alone is Dec 31), a coarser later statement that still contains the earlier date is no slip, statement dates and labels ('Jun-11-2026 call (DCD): …') are skipped, outside estimates (Epoch, Aterio, satellite tracking) are cut off, and 'no change reported' carries no target. A delivery statement that itself says "delayed N months" counts too; a fuel-supply or permit slip does not (that is the cushion model's input).
- **D2**: construction halted, paused, stopped or suspended (not "work continuing despite a pause"), a stop-work order, or an expected completion that reads 'cancelled'.
- **L1 / L2 price**: the lowest Marden price among the site's instruments in the price model's latest `model_output` (`mardenPrice` / `price` Sourced<number>, or `value`). Until the price model writes outputs, only the text rules below apply and `missing` says so.
- **L1 syndication**: syndication stalled, struggling, failed or pulled, or arrangers holding more than planned, in the site's debt notes, tranche notes or the instruments' terms. Slow syndication alone ("has taken more than six months") is not stalled.
- **L2 default notice**: a notice of default, a declared event of default, acceleration, a missed payment, or an instrument `status` of default.

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

## Runs on the local database (2026-10-21)

| As of | Broken | Strained | Holding | Notes |
| --- | --- | --- | --- | --- |
| 2026-07-01 (backtest) | 0 | 0 | 29 | before S&P's cut of Oracle (2026-07-09); dossier notes not yet public |
| 2026-09-01 (backtest) | 0 | 7 | 22 | T1 on the six Oracle-tenant sites; D1 Abilene, Lake Mariner |
| 2026-09-30 (backtest) | 1 | 7 | 21 | Jupiter T2 after Oracle's force majeure notice (2026-09-24) |
| 2026-10-21 (today) | 2 | 14 | 13 | Jupiter (T2), Southgate (D2, partnership ended); CoreWeave B+ (T1) on six sites; E1 OpenAI cash burn |

## Limits (v0.1)

- Most signals are still statements, read by fixed text rules. Structured facts from the extraction agents (a `force_majeure_notice` or `rating` fact) should replace them; the rules then become field checks.
- No Marden price exists yet, so L1/L2 rest on syndication and default text only.
- Site → campus links are all `proposed`, so campus and plant events (e.g. the Green Chile lateral's "rerouted after rejections") are not inputs yet.
- Backtests before 2026-10-20 see only dated items and events; the archive's dossier notes (ratings text, debt notes) appear from 2026-10-20.
