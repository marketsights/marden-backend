# Marden cushion model v0.1 (Oct 21, 2026)

Code: `services/marden/models/cushion*.py`. Output: `model_output` rows of model `cushion`, version `cushion v0.1`, one per V1 site and as-of day. Spec: 22 "Cushion", 03 (Completion), 04 (Site detail Schedule tab), 00-global (model labels).

**Cushion = lease outside date − P50 projected rent start, in whole months.** Every number is computed in code from published facts and events; nothing is typed in. Ranges and odds are uncalibrated until the backtest supports them: they carry the "model v0" label and `oddsOfMissing` carries method version `odds v0`.

## 1. Inputs (point in time)

A run for today reads every published row. A run for a past day is a backtest and reads only what was public on that day (`marden.models.pit`): a fact's record date (`valid_from`) plus its source's publication lag (EIA-860M: the 24th of the following month), and events dated by then or learned by then. With `MARDEN_SHOW_UNLICENSED_PRICES=false`, rows from unlicensed sources are dropped from inputs too.

| Input | Fields |
| --- | --- |
| Schedule | site `expected_energization`, `original_energization`, `first_delivery`, `rent_start`, `rfs_statement` (dated, per project, prior/latest), `schedule_change`, `construction_stage`, `pct_complete` |
| Power | site `power_supply`, `power_status`, `power_contract`, `onsite_generation`, `air_permits`; plants serving the site (`serves_sites`): `status`, `expected_cod`, `original_cod`, `latest_change`, `air_permit` |
| Events | site catalysts, permit and opposition events |
| Lease | `outside_date`; else a lease trigger in the site's own records ("payment deferral if not operational by 2028", "if 2028 date missed"); `lease_annual_rent_usd`, else `lease_tcv_usd` / `lease_term_years` |
| Slip history | EIA-860M generators (`eia_generator` planned_online vs operating_month); the archive's dated plan revisions |

Dates are read as the sources state them: a quarter is its three months, a half six, "early" Jan–Apr, "mid" May–Aug, "late"/"end"/"YE" Oct–Dec, a year twelve. Statement dates ("(Vantage, Aug-2025)", "Jun-11-2026 call (DCD): ...") and non-schedule dates (draw periods, maturities, purchases, "as of") are not read as targets.

## 2. Milestones

The eight spec 04 milestones, in order: land and zoning; air permit; utility or large-load agreement; gas supply / grid interconnection in service; on-site generation in service; building shells; first hall energized; rent start. Each gets a status (Done, Denied, Withdrawn, Slipped, Not started, Early works, Projected, At risk) from fixed text and field rules, dated events (done, denied, slipped) and its latest evidence. "Not started" means no record of it yet. A milestone that does not apply (on-site generation and its air permit at a grid site with no generation) is listed in `notApplicable`. A done milestone implies its predecessors are done (undated).

Critical path: land → shells → first hall → rent start; utility agreement → gas supply / grid in service; air permit (+ fuel supply off-grid) → on-site generation; the first hall needs the site's power (grid sites the grid milestone, off-grid sites on-site generation, hybrid sites both). Rent start is the rent commencement of the financed scope: the last phase for a phased lease, else the first rent date; with no rent date it follows the first hall. No lag is assumed between a milestone and its successors.

## 3. Slip history

One record per project: a plan stated on day t0 for month T0 and what a later source said on day t1 (a revised plan, "on track / no change", or delivery). Slip ratio = (T1 − T0) / max(T0 − t0, 3 months).

- **EIA-860M**: a generator's first planned month against its operating month or latest plan, each dated by the report's public date. Today Marden holds one monthly report (Aug 2026), so there are no pairs yet; outputs list this under `missing`. The collector writes changes only, so unchanged re-reports add no records.
- **Marden's research archive**: per project, the earliest `rfs_statement` marked prior with a date ahead of it against the last statement marked latest (its headline hall or delivery date; grid and shell clauses are not read). Where a site has none, `original_energization` (when it names the day it was given) against `expected_energization`.

A site's own records are left out of its own pool. A category (build, power, permits) with fewer than 5 records uses all records.

## 4. Simulation

10,000 schedules per site, seeded from the inputs hash (equal inputs give equal outputs). For each pending milestone with a planned window: plan P uniform over the window (its unexpired part; wholly past = overdue, P = today), ratio r a smoothed bootstrap draw (Silverman bandwidth, floor −0.9), finish = max(P + r × max(P − today, 3 months), today), then no earlier than its predecessors. Done milestones are fixed; an undated pending milestone is not simulated and listed as missing.

## 5. Outputs (`handoff/api/types.ts`)

- `projection` (CushionProjection): `p10`/`p50`/`p90` rent-start months; `outsideDate` (the lease value and its tag; coarser than a month: the window's last day, in `outsideDateAssumed`). A year-only date ("if 2028 date missed") is a range, not a day: the deadline can be any day of that year, so the model tests the whole year and reports `outsideDateRange` {from Jan 1, to Dec 31, central Jul 1}, `oddsOfMissingRange` {best: the odds at Dec 31, worst: the odds at Jan 1} and `cushionMonthsRange` {best, worst}; the single values other models and alerts read (`cushionMonths`, `oddsOfMissing`, `outsideDateAssumed`) use the midpoint, Jul 1 (the deadline is the end of June), and `basis.outsideDate.yearOnly` says so. Until 2026-10-22 a year-only date was taken as Dec 31, its best case: Jupiter's "rent start 1 month past the deadline" and 54% odds were the most favourable reading of "2028"; on the same run the range is 1–13 months past and 54–100%, 7 months and 91% at Jul 1. The price model reads only the P50 rent start (price_chain), which the deadline does not move, so Jupiter's price is unchanged by it (82.17 on 2026-10-22); `cushionMonths` (null with no lease date); `deltaWeek` (against the stored run exactly 7 days earlier); `oddsOfMissing` = share of simulated rent starts after the outside month ('odds v0'); `costPerMonthLate` and `lateCost`: the cost of each month late (section 5a).
- `milestones` (Milestone[]) with `projected` p10/p50/p90 for simulated milestones.
- `driver`: in each simulation the milestone that set rent start is charged that run's delay against the plan; the category charged most (Power, Build, Delivery, Permits). Funding is not modelled.
- `missing`, `notApplicable`, `basis` (slip-history counts, binding shares, delay months, P50 change vs 7 days earlier, assumptions), `inputsHash`.
- `leaseOutcomes` / `leasePosition` (Lease terms tab, when an outside date exists): the share of simulated schedules that start rent by and after the outside date ('odds v0'), and the P50 position against it.
- Builders read `cushion_output.stored_output(conn, slug, day)` and `cushion_history(conn, slug, day)` (SiteSchedule.cushionHistory).

## 5a. Cost of each month late ('late cost v0.1', `marden/models/late_cost.py`)

A Marden model value (tag M) from stated terms only, in parts, shown on the Schedule cushion card with each part's formula and sources on hover:

- **Construction interest** = for each instrument on the site: amount x all-in rate / 12. Amount: drawn (outstanding) when stated, else the instrument's amount (a bond is funded at issue). Rate: a fixed coupon as stated; floating = the newest SOFR fixing public by the as-of day (New York Fed, series `sofr`) + the stated spread. An instrument with no stated amount or rate (or a repaid / proposed one, or a floating one over another benchmark) is left out and named in `excluded`.
- **Rent not yet collected** = the cushion model's rent input (lease annual rent / 12, else contract value / term), for each month rent start slips past its due date. Not counted once rent has started.
- **Who bears it** only as a source states it: the site's `structure_term` facts on completion support and reserves (interest) and on delay terms such as delay credits or abatement (rent), quoted with their tag and source; otherwise the card says it is not public. Interest is paid by the borrower; rent not collected is the landlord's.
- n/d always says why: rent started; no instrument states both an amount and a rate (which ones, and why); no debt linked.

On 2026-10-22: 25 of 38 sites have a figure, e.g. Beacon Point $76.1M a month ($4.25B x 6.129% / 12 = $21.7M + rent $54.4M), Childress $166.4M ($413M drawn of $1.545B x (SOFR 3.88% + 225 bp) / 12 + $525M x 5.96% / 12 + rent), Jupiter $95.7M ($18B x (SOFR + 250 bp) / 12; no rent disclosed); n/d with the reason for Abilene, Frontier and Lighthouse (construction loan rates not public) and Polaris Forge 1 (rent started).

## 6. Runs

`python -m marden model cushion [--as-of YYYY-MM-DD] [--site SLUG ...]`. Daily at 09:35 UTC (`models.cushion_daily`, before the 10:05 UTC read-model rebuild), and within a minute of a published fact or event on its inputs (`model_dirty` marks drained by `models.run_dirty`). After a run the site's read models are marked dirty.

## 7. Limits

| Gap | Effect | Fix |
| --- | --- | --- |
| One EIA-860M report stored | Power milestones use the archive's build revisions | Load earlier monthly files (robots.txt disallows the archive path for automated fetch; a person can download them and use `--import-dir`) |
| Outside dates: 1 filed (Childress), 1 reported lease trigger (Jupiter) | Cushion and odds n/d elsewhere | Lease exhibits and extraction |
| Slip records are few (about 17) and come from company statements | Wide, uncalibrated tails | Backtest on dated history; more sites |
| Text rules for status and dates | A misread clause moves a milestone | Extraction agent with quotes; review queue |
| Independent slips, no lags | Understates correlated delays | Fit correlation once enough history exists |
