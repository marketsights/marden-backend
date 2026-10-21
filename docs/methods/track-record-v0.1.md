# Track record v0.1 (model `track_record`)

Written 2026-10-21. Code: `services/marden/models/track_record.py` (run, assembly, refresh, CLI, task, triggers), `track_record_cases.py` (the locked cases, the market side), `track_record_scan.py` (the signal side). Specs: `handoff/specs/12-track-record.md`, `00a-product-spec.md` "Proof before selling", `00-global.md` "Status rules", "Model labels".

The backtest asks one question per past repricing: on what day did Marden's signal fire, and on what day did the market price move? Lead days = move date − first signal date. Every number is computed in code; the assumptions below were locked in writing before the first run (pricing-v0.1 "Backtesting"), and a change to any of them is a new method version.

## Output

One `model_output` row per as-of day: model `track_record`, version `0.1`, subject `('series', 'all')`.

```
{ asOf, methodVersion: 'track_record v0.1', methodUrl, window: {start, end},
  total, published,                                  // TrackRecord.total / .published
  cases: [{ key, name, instrumentId, instrumentIds,  // TrackRecordCase fields first
            flagged, moved, leadDays, backtestRunning,
            outcome: 'early' | 'same day' | 'late' | 'missed' | 'pending',
            driver: 'credit' | 'rates' | 'n/d',     // what moved the price (see below)
            signals: {status?, price?, cushion?},    // each signal's first fire
            evidence: [...], flagBasis: 'snapshot' | 'reconstructed',
            sites, baseline, missing, pending }],
  controls: [{ key, name, instrumentIds, flagged, signals, sites, graded, missing }],
  summary: { repricings, pending, flaggedFirst, sameDay, late, missed, medianLeadDays,
             creditDriven: {...}, controls, falseAlarms, ungradedFlags },
  earlierCalls: [{ name, flagged, moved, leadDays }], // RiskList.earlierCalls: lead > 0 only
  rules: StatusRuleRow[],                             // the fixed status rules, as the screen shows them
  models: [{ name, version, calibration }],
  assumptions: string[], flagBasis: string, missing: string[],
  scan: { weekly, refined, stepDays, mode, states: [...] } }
```

The read model `track_record` drops `cases` into `TrackRecord.cases` (`apps/api/src/contracts/app.ts`); `risk_list` takes `earlierCalls` for weeks whose list carries none.

## Locked assumptions

- **Window.** 2026-04-01 to the as-of day. Each instrument's baseline is its state at the start of the window, so the backtest covers the Q2–Q4 2026 repricings; the Q1 2026 neocloud dip is before it.
- **Cases.** Every V1 instrument except convertibles (their price follows the stock), grouped where one name has several notes (CoreWeave unsecured 2030–2032, five Oracle notes, CoreWeave DDTLs 1.0–5.5, the xAI VCI loans), plus the four repricings spec 00a names (Jupiter, CoreWeave unsecured, Anthropic junior, Nscale) even without an instrument or a price on file. A case whose market moved in the window is a past repricing; one that did not is a control.
- **Market move** (spec 12, "price below 95 or spread +200bp"): the first observation in the window, public by the as-of day, with a price below 95 from a baseline of 95 or more, or a spread 200bp or more over the baseline spread. A single TRACE print must be confirmed by the instrument's next print within 10 days; a fund mark, BDC mark, dealer quote or exchange quote counts on its own.
- **Observations.** `price_obs` kinds trade, dealer, quote, exchange and fund_mark (never `marden` or `legacy`), one value per instrument and day: the TRACE median if there are prints, else dealer and exchange quotes, else the median fund mark. A BDC mark adjusted for the unfunded commitment replaces the same fund's raw fair value over commitment (Nscale's raw 63.8 is a drawn-share artefact, its adjusted mark is 98.5). Public dates: N-PORT 60 days after the period end, BDC 10-Q 45 days, BDC 10-K 90 days, others on their own date.
- **Baseline.** The median of the instrument's first three observations in the window (the spread baseline: the first three that carry a spread). A loan with no observation in the window's first 30 days starts at par (loans are syndicated at par; Jupiter priced at SOFR+250 at par on 2025-11-07).
- **Driver.** A move is credit-driven when the spread at the move is 100bp or more over its baseline, rates-driven when less (most of the price fall is the Treasury move), n/d without a spread. The spec's rule counts both; the summary reports credit-driven moves separately.
- **Signals**, each against the subject's own state on the window's first day:
  - status: the chain status of any case site is worse (Holding / No build < Strained < Broken), by the fixed rules (E/T/D/L; L1/L2 include the Marden price);
  - price: the Marden price of any case instrument is below 95 when it was 95 or more (or not yet priced);
  - cushion: the cushion band of any case site moves into 0–3 or below zero from a better band (00-global alerts: "cushion under 3 months or under zero"); moves between 12+, 6–12 and 3–6 do not count.
- **Case sites.** The instruments' own sites; for an instrument without one, the sites where its issuer (or, for Anthropic, the named entity) is a chain party (CoreWeave's eight sites, Oracle's six).
- **Scan.** Weekly from the window start to the as-of day, then daily in the week before each case's first signal, so the flag date is exact to the day (other signals stay at weekly resolution).
- **Point in time.** A scan day's state is the model's stored output when a live run wrote it on that day (`created_at` in US Eastern equals `as_of`): a snapshot taken at the time. Otherwise it is reconstructed by running the model code on the day with only what was public then (`marden.models.pit`: record dates plus publication lags; the archive's dossier notes from 2026-10-20). Reconstructions are not stored as model outputs or price history.
- **Licensing.** With `MARDEN_SHOW_UNLICENSED_PRICES=false` the unlicensed sources (TRACE, Börse Frankfurt) are dropped from the market side as well as from the models' inputs.

## Snapshots versus reconstructions

Spec 12 says flag dates come only from snapshots taken at the time. Marden's live daily runs began on 2026-10-21, so every flag before that date is a point-in-time reconstruction, and each case says so (`flagBasis`). From now on each daily run is a snapshot and the backtest uses it on its day. A reconstruction is honest about inputs (only what was public that day) but not about the code: the rules and the price engine were written in October 2026, after most of these moves. That is the main limit of this track record.

## When it runs

- Weekly, Sunday 21:17 UTC (`models.track_record`, queue `models`): the full scan, before the Sunday-night risk ranking that reads `earlierCalls`.
- Within a minute of a new market price on a case instrument (`@price`) or a live status / price / cushion run on a case subject (`@model:status`, `@model:price`, `@model:cushion`) through `models.run_dirty`: a refresh that re-derives the market side and today's state on top of the latest full scan (a refresh before any full run does nothing).
- After a full run for today, or a refresh, the `track_record` view is marked dirty. A run on a subset of cases (`--case`) is not the published backtest and marks nothing.
- `python -m marden model track_record [--as-of YYYY-MM-DD] [--case KEY ...] [--workers N] [--step DAYS] [--no-mark] [--json]`. A past `--as-of` ends the window that day: only moves public by then, only signals up to then.

## A diagnostic added after the first run (not locked)

For a traded bond the Marden price follows the bond's own prints (TRACE, N-PORT, dealer quotes enter its Kalman filter), and status rule L1 trips at a Marden price of 98. So a "lead" from L1 or from the price signal is partly the bond falling through 98 before it falls through 95, which says little about foresight. After seeing the first run, each case also gets `priceFree`: the same scan without anything derived from the Marden price (the chain status without its L1 / L2 price findings, and the cushion), with its own flag and lead. The summary reports it under `summary.priceFree` with `postHoc: true`. The locked headline is not changed by it.

## Runs on the local database (2026-10-21)

Full scan, step 7 days, 3 workers: 30 weekly scan days plus 60 refinement days, 20 minutes on the shared machine (the price engine is about 15 s a day); the three past as-of runs reuse those states in the same sitting.

| As of | Repricings | Flagged first | Same day | Late | Missed | Median lead (days) | Credit-driven | Graded controls, false alarms | Price-free: first / late / missed, median lead |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-07-31 | 3 (+3 pending) | 1 | 1 | 1 | 0 | 0 | 1 | 16, 9 | 1 / 0 / 2, 4 |
| 2026-08-31 | 3 (+3 pending) | 1 | 1 | 1 | 0 | 0 | 1 | 19, 11 | 1 / 0 / 2, 4 |
| 2026-09-30 | 9 (+2 pending) | 7 | 1 | 1 | 0 | 50 | 1 | 14, 9 | 3 / 0 / 6, 54 |
| 2026-10-21 | 11 (+2 pending) | 9 | 1 | 1 | 0 | 50 | 2 | 12, 8 | 3 / 4 / 4, −6 |

Cases as of 2026-10-21 (every flag a reconstruction):

| Case | First Marden signal | Market move | Lead | Driver | Price-free flag (lead) |
| --- | --- | --- | --- | --- | --- |
| Oracle senior notes | 2026-07-09 T1 (S&P cut Oracle) on Oracle's sites | 2026-07-13 5.70% 2036 TRACE 94.2 | 4 | rates (+12bp) | 2026-07-09 (4) |
| CoreWeave unsecured notes | 2026-07-16 Marden price 94.4 (9.625% 2032) | 2026-07-16 TRACE 94.25 | 0 | credit | 2026-10-20 T1 (−96) |
| Edged 7.5% 2031 | 2026-07-27 Marden price 94.9 | 2026-07-23 TRACE 94.5 | −4 | rates | none (no site) |
| Hut 8 Beacon Point 2042 | 2026-07-21 L1 (Marden price 95-98) | 2026-08-31 TRACE 94.7 | 41 | rates | none |
| The Barn project bonds | 2026-07-09 T1 (Oracle) | 2026-09-01 TRACE 94.0 | 54 | rates | 2026-07-09 (54) |
| Meta Hyperion (Beignet) 2049 | 2026-07-21 L1 (Marden price) | 2026-09-09 TRACE 94.9 | 50 | rates | none |
| Jupiter construction loans | 2026-07-09 T1 (Oracle) | 2026-09-18 dealer 90 (FT via Kfgo) | 71 | n/d | 2026-07-09 (71) |
| Applied Digital PF1 Bldg 4 7.00% 2031 | 2026-07-17 L1 (Marden price) | 2026-09-23 TRACE 94.25 | 68 | rates | 2026-10-20 T1 (−27) |
| Hut 8 River Bend 2042 | 2026-07-22 L1 (Marden price) | 2026-09-24 TRACE 94.5 | 64 | rates (+1bp) | none |
| Galaxy Helios II 9.875% 2031 | 2026-09-23 L1 (Marden price) | 2026-10-13 TRACE 94.5 | 20 | rates (+87bp) | 2026-10-20 T1 (−7) |
| Core Scientific 7.75% 2031 | 2026-07-22 L1 (Marden price) | 2026-10-14 TRACE 93.0 | 84 | credit | 2026-10-20 T1 (−6) |
| Anthropic junior chip paper | — | no instrument or dated price on file | pending | — | — |
| Nscale GPU facilities | — | none in the window (BDC marks 98.5–98.75 adjusted for the unfunded commitment) | pending | — | — |

Controls (graded: a market price was observed and did not move): flagged Applied Digital 9.25% 2030 (T1, 10-20), Polaris Forge 2 (T1 Oracle, 07-09), Cipher Barber Lake (D1, 09-25), CoreWeave DDTLs (L1 price, 07-24), IREN Childress (L1 price, 09-09), TeraWulf Lake Mariner (D1, 08-05), Vantage HoldCo sub loan (price 94.0, 05-15), xAI VCI (L1 price, 04-29); not flagged Cipher Black Pearl, Fluidstack Abernathy, Galaxy Helios I, Meta El Paso. Abilene and Frontier (T1, 07-09) have no market price, so they are ungraded.

What the numbers say:

- By the spec's rule 11 instruments repriced in the window; 9 of the 11 moves are rates-driven (long fixed-rate bonds falling with Treasuries; Hut 8 River Bend's spread was 136bp at the move against 135bp at issue). Only CoreWeave unsecured and Core Scientific moved on credit.
- The headline (9 of 11 flagged first, median 50 days) rests mostly on L1, a Marden price of 98 or less, and on 8 false alarms out of 12 graded controls. Without the price-derived signals, 3 of 11 were flagged first, all by one event: S&P's cut of Oracle on 2026-07-09 (T1 on Oracle's six sites), 71 days before Jupiter's first quote and 54 before The Barn's bonds broke 95.
- Misses and late calls: Edged (no site, so only its price; 4 days late), CoreWeave unsecured (same day: the Marden price follows the bonds; the non-price T1 on CoreWeave's B+ rating appears only with the archive's dossier notes on 2026-10-20, 96 days after the move).
- Validation against pricing-v0.1's locked Jupiter path (below 95 on 2026-08-17; 94.1 on 2026-09-18): not reproduced. The reconstructed Marden price for Jupiter stays at 98.3–100 through 2026-09-17 and first falls below 95 (94.7) on 2026-09-18, the day the 90 dealer quote enters; 87.7 on 2026-09-30 after the force majeure notice.

## Limits (v0.1)

| Gap | Effect | Fix |
| --- | --- | --- |
| Flags before 2026-10-21 are reconstructions with code written in October 2026 | Hindsight in the rules and the price engine | Accumulate live daily snapshots; grade only those |
| L1 at a Marden price of 98 and the price signal follow the bonds' own prints | Leads on traded bonds are partly mechanical | Grade the price-free diagnostic; a price signal from untraded inputs only |
| 9 of 11 moves are rates-driven | "Repricing" by the spec rule is mostly Treasury moves | A rates-neutral price rule (price at the baseline curve) in v0.2 |
| Archive dossier notes count only from 2026-10-20 | Ratings and debt notes reach backtests late (e.g. CoreWeave B+) | Dated extraction of the underlying filings |
| Anthropic junior and Nscale have no dated price | Two named cases stay pending | A dealer quote or mark with a source; BDC Q3 marks for Nscale |
| Weekly scan, daily only before each case's first signal and first price-free signal | Its other signals are dated to the scan week unless a refined day covers them | Daily scan once the price engine is faster |
