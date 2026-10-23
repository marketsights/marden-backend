# Status rules v0.5 (model `status`)

Written 2026-10-21 (v0.1); v0.2 the same day: rule L2 reads the Marden price rate-neutral ("Why L1 and L2 are rate-neutral" below); v0.3 on 2026-10-22: rule L1 does too (v0.2 outputs had already been stored, streamed and shown, so the change is a new version); v0.4 the same day: a withdrawn permit or queue position trips T2 only when nothing replaces it ("Why a replaced withdrawal is not a failure" below; v0.3 outputs had been published to funds); v0.5 the same day: L1 and L2's price tests read market evidence only, never the Marden model price on its own ("Why the price rules read market evidence only" below). Code: `services/marden/models/status.py` (run, CLI, tasks, triggers), `status_inputs.py` (what it reads), `status_rules.py` (the rules), `status_credit.py` (the rate-neutral price test of L1 and L2), `status_power.py` (when a withdrawal is replaced), `status_text.py` (text and rating rules), `status_dates.py` (in-service dates), `materiality.py` (the alert engine's test). Spec: `handoff/specs/00-global.md` "Status rules" and "Materiality and alerts".

## Output

One `model_output` row per financed site and as-of day: model `status`, version `0.5` (method `status v0.5`), subject `('site', slug)`. The v0.1 to v0.4 rows stay stored; readers take the newest output of a day.

```
{ "chain": ChainLink[4],      // end_user, tenant, developer, lenders, in that order (types.ts ChainLink)
  "status": ChainStatus,      // 'Broken' | 'Strained' | 'Holding' | 'No build'
  "missing": string[] }       // inputs that were not on file, e.g. "lenders: Marden price (price model output)"
```

`chain` and `status` drop straight into `SiteSummary.chain` / `.status`, `CompletionRow.chain`, `InstrumentRow.chainStatus`, `FinancedLink.chainStatus` and the `status` StreamEvent (each link passes `marden.core.events` validation). Each tripped link carries `ruleCode`, a `reason` (rule, the clause that tripped it, source and public date) and `fact`: the statement as a `Sourced<string>` (tag F or R, its URL, the day it became public). The market evidence that trips L1/L2 is the `fact`: a floating loan's trade, quote or fund mark as it was published (its tag F or R, source, URL and date); a spread-loss L1 or L2 fact carries tag M, `sourceName` 'Marden status model on <the quote's source>' and `methodVersion` `status v0.5` (the loss is this model's arithmetic on the market price). When the Marden model price alone is past a line, the lenders link ends its reason with a note and trips nothing: "Marden model 83.3 (EX-JUPITER-190), no market quote; not used for status". A green tenant link shows the rating statement it read; a tenant link whose power test set a replaced withdrawal aside ends its reason with "Not T2: '<the withdrawal>' (source, day) is replaced by '<the live record>' (source, day)"; a green lenders link names the lowest market price (its kind, source and date) and, for a fixed coupon, its spread-only loss since the reference day, or says no market quote is on file.

## The rules (fixed)

| Link | Amber | Red |
| --- | --- | --- |
| End user | E1 its plan needs new funding | E2 contract cut, or the end user exits |
| Tenant | T1 rated BBB- or lower, or must post contested collateral | T2 BBB- or lower and power denied or withdrawn (a withdrawal only when nothing replaces it), or force majeure filed |
| Developer | D1 in-service date slips 3+ months | D2 construction halted |
| Lenders | L1 market price 95–98 on credit alone (a trade, exchange or dealer quote, or fund mark; fixed rate: the spread move since the reference day is worth 2 up to 5 points; floating rate: the price 95–98), or syndication stalled | L2 market price below 95 on credit alone (same evidence; fixed rate: the spread move since the reference day is worth 5+ points; floating rate: the price below 95), or default notice |

Chain status = worst link: any red → Broken, any amber → Strained, else Holding. `No build` when a filed or reported statement says construction has not started. A link with no named party (`chain_party`) is `none` with party `—`; the lenders link is judged whenever the site has debt, with party `n/d` if no lender is named. When no rule trips, the link is green and `missing` lists what was not on file: green means "no rule met on the statements Marden holds", not "checked and clear".

Display words (screens, alerts, exports; stored values, API contracts and rule codes unchanged): Broken → **At risk**, Strained → **Watch**, Holding → **No flags** (no rule tripped on the statements Marden holds, not checked and clear), No build → **Not started**; links red → **Red flag**, amber → **Warning**, green → **No flags**, none → **No data** (`marden.models.status_labels`, web `src/lib/status-labels.ts`, API `apps/api/src/lib/status-labels.ts`).

How each rule reads the record:

- **E1**: a funding-need statement (cash burn, negative free cash flow, funding gap, needs to raise) in the end user's `funding_fact` rows, or a site statement naming the end user.
- **E2**: a contract cut, termination, cancellation or exit tied to a contract, lease, order, commitment or the project, naming the end user.
- **T1 rating**: the tenant's latest rating statement, pooled across every site where the same entity is tenant: the sites' `tenant_rating` texts and dated rating headlines (`risk_evidence`, `schedule_change`) that name the tenant. The lowest agency grade stated for the tenant itself counts (S&P / Fitch / DBRS / KBRA letters and Moody's on one notch scale; 'high yield' without a grade counts as BB+ or lower). Clauses about a bond, a backstop or a guarantor, text in brackets and grades a contract or tariff *requires* are not the tenant's rating. 'Unrated' is not 'BBB- or lower'.
- **T2 force majeure**: a notice, invocation or filing of force majeure (not a contract clause such as "except force majeure") in a site statement naming the tenant, or in the site's own lease terms.
- **T2 power denied or withdrawn**: a denial / rejection / withdrawal word within eight words of a power object (right-of-way, pipeline, gas, interconnection, permit, PPA, transmission, generation …) in one clause of a power statement, a schedule statement or an event, and not a procedural ruling (a denied motion, rehearing or complaint; a completeness or deficiency letter; a confidentiality ruling) or a voluntary switch of technology. Events on approved campuses / plants count. From v0.4 a clause whose only such word is a withdrawal (withdrawn, withdrew, withdraws, withdrawal) counts only when, on the run day, no other permit, application or queue position for the same plant / campus is live (below); a denial, rejection, revocation or cancellation counts as before.
- **T2 replaced withdrawal** (`status_power`): the records are the permit, queue and interconnection events on the site and its approved campuses and plants (one record per permit number or queue id, in the state of its latest action public on the day: withdrawn, denied, rejected, cancelled, revoked, vacated, expired, terminated or retired ends it; an application, notice, preliminary decision, hearing or decision keeps it live) and the `air_permits` facts on the site and those plants (each permit's own status), plus the plants' `queue_ids` with each queue position's `status_class` (active, under construction, in service: live). A withdrawal on a plant is replaced by a live record on that plant, the campus it serves or the site; on a campus, by one on the campus, its plants or the site; on the site, by any of them. A later live record of the same permit (refiled, reinstated) replaces it; an earlier one does not. The note cites a record on the same subject first, then a pending one (an application in progress), then the latest.
- **D1**: a site's ready-for-service target moved 3+ months: per project, the latest dated statement (`rfs_statement`) against the first one; and the dossier's `expected_energization` against `original_energization`. Dates are read by `status_dates`: a period counts as its last month (00-global: a year alone is Dec 31), a coarser later statement that still contains the earlier date is no slip, statement dates and labels ('Jun-11-2026 call (DCD): …') are skipped, outside estimates (Epoch, Aterio, satellite tracking) are cut off, and 'no change reported' carries no target. A delivery statement that itself says "delayed N months" counts too; a fuel-supply or permit slip does not (that is the cushion model's input).
- **D2**: construction halted, paused, stopped or suspended (not "work continuing despite a pause"), a stop-work order, or an expected completion that reads 'cancelled'.
- **Market evidence** (v0.5, `status_inputs.market_quotes`): for each of the site's instruments, the newest of a TRACE trade (kind `trade`), an exchange or dealer quote tagged F or R (kinds `exchange`, `dealer`), or a fund mark (N-PORT median across funds, public 60 days after its period end; BDC schedule of investments, public 45 / 90 days after), usable on the day and dated at most 200 days before it, read through `price_marks.load` like the price model's own observations (point in time, the unlicensed-price switch). On one day a trade beats a quote, a quote a fund mark. Never the model's own `marden` rows, never an exchange row tagged M. An instrument with no such evidence gets no L1 or L2 price test and `missing` says so (`lenders: market price of … (a trade, exchange or dealer quote, or fund mark; L1 / L2 price test)`); its Marden price, when past a line, is a note on the link.
- **L2 price** (rate-neutral, `status_credit`): each of the site's instruments with market evidence. A floating-rate loan (`basis.terms.couponType` `floating`, or no terms in the price model's latest `model_output`) trips L2 when its market price is below 95. A fixed-rate instrument trips L2 when its **spread-only loss** is 5 points or more: the quote's spread is its G-spread on its own day's Treasury curve (yield minus the Treasury par yield at the same maturity, the price model's `spreadBp` measure, `status_credit.quote_test`); the instrument's cash flows (on the quote's day) priced at the reference day's Treasury par yield at the remaining maturity plus that spread (`spreadBp`, yield minus the Treasury par yield at the same maturity) give the *credit price*; the same with the reference-day spread give the *reference price*; loss = reference price − credit price, i.e. (spread now − spread at reference) × spread duration, revalued in full. 5 points is the move that would take a par-issued instrument below 95. When several instruments trip, the link cites the largest loss (a floating loan's loss counts as 100 − its market price).
- **Reference day**: the instrument's issue date (its issue comp: `basis.terms.issueDate` and `issuePrice`), where the reference spread is the issue price's spread on that day's Treasury curve — the price model solves its liquidity premium so it reprices the issue price that day, so this is the model's own spread at issue; else the first day Marden priced it (its earliest stored `price` output with a spread, on or before the run day), with that output's `spreadBp`. A fixed-rate instrument with neither gets no L1 or L2 price test and `missing` says so (`lenders: … (rate-neutral L1 / L2 test)`).
- **L1 price** (rate-neutral from v0.3, market evidence only from v0.5, same test as L2): when no price L2 fired, a fixed-rate instrument trips L1 when its quote's spread-only loss is 2 points or more and under 5 (the move would take a par-issued instrument to 98 or below); a floating-rate loan (or a price without terms) when its market price is 95–98. When several instruments trip, the link cites the largest loss on credit alone (a floating loan's counts as 100 − price). Until the price model writes outputs, only the text rules below apply and `missing` says so.
- **L1 syndication**: syndication stalled, struggling, failed or pulled, or arrangers holding more than planned, in the site's debt notes, tranche notes or the instruments' terms. Slow syndication alone ("has taken more than six months") is not stalled.
- **L2 default notice**: a notice of default, a declared event of default, acceleration, a missed payment, or an instrument `status` of default.

### Why a replaced withdrawal is not a failure (v0.4)

Approving the 21 site–campus links on 2026-10-22 made the on-site plants' events inputs, and Stargate Abilene went Strained → Broken on one of them: TCEQ air permit 182267 for the Abilene on-site plant, "withdrawn (project 401169)" on 2026-04-20 (TCEQ's record: an NSR application filed 2025-11-20, agency status CANCELLED). The same plant holds issued permits 177262 (PBR, backup generators), 177263 (standard permit, the Titan 350 / LM2500 turbines) and Title V O4721 (decision 2026-01-16), and its PSD permit 182126 (PSDTX1688 / GHGPSDTX263, Crusoe Energy Systems) went to preliminary decision and public notice on 2026-08-05 and 2026-08-11; the site's own `air_permits` fact (2026-10-17) lists 182126 as pending. A developer withdraws an application to refile it, fold it into another permit or move the same units to another record; that is a replacement, not a lost power source. A withdrawal with nothing live beside it still counts: STACK Danville's three gas units withdrawn from PJM's queue on 2026-09-24 (1,820 MW, no other position or permit on file) stay a power withdrawal for T2 and the alert engine's M3.

The test is point in time like every input: on 2026-08-04, before 182126's first notice, the withdrawal was replaced by the plant's issued permits (the latest, O4721); a record public only after the run day cannot replace anything. The alert engine's M3 ("denies or withdraws a power source") is unchanged: an event that withdraws a permit is still reported, since the reader decides whether the replacement holds.

### Why the price rules read market evidence only (v0.5)

Through v0.4, L1 and L2 read the Marden price, which made the lenders link circular: the price model marks a loan down (its chain intensities, the completion hazard, the recovery), and then the status says lenders are in trouble because the model marked it down. Project Jupiter's construction loans (EX-JUPITER-190, floating) read "L2 Marden price below 95: Marden price 83.3" on the model alone. A chain status is meant to be evidence the reader can check, like a default notice or a stalled syndication, so the price branch now needs a price someone outside Marden put on the instrument: a TRACE print, an exchange or dealer quote, a fund's filed mark. Filed or reported events (default notice, acceleration, missed payment, a `status` of default; stalled or failed syndication) count as before.

Runs on the local database, 2026-10-22 (status v0.5 against the stored v0.4 outputs of the same day):

- Jupiter stays Broken and its lenders link stays red, now on market evidence: "L2 Market price below 95: market price 90.0 (EX-JUPITER-190, floating rate; dealer quote, Kfgo, 2026-09-18); Marden model 82.2", the FT-reported dealer quote (tag R), plus L1 syndication stalled (SEC EDGAR, 2026-10-20). The 2026-06-30 N-PORT mark (94.25) is older than the quote.
- CoreWeave DDTL 3 (EX-CRWV-094, model 93.0, no market quote) and DDTL 4 (EX-CRWV-095, model 95.4; the newest N-PORT median, 100.0 for 2026-07-31) lose L2: Broken → Strained and Broken → Holding.
- Frontier (EX-VANTAGE_ORCL-366, model 96.6, no quote) and Galaxy Helios I (EX-GLXY-162, model 95.1, BDC 10-Q median 98.2) lose L1 on price; both stay Strained on other links.
- Polaris Forge 1 building 4 goes Strained → Broken: its 2026-10-20 TRACE print (91.5) is a 5.8-point spread-only loss since issue, where the model's own spread gave 4.7.

### Why L1 and L2 are rate-neutral (v0.2, v0.3)

A chain status is a credit signal. Under v0.1, L2 read the raw Marden price, and long fixed-rate bonds that fell only because Treasury yields rose (5-year par yield 3.71% on 2025-11-13, 4.94% on 2026-10-20; 20-year 4.69% to 5.47%) broke their chains: 11 of 29 sites were Broken on 2026-10-21, five of them (Beacon Point, River Bend, Hyperion, Core Scientific / CoreWeave, Polaris Forge 1 building 4) on rates, not credit. Holding the risk-free curve at the reference day takes the rate move out and leaves the spread move, which is what the price model attributes to credit (tenant and chain intensities, the liquidity premium held from issue, and the residual the Kalman filter reads off the instrument's own trades and marks). A floating-rate loan's coupon resets with SOFR, so its price has no rate duration and is already a credit price.

v0.2 left L1 on the raw price, so the gap stayed open one notch down: a fixed-rate bond that fell 2–5 points on rates alone tripped amber (Childress's IREN notes at 96.9 with a spread 6bp tighter than at issue; Polaris Forge 2 at 95.1, 32bp tighter), while one that fell below 95 on rates alone, or whose spread widened by 2–5 points' worth while rates also rose, read green on price (Beacon Point 91.9 with 3.6 points of spread loss, Core Scientific / CoreWeave 93.0 with 3.0, Polaris Forge 1 building 4 92.5 with 4.7). v0.3 applies the same spread-only loss to L1 (owner decision, 2026-10-22).

Why the issue date: it is the one day the price is known without the model (the issue price, from the issue comp) and the price model calibrates to it, so the reference spread is the model's own and needs no stored history. The first Marden price is the fallback for instruments without an issue comp (EX-XAI_VCI-388, first priced 2026-07-01; EX-XAI_VCI-390, first priced 2026-08-17 since the price model stopped linking it to the AH2 notes' comp on 2026-10-22); it depends on which days the price model has been run, and a backtest before that day has no reference.

Every text rule matches inside one clause and skips negated ("no force majeure notice", "cannot terminate", "not an event of default") and modal or conditional wording ("may terminate", "right to", "if", "except"). A news item filed under several sites counts for a site only if it names that site.

## Inputs and point in time

Inputs are published facts and events, read through `marden.models.pit`:

- Site facts: `risk_evidence`, `schedule_change`, `rfs_statement`, `tenant_rating`, lease terms (`lease_termination`, `lease_tenant`, `leases`, `structure_term`, `credit_support`), debt notes (`debt_facility`, `tranches`, `debt_items`), power text (`power_contract`, `power_detail`, `power_status`, `power_supply`, `onsite_generation`), `expected_energization`, `original_energization`. Chain parties (`chain_party`) and their entities' `funding_fact`. The site's instruments' `research_terms` and `status`. Events on the site, its chain-party entities, its instruments, and (approved links only) its campuses and plants; `catalyst` events (scheduled steps) are not inputs. For T2's replaced-withdrawal test (v0.4): `air_permits` on the site and its approved plants, the plants' `queue_ids` and those queue positions' `status_class` (Marden's own queue matches, tag M `linked_*` facts, are not used).
- Only tags F and R can trip a rule. Tag M rows (the archive's legacy judgments, other models' estimates, the Marden price) never trip one; the Marden price is read for the note and for a fixed coupon's terms and issue comp.
- A run for today reads every published row. A run for a past day is a backtest: a fact counts once public (`valid_from` plus the source's lag, e.g. N-PORT +60 days); `learned_at` is ignored because the archive and the first collector runs were loaded on 2026-10-21. One extra rule here: a dossier note in the seed archive (debt notes, rating text, lease terms, energization text) is public only from the archive's own date (`document.doc_date`, 2026-10-20), because the note was written then even when its record is older; dated items (risk evidence, schedule changes, RFS statements, events) are public on their own date.
- Licensing: with `MARDEN_SHOW_UNLICENSED_PRICES=false` rows from unlicensed sources are dropped from the inputs (by `pit`).

## When it runs

- Daily at 09:53 UTC (`models.status`, queue `models`): after the overnight collectors, before the 10:05 UTC screen rebuild.
- Within a minute of a published input (`models.run_dirty`): the model declares its inputs in `marden.models.triggers` (the site fields above and `@event`; chain-party entities' `funding_fact` and events; the instruments' fields, events and `@model:price`; approved campuses' and plants' events; the site's and approved plants' `air_permits` and the plants' `queue_ids`). A queue position's own `status_class` change reaches the site at the daily run: a lookup on each of the queue collectors' ~60k rows per run would slow them.
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

## Runs on the local database (2026-10-22, status v0.4)

Re-run for 07-01, 08-17, 09-01, 09-30, 10-14, 10-20 and today (2026-10-21, US Eastern business day) after the 21 site–campus links were approved, so the campus and plant events are inputs on every day:

| As of | Broken | Strained | Holding | Status changes against the stored v0.3 outputs |
| --- | --- | --- | --- | --- |
| 2026-07-01 (backtest) | 0 | 0 | 29 | none |
| 2026-08-17 (backtest) | 0 | 11 | 18 | none (v0.3 with the links would have broken Abilene on the replaced withdrawal) |
| 2026-09-01 (backtest) | 1 | 10 | 18 | Jupiter Strained → Broken: T2 on the linked Green Chile plant's "Oracle Data Center's Pipeline Plan Is Rerouted After Rejections" (Energyconnects, 2026-08-18), a denial; the v0.3 backtest was stored before the links were approved |
| 2026-09-30 (backtest) | 1 | 10 | 18 | none |
| 2026-10-14 (backtest) | 2 | 12 | 15 | none |
| 2026-10-20 (backtest) | 3 | 15 | 11 | none |
| 2026-10-21 (today) | 6 | 12 | 11 | Abilene Broken → Strained: T2 → T1, 182267's withdrawal replaced by 182126 |

Abilene is Strained on every day from 08-17 (T1 Oracle BBB-, D1, and E1 from 10-20). Of the v0.3 runs below, the 10-21 one was stored after the link approvals (Abilene Broken); the others before.

## Runs on the local database (2026-10-22, status v0.3)

Each day reads the price model's latest stored output on or before it (outputs exist for 07-01, 08-17, 09-30, 10-14 and 10-21; 09-01 reads 08-17's, 10-20 reads 10-14's). The price outputs of all five days were re-run on 2026-10-22 after the issue-comp link fix (price-engine-v0.1 "Calibration"); the status backtests of 07-01, 08-17, 09-01, 09-30, 10-14 and 10-20 were re-run with today's, so the stored history (home's week-ago comparison reads 10-14) is on one method.

| As of | Broken | Strained | Holding | v0.2 (raw-price L1) | L1 changes |
| --- | --- | --- | --- | --- | --- |
| 2026-07-01 (backtest) | 0 | 0 | 29 | 0 / 1 / 28 | Colossus 2 no longer L1: its 97.7 was EX-XAI_VCI-390 priced from the AH2 notes' issue comp (2026-02-18), before the AH3 loan existed; it is now unpriced until its first public mark (N-PORT 2026-05-31, public 07-30) |
| 2026-08-17 (backtest) | 0 | 11 | 18 | 0 / 14 / 15 | Under 2 points on spread, now green: Beacon Point, Hyperion, River Bend (Holding), Polaris Forge 2 and The Barn (still Strained on other links). L1: CoreWeave DDTL 3 / 4 and Helios I (floating, 95–98), Polaris Forge 1 building 4 (2.0 points on spread) |
| 2026-09-01 (backtest) | 0 | 11 | 18 | 0 / 14 / 15 | as 08-17 (same price output) |
| 2026-09-30 (backtest) | 1 | 10 | 18 | 1 / 12 / 16 | Under 2 points on spread, now green: Childress, Core Scientific / CoreWeave, Galaxy Helios II (Holding), Polaris Forge 2. New L1: Polaris Forge 1 building 4 (3.0 points). Jupiter L2 (floating 87.7) |
| 2026-10-14 (backtest) | 2 | 12 | 15 | 2 / 10 / 17 | New L1: Beacon Point (2.2 points), Galaxy Helios II (4.1), Polaris Forge 1 building 4 (3.5), The Barn (4.4). Under 2 points on spread, now green: Childress (Holding), Polaris Forge 2 |
| 2026-10-20 (backtest) | 3 | 15 | 11 | 3 / 15 / 11 | 10-14's prices, so the same L1 findings; Beacon Point Holding → Strained, Childress Strained → Holding (Galaxy Helios II, Polaris Forge 1 building 4 and The Barn were Strained already on other links) |
| 2026-10-21 (today) | 6 | 12 | 11 | 6 / 12 / 11 | New L1: Beacon Point (3.6 points on spread; Holding → Strained), Core Scientific / CoreWeave (3.0), Polaris Forge 1 building 4 (4.7). Under 2 points on spread, now green: Childress (IREN notes 96.9, spread 6bp tighter than at issue; Strained → Holding), Polaris Forge 2 (95.1, 32bp tighter). Still L1 on the floating price: Frontier (97.3), Galaxy Helios I (95.1). L2 unchanged: The Barn (8.7 points), Galaxy Helios II (6.6), CoreWeave DDTL 3 / 4 and Jupiter (floating) |

Under v0.1 (raw price for L1 and L2) today was 11 / 10 / 8.

Jupiter's construction loan is floating rate (SOFR+250), so its L link is the same under every version: green on 08-17, 09-18 and 09-24 (the stored price output of 08-17, 99.4: Marden's own records, not the methodology's locked path, which is below 95 on 08-17 — pricing-v0.1 "Jupiter backtest"), L2 at 87.7 on 09-30 and 85.8 today.

## Limits (v0.5)

- Most signals are still statements, read by fixed text rules. Structured facts from the extraction agents (a `force_majeure_notice` or `rating` fact) should replace them; the rules then become field checks.
- Market evidence is sparse: a construction loan may have one press-reported dealer quote and a quarterly fund mark, so its price test can rest on a quote weeks old (at most 200 days; the link names the date). A fixed coupon's terms and issue comp still come from the price model's output.
- (v0.2–v0.4, the Marden spread) A fixed coupon's L1 / L2 reading was only as good as the price model's spread: the Kalman residual lags a fast sell-off by a few days' moves (Galaxy Helios II's model spread 748bp against 800bp at its 2026-10-20 TRACE print; price-engine-v0.1 "Residual"), so a loss crossing 2 or 5 points can show a few days late. Amortization is not modelled (bullet flows), which moves a spread by about 15–20bp on the amortizing project bonds.
- The reference spread is the price model's own (lp held from issue plus the filtered residual); on an instrument with no issue comp it is the first stored Marden price, which depends on the days the price model has been run.
- 21 site → campus links were approved on 2026-10-22; the other proposed links' campus and plant events are not inputs until they are approved. Links are read as they stand today, also in backtests (a link has no point-in-time history).
- A permit 'decision' event is read as issued; the agency's record (`air_permits` status) says when one was a denial, and only from the day Marden read it. Queue positions held only through Marden's own matches (tag M) do not replace a withdrawal.
- Backtests before 2026-10-20 see only dated items and events; the archive's dossier notes (ratings text, debt notes) appear from 2026-10-20.
