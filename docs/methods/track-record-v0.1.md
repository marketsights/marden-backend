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

## Results

See "Runs on the local database" below; filled from the runs of 2026-10-21.
