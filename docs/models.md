# Marden models

Every number on a screen that is not a filed or reported fact comes from one of the models below. They are Python under `services/marden/models/`, use numpy, scipy and QuantLib, and never call an LLM: agents extract facts with quotes, the models compute (ARCHITECTURE rule 7). This page lists each model's inputs, its output (the `types.ts` piece and where it is stored), its version, how far it is calibrated, and how it was validated. The method pages under `docs/methods/` hold the full rules.

Reviewed 2026-10-21 (adversarial model review). Numbers are from the local database on that day.

## How every model runs

- **Storage.** A run writes one `model_run` row and one `model_output(model, version, subject_type, subject_id, as_of, output jsonb)` row per subject and as-of day (`marden.models.runs` / `marden.models.sourced`). A re-run of the same day replaces that day's rows. The price model also writes `price_obs` rows of kind `marden` (tag M, `method_version 'price v0.1'`).
- **Output shape.** `output` is exactly the `handoff/api/types.ts` piece the read-model builder drops in, plus bookkeeping keys (`missing`, `basis`, sometimes `request` or `inputsHash`). `tests/models/test_model_output_shapes.py` compiles the newest stored output of every model a builder reads (all but the `curve_fit` diagnostics) with `tsc --strict` against those interfaces (and against `apps/api/src/contracts` where the API owns the type).
- **Tags.** Model values are `Sourced<T>` with tag `M`, a `sourceName` and a `methodVersion` of the form `<model> v0.1`. Odds, ranges and bands that no backtest has calibrated carry `model v0` or `odds v0` (00-global "Model labels").
- **Missing inputs.** A missing input gives `null` (n/d) in the output field and a line in `missing`; it is never 0 and never guessed (00-global data rule 3). Where a sum has an unsized part, the known part is kept in `basis` as a floor and the sum is n/d, or the output says it is a floor.
- **Point in time.** Inputs are read through `marden.models.pit` (facts and events), `marden.models.price_marks` (price observations) and `track_record_cases.market_series`. A run for a past day is a backtest: it sees only what was public that day (see "Point in time" below). `pit.audit()` lists every input a run read with the day it became public; `python -m marden model price|cushion --audit-inputs` prints it. `tests/models/test_model_input_paths.py` fails on any direct `fact` / `event` / `price_obs` / `holding` read that bypasses these readers without a listed reason.
- **Licensing.** With `MARDEN_SHOW_UNLICENSED_PRICES=false`, rows from the unlicensed sources (`marden.core.licensing`: finra.org TRACE, Börse Frankfurt, Yahoo Finance, ICE via FRED, SPP, El Paso Electric) are dropped from every model input, not only from display (`docs/methods/licensing.md`). Outputs already stored keep the inputs they were computed with: re-run the models after flipping the switch.
- **Schedules and triggers.** Every model is a procrastinate task in the `models` queue with a periodic schedule (spec 22 "What runs when") and input triggers: a published fact, event, price or model output that matches a `model_depends` rule upserts `model_dirty(model, key)` in the writer's transaction, and `models.run_dirty` (every minute) runs the model on the marked keys. A run for today marks `@model:<name>` on its subjects, which dirties the read models (and the downstream models) that read it.
- **CLI.** `python -m marden model <name> [--as-of YYYY-MM-DD]` for every model below.

## Point in time

| Input | Public on | Where |
| --- | --- | --- |
| Fact (filing, statement, report) | its record date (`valid_from`) | `pit.public_date` |
| N-PORT fund mark or fund net assets | period end + 60 days | `pit`, `price_marks`, `mark_gap_inputs.line_public_date` |
| BDC schedule of investments | filing date, else quarter end + 45 days (10-Q) / 90 days (10-K) | `price_marks`, `mark_gap_inputs` |
| SEC XBRL period value | period end + 40 days, or the filing date when later (10-Ks, late filers, restatements: the filing named in `quote_loc`) | `pit.public_date` |
| SEC XBRL cover-page count (`dei:` shares) | its filing date | `pit.public_date` |
| EIA-860M monthly inventory | the 24th of the following month | `pit.public_date` |
| TRACE print, exchange quote, dealer quote | its date | `price_marks` |
| Event | its date; a dated-ahead hearing or deadline once Marden learned it | `pit.event_rows` |
| Stored model output | its as-of day | `runs`, `pit.note_outputs` |
| Instrument static terms | the instrument's issue date (else its first public price); an instrument not issued by the day is not priced | `price_terms.note_terms` |

Known limits: an event's date is its record date, so minutes or agendas posted after the meeting (some county uploads carry a later path date) are visible to a backtest from the meeting date; none of them is material to a status rule. The research archive's records are dated by their own record dates; the archive file date is not used as a public date.

## The models

| Model | Code | Subject → output (`types.ts` piece) | Inputs | Runs (UTC) | Version | Calibration | Method page |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `status` | `status.py`, `status_inputs.py`, `status_rules.py`, `status_text.py`, `status_dates.py`, `materiality.py` | site → `{chain: ChainLink[4], status: ChainStatus, missing}` = `Pick<SiteSummary, 'chain' \| 'status'>` | site facts and events, chain parties' funding and ratings, instrument status and terms, the Marden price (`price` output) | daily 09:53; on site, chain-party, instrument, price, campus and plant inputs | `0.1` | fixed rules (00-global status table), no fitted parameters | `status-rules-v0.1.md` |
| `cushion` | `cushion*.py` | site → `{projection: CushionProjection, milestones: Milestone[], driver, leaseOutcomes, leasePosition, notApplicable, missing, basis}`; lease pieces = `SiteLeaseTab['outcomes' \| 'today']` | milestone facts and evidence dates, EIA-860M planned vs actual, the archive's slip history, outside dates, rent | daily 09:35; on milestone, plant, obligation and EIA inputs | `cushion v0.1` | 10,000 simulated schedules; `oddsOfMissing` is `odds v0` | `cushion-v0.1.md` |
| `price` (+ `cds`, `curve`, `curve_fit`) | `price*.py` | instrument → `{price, mardenPrice: Sourced<number> (InstrumentRow.mardenPrice), spreadBp, yieldPct, change1w, recovery, band {low, high, level 0.8, label 'model v0'}, inputs: Sourced<number>[], tier, missing, basis}`; entity → CDS `{label 'Marden bond-implied CDS', spreadBp, upfrontPct, hazard, maturity, years, band, raw, bonds, equity, missing}`; series `all` → `CurveTier[]` (+ `curve_fit` diagnostics) | Treasury curve and SOFR, N-PORT / BDC marks, TRACE, exchange and dealer quotes, XBRL debt and shares, equity closes, issue comps, site power and permit signals, the cushion output | daily 09:40; on instrument terms, prices, XBRL, equity, issue comps, site signals and cushion | `price v0.1` | CDS graded (below); tier curves fitted on 43 deals; bands `model v0`; signal rules (1.75, 0.25, 0.15) are judgment | `pricing-v0.1.md`, `price-engine-v0.1.md` |
| `mark_gap` | `mark_gap.py`, `mark_gap_inputs.py` | instrument → `{holders: Holder[], count, parMm, fundMarks, nextMarksPublic, markGap, mardenPrice, missing, basis}` | N-PORT and BDC holding lines, fund net assets, the Marden price | daily 09:48; on holdings, marks, net assets and price | `0.1` | arithmetic, nothing to calibrate | `mark-gap-v0.1.md` |
| `gpu_index`, `gpu_model` | `gpu_index.py`, `gpu_inputs.py`, `gpu_value.py` | series `all` → `{index: GpuPricePoint[], providers: GpuProviderPrice[], models, decline}`; gpu_model → `GpuModelDetail` | public GPU list prices, chip specs and launch dates, useful-life policies, Treasury curve, GPU-loan spreads | daily 09:23; on GPU series and useful life | `0.1` | `model v0` until 50 resale prices per model | `gpu-collateral-v0.1.md` |
| `gpu_collateral` | `gpu_collateral.py`, `gpu_facilities.py` | instrument → `GpuCollateral` | the GPU value curve, facility units, drawn amounts, coupons, maturities | daily 09:27; on facility facts and GPU series | `0.1` | `model v0` | `gpu-collateral-v0.1.md` |
| `gpu_deployment` | `gpu_deployment.py` | series `all` → `GpuDeploymentPage` | facility units, site MW, XBRL construction in progress and PP&E (filing dates applied) | daily 09:31 | `0.1` | `model v0` | `gpu-collateral-v0.1.md` |
| `recovery` | `recovery.py`, `recovery_shell.py` | instrument → `{recovery: Sourced<string>, valueB: Sourced<number>, method, missing, basis}` | GPU collateral output, site cost, MW and completion, debt stack seniority and sizes | Mondays 09:41; on collateral and site inputs | `0.1` | `model v0` ranges | `recovery-v0.1.md` |
| `obligations`, `obligation_company` | `obligations.py`, `graph.py`, `credit_inputs.py` | series `all` → `ObligationGraph`; obligation → `ObligationEdge`; entity → `CompanyObligations` | obligation facts (totals, schedules, links), XBRL cash flow, cash and backlog, status outputs | daily 09:25; on obligation, XBRL and status inputs | `0.1` | arithmetic over mapped edges | `payer-capacity-v0.1.md` |
| `payer_capacity` | `payer_capacity.py`, `payer_years.py` | entity → `PayerCapacity` | XBRL financials, ratings, obligation edges, bond-implied CDS | daily 09:29; on XBRL, rating, obligation and CDS inputs | `0.1` | downgrade bands are rules; `model v0` | `payer-capacity-v0.1.md` |
| `leases` | `lease_map.py` | series `all` → `LeaseMapPage` | lease and compute edges, dependent debt and holders, payer capacity bands | daily 09:33; on obligation inputs and `payer_capacity` | `0.1` | priority weights uncalibrated (`model v0`) | `payer-capacity-v0.1.md` |
| `capacity` | `capacity.py`, `capacity_methods.py` | campus, building → `CapacityEstimate` | filed MW, grid requests, generators, plants, chips, PUE, TDP | Mondays 09:45; on facility inputs | `0.1` | GPU share of IT fitted leave-one-out on 24 campuses; `model v0` | `capacity-v0.1.md` |
| `stress` (incl. contagion) | `stress.py`, `stress_run.py`, `stress_scenarios.py`, `stress_price.py`, `stress_inputs.py`, `contagion.py` | scenario → `StressResult` / `ContagionResult` (+ `request`); instrument → `{rows: InstrumentDetail['stress']}`; entity → `{stressSummary}` | the price engine's book for the day, the cushion simulation, obligations, holders | nightly 06:17; custom runs through `stress_run(jsonb)` → `models.stress_custom` | `0.1` | `model v0` | `stress-v0.1.md` |
| `risk_list` | `risk_list.py`, `risk_note.py` | week → `RiskList` (frozen once published) | status, cushion, mark gap and price outputs | Mondays 03:40 (Sunday night ET) | `0.1` | rule-based score; odds `odds v0` | `risk-list-v0.1.md` |
| `track_record` | `track_record*.py` | series `all` → `{cases, earlierCalls, total, published, asOf, ...}` (cases = `A.TrackRecordCase` fields) | market prices (TRACE, marks, quotes) and point-in-time re-runs of status, price and cushion | Sundays 21:17; refresh on new prices and live runs | `0.1` | reconstructed flags until live snapshots accumulate | `track-record-v0.1.md` |

## Validation

### Price engine against `pricing-v0.1.md` "Results to reproduce"

Reproduced twice: by the engine (`python -m marden model price_validate --json PATH`) and by an independent re-computation written for this review from the raw database rows (own Treasury bootstrap, own hazard solve, QuantLib's ISDA engine; script kept in the review scratchpad, not in the repo).

| Test | Target | Engine | Independent | Verdict |
| --- | --- | --- | --- | --- |
| CoreWeave 5y CDS, mean abs error / bias | ~12% / +1% | 10.3% / −2.9% (5 of 7 dates) | 10.3% / −3.2% (same 5) | met on error; bias −3% (vendors differ by ~13%) |
| 2026-10-15 (vendor 1,194; method 1,019) | | 994 | 989 | |
| 2026-10-14 (vendor 1,050; method 1,176) | | 1,054 | 1,048 | |
| 2026-07-29 (vendor 855; method 736) | | 740 | 738 | |
| mid-June (vendor 452; method 436) | | 439 (05-31 marks) | 439 | |
| 2025-09-26 (vendor 371; method 432) | | 438 (one note) | 437 | |
| 2025-12-12, 2025-11-20 | 765 / 766 | not graded: no CoreWeave N-PORT marks for Oct–Dec 2025 | same | open |
| Oracle 5y CDS | 21% / −10% | not graded: no vendor Oracle levels in any stored source | | open |
| CoreWeave leave-one-bond-out | 17 prices, 0.76 MAE, 0.87 RMSE, 65% within 1 pt | 11 prices, 0.81 MAE, 0.91 RMSE, 64% within 1 pt (was 14 prices, 0.86 / 0.95 / 57% before May was de-duplicated) | 11 prices, 0.80 / 0.90 / 64% | close on error; fewer prices (Apr–Jul 2026 month-ends with ≥3 holders exist for 11 note-months) |
| Jupiter, locked path with the methodology's event list | below 95 on 08-17; 94.1 on 09-18 | 94.30 on 08-17 (first below 95); 94.42 on 09-18 | not re-implemented | met; the liquidity premium (160.5bp vs 172bp) is solved with Oracle's first public intensity (2026-01-02), since none was public on the 2025-11-07 issue day: a look-ahead confined to this validation path |
| Jupiter on 03-31 / 07-16 / 09-24 | 98.2 / 96.7 / 89.0 | 98.74 / 96.94 / 91.81 | | 09-24 missed (no rule for a force majeure notice beyond one more delay) |
| Jupiter from Marden's own records | (no target) | stays near 99 until 09-24; first below 95 on 09-28 | | the dated April/July/August events are stored only in later summaries |
| Tier curve levels (bp) | 239 / 252 / 295 / 311 / 462 / 505 | 242 / 268 / 291 / 313 / 352 / 414 (43 deals) | | junior chip paper and neocloud bonds low; residual SD 131 vs 91, LOO RMSE 138 vs 111 |

### Other models

| Model | Check | Result |
| --- | --- | --- |
| `status` | design fixture (`handoff/fixtures/completion.json`) | Jupiter E1/T2/D1 match; Frontier, Lighthouse, The Barn match; L link now follows the Marden price (L2 below 95) |
| `status` | materiality scan of 9,048 published events | 14 material (10 M4, 4 M3); 10 false positives fixed |
| `cushion` | Jupiter outside date from the reported lease trigger | 2028 (taken as 2028-12-31), cushion −2, odds 0.555 (`odds v0`) today |
| `mark_gap` | holder counts and filed marks recomputed from the copied filings | match; Thrivent 89.5031 and Hartford 76.10 reproduce `price-sources.md` |
| `gpu_*` | H100 resale (one observation) | model $6,254 vs $14,250 realized: conservative, stays `model v0` |
| `payer_capacity` | spec 24 Oracle backtest (data public 2026-04-01) | Watch a quarter before S&P's 2026-07-09 cut |
| `capacity` | 49 campuses with disclosed chip counts | median error 62%, stays `model v0` |
| `stress` | in-process re-price vs the stored price output | identical (tested) |
| `track_record` | 13 cases | 9 of 11 flagged first, but only the Oracle downgrade is a price-free early call (see `track-record-v0.1.md`) |

### Point-in-time audits (this review)

Runs re-done under `pit.audit()`; "late" = inputs not yet public on the run day.

| Run day | Model | Inputs read | Late | Note |
| --- | --- | --- | --- | --- |
| 2026-08-17 | `price` | 21,749 (7,705 facts, 10,458 price observations, 2,781 events, 779 static terms) | 0 | N-PORT marks used only from period end + 60 days |
| 2026-08-17 | `cushion` | 162 facts | 0 | |
| 2026-08-17 | `status` | 6,661 | 0 | |
| 2026-08-17 | `mark_gap` | 898 | 0 | |
| 2026-07-01 | `price` | 17,501 | 0 | |
| 2026-07-01 | `stress` | 18,859 | 0 | 96 late before the fix (issue comps dated after the day) |
| 2026-09-30 | `price` / `mark_gap` / `stress` | 25,620 / 2,458 / 29,702 | 0 / 0 / 0 | stress had 10 late before the fix |
| 2026-10-14 | `price` / `stress` | 26,719 / 62,737 | 0 / 0 | |
| 2026-10-21 (today) | all 15 run here | 28,049 for `price` | 0 | today's `mark_gap` read 4 N-PORT net-asset facts inside their 60-day window before the fix (unused: no holder line of that period was usable) |

`tests/models/test_model_pit.py` re-runs the price and cushion models as of 2026-08-17 and the stress model as of 2026-07-01 on copied real rows and asserts the audit's input list holds nothing public after the day, while a run for today does read later inputs; it also unit-tests the N-PORT, XBRL (10-K, restatement, cover-page) and static-terms public dates and the licence switch.

## Review findings fixed (2026-10-21)

- **XBRL look-ahead.** The 40-day rule let backtests read 10-K values (filed up to 90 days after year end) and restated values before their filing: 5,754 of 15,456 XBRL facts were filed more than 40 days after their period end. `pit.public_date` now also waits for the filing named in `quote_loc`; cover-page share counts are public on their filing date.
- **Stress backtests read late issue comps.** `stress_price.Book.build` attached instrument terms from comps dated after the run day outside `pit.unaudited()` (96 late inputs on 07-01). The comps are static terms dated their issue day and an instrument not yet issued is not priced, as in `price.run_day`; the attach is now unaudited the same way.
- **Leave-one-bond-out double-counted May.** Two May month-end cross-sections (05-29 and 05-31) were graded separately; one per month is graded now.
- **n/d shown as 0.** Default odds of a facility with no tenant, obligor or guarantor intensity (0 → n/d); the equity loading of an issuer whose equity model cannot run (0 → n/d); `fundsHit` in severe scenarios whose prices are n/d (0 → n/d); `totalAtRiskB` of a contagion walk with no scheduled payment (0 → n/d, and a floor note when partial); a holder filed with no principal amount (`parMm` 0 → n/d).
- **Holders.** A fund filed with a positive value but no principal amount is a holder again (it was dropped with the short positions); a holder's `parMm` keeps six decimals, so a $430 line is not shown as 0. Fund net assets from N-PORT are used only from period end + 60 days on today's run too, as the holder lines already were.
- Earlier in the review: the archive's own computed issue spreads (tag M) are recomputed in code (`issue_spreads.py`); legacy terms give way to the issue comp's sourced terms; partial sums with unsized edges are n/d with a floor in `basis`; every M value names its source.

## Open items

- **Archive values without a source.** A few inputs are the research archive's own records with no URL or quote (tag M, `Marden research archive`): the sizes of Abilene phase 1 and 2, The Barn bonds and the Vantage TX + WI debt, two maturities and one coupon, three future-chip TDPs and launch dates, and the American Compute residual bands behind the GPU half-life. Each size matches the sourced issue comp (SEC or KBRA; Vantage = 23.25 + 14.75), and the price model already prefers the comp's terms, but the instrument facts should carry those citations (seed loaders).
- **Lender link on rates.** Status rule L2 (price below 95) now reads the Marden price, so long fixed-rate bonds that fell with Treasuries (The Barn 2045, Hyperion 2049) turn their chains Broken: 11 of 29 sites are Broken today. The rule is as specified; whether it should use spread instead is an owner decision.
- **Sums over the mapped graph.** `computeCommitmentsB`, `owesB`, `owedB` and lease `priorityScore` are 0 when no mapped edge or dependent debt exists (they are defined over the mapped obligation graph); an unsized edge makes them n/d or a floor.
- **Schedule clock.** Crons are UTC: in winter (EST) the 09:40 price run is 04:40 ET, before spec 22's 5–6 am window, and the 09:53 status run 04:53 ET.
- **Over-marking.** `readmodels/deps.py` marks `curve` and `gpu_collateral` for every `series` model output (`@model` prefix), so `track_record`, `leases`, `obligations` and `gpu_*` runs also rebuild those views. Harmless (extra rebuilds); `@model:curve` / `@model:gpu_deployment` would be exact.
- **Inputs not collected:** ICE BofA spread indexes (FRED page down; severe scenarios n/d), agency downgrade triggers, 10-K lease-commencement totals, 2025 N-PORT history (two CDS dates), Oracle vendor CDS levels, licensed TRACE.
