# Marden price engine v0.1 (Oct 21, 2026)

Code: `services/marden/models/price*.py`. Method: [pricing-v0.1.md](pricing-v0.1.md) (the owner's methodology; this page says how the code implements it and where it departs). Outputs: `model_output` rows of models `price` (instrument), `cds` (entity), `curve` (series `all`, exactly `CurveTier[]`) and `curve_fit` (series `all`), version `price v0.1`, one per subject and as-of day; plus one `price_obs` per priced instrument and day (kind `marden`, tag M, method version `price v0.1`, source `Marden model price`).

Every number is computed in code (numpy, scipy, QuantLib 1.44) from stored rows. A missing input gives null fields and a `missing` entry. Bands are uncalibrated and carry the "model v0" label.

    cd services && uv run python -m marden model price [--as-of YYYY-MM-DD] [--instrument ID ...]
    cd services && uv run python -m marden model price_validate [--json PATH]

## 1. Inputs (point in time)

A run for a past day is a backtest: it reads only what was public that day.

| Input | Rows | Public |
| --- | --- | --- |
| Treasury par curve 1m–30y, SOFR | `series` `ust_par_*`, `sofr` (`value`) | on the curve date |
| Fund marks | `price_obs` fund_mark: N-PORT per-fund rows collapsed to a median per period (holders, min–max); BDC 10-Q/10-K medians (unfunded-adjusted M rows win) | N-PORT period + 60 days; BDC 10-Q + 45, 10-K + 90 |
| TRACE end-of-day prints, Börse Frankfurt quotes, press dealer quotes | `price_obs` trade / exchange / dealer | trade date |
| Equity closes | `series` `equity:<TICKER>` `close` | close date |
| Debt and shares | entity XBRL `total_debt_usd` (else `long_term_debt_usd` + `debt_current_usd`), `shares_outstanding` (else `common_shares_outstanding`) | period + 40 days |
| Issue comps | `series` `issue_comp:<fid>` (`deal_terms`, `issue_date`, `issue_spread_bp`, `tier`) | issue date |
| ICE BofA HY OAS | `series` `ice_bofa_us_hy_oas` | observation date |
| Terms | instrument `coupon`, `coupon_type`, `coupon_rate_pct`, `coupon_spread_bp`, `maturity`, `tenants`, `gpu_model`, `gpu_collateral`, `backstop` | from the instrument's issue date (the seed dated them by load day, though they were public at issue) |
| Chain | `chain_party` tenant of the instrument's site; entity `parent_id` | identity rows |
| Construction | model `cushion` P50 rent start of the site (latest on or before the day) | its as-of |
| Site signals | site facts `risk_power`, `power_detail`, `power_contract`, `risk_evidence`, `schedule_change`, `risk_permits`, `issue`, `materiality_note`, `onsite_generation`; site events | record date |
| GPU residual bands | `series` `gpu_residual:<chip>` `residual_pct_of_new` | as stored |

With `MARDEN_SHOW_UNLICENSED_PRICES=false`, rows from unlicensed sources are dropped before any input is built: FINRA TRACE (finra.org), Börse Frankfurt, Yahoo Finance, and ICE via FRED. The `pit` fact reader and `price_marks` both apply this.

## 2. Bond-implied CDS (`price_issuer`, `price_cds`)

Applies to every issuer with straight bonds: kind `bond`, fixed coupon, USD. Converts and foreign-currency notes are left out.

1. **Bond intensities.** Each bond observation gives a flat intensity λ at 40% recovery of par. It solves survival-weighted cash flows on that day's Treasury zero curve. The default leg is on a monthly grid; coupons are semiannual from maturity with 30/360 accrual; the solver adds accrued interest to the clean price.
   - A day uses one observation per bond, in this order: TRACE, then exchange, then N-PORT.
   - An N-PORT median needs 3 holders, or 5 for Oracle.
2. **CDS maturity.** The day's intensities are interpolated linearly by maturity to the standard CDS maturity (QuantLib `cdsMaturity`, CDS2015 rule). They are extrapolated when all bonds lie on one side, and never averaged.
3. **ISDA conversion.** QuantLib's `IsdaCdsEngine` gives the conventional spread and the upfront on a 500bp coupon. Conventions: quarterly CDS2015 schedule, Act/360, 40% recovery, Treasury discounting on a log-linear discount curve.
4. **Fusion.** A Kalman filter combines the raw daily levels in log λ.
   - State noise: 50% a year.
   - Observation noise: the price noise (TRACE 0.75pt, exchange 1.5, N-PORT 1.0/√holders) converted through dP/dλ.
   - Between prints the state moves by loading × CreditGrades elasticity × the day's equity log return. CreditGrades uses L = 0.5, λ = 0.3, R = 0.4 and the 5y Treasury.
   - The loading is estimated monthly: month-end Δlog λ against the summed equity-implied moves, OLS through the origin, shrunk toward a prior and clipped to [0, 2]. The shrinkage weight is 3 pseudo-months of a 15% move.
   - The prior makes CreditGrades' local response equal the methodology's 0.018 per unit of equity log return: β₀ = −0.018 / (λ × elasticity).

Output `cds`: `{label: "Marden bond-implied CDS", spreadBp, upfrontPct, hazard, maturity, years, band, raw: {date, spreadBp, bonds[...]}, equity: {ticker, loading, pairs, prior}, missing, assumptions}`.

## 3. Tier curves (`price_tiers`)

Spread over Treasuries = tier level + b × (HY − its sample mean) + c × NS(T), with NS(T) = (1 − e^(−T/3))/(T/3).

- **Fit.** Ridge regression on the comps issued from 2025-05-01 that were public on the day. Tier levels shrink toward the pooled mean (weight 1). b shrinks toward 33bp per point (weight 4), and c toward 0 (weight 1).
- **Floating deals.** These use margin + (SOFR − Treasury par yield at the tenor) on the issue date.
- **Tier rules** (`tier_of`), on the comp's stored type, tenant tier and rating:

| Comp | Tier |
| --- | --- |
| Construction loan | Construction loan |
| GPU lease, or junior / mezz / holdco wording | Junior chip paper |
| Tenant tier `back` (Google backstop) | Backstopped senior |
| GPU loan with a named customer | Contract-backed loan |
| GPU loan with an undisclosed customer | Junior chip paper |
| Project bond, IG-rated, on an IG lessee | IG project bond |
| Project bond, sub-IG, on an IG lessee | Backstopped senior |
| Other project bonds | Neocloud bond |

Data-center ABS/CMBS are not one of the six tiers and are left out.

Output `curve` is `CurveTier[]`. Each tier lists the priced instruments as points (years, Marden yield, id) and its fit (Treasury par yield + tier spread) at 0.5–10y. `curve_fit` holds the levels, coefficients, residual SD, leave-one-out RMSE, the sample and the exclusions.

## 4. Facility price (`price_facility`, `price_chain`)

P = Σ CF·S·D + Σ R·N·(S(t−1) − S(t))·D, with D = Treasury × e^(−(lp + r)t).

**Issuer bonds.** For a straight bond of a corporate CDS issuer (listed, or with two or more bonds):
- λ is the issuer curve at the bond's maturity: the latest cross-section's line, scaled to the filtered CDS-maturity level.
- Recovery is 40% and lp = 0.

**Facilities.** λ(t) is built along the chain:
- **Tenant leg:** (1 − 0.5) × λ_tenant. The protection is 0.5 before rent start (locked) and also 0.5 after it. The post-rent value is a v0.1 assumption: the methodology gives none, and CoreWeave-leased project bonds trade far tighter than full pass-through.
- **Obligor leg:** for an SPV of a CDS issuer, (1 − 0.5) × λ_parent when customer contracts are named, else the full λ_parent.
- **Construction leg:** until the cushion P50 rent start, 1.5%/yr × 1.75^denials (cap 6×) + 0.15/yr × min(0.25 × delays, 1).
- **Backstop:** with a guarantor, the facility defaults only jointly with it (Gaussian copula, ρ = 0.3).
- **Recovery:** buildings flat at 55%. GPU collateral starts at 55% and halves every chip half-life. The half-life comes from a log-linear fit of the stored residual bands: B200 ≈ 3.4y, H100 ≈ 0.9y.

**Signals** (`price_signals`). These are read by the status model's fixed text rules: `power_denied`, `stated_slip` ≥ 3 months near a power word, and `force_majeure` near a power word.
- Counts do not sum restatements. The denial count is the larger of the distinct denial events and the most denials any one record states ("twice" = 2; a plural = 2).
- The delay count is the larger of the distinct delay events and 1 when a record states a slip.
- A force majeure notice citing power counts as one more confirmed delay. This is the v0.1 reading: the methodology lists the notice as an event, not as a rule.

**Calibration.** The liquidity premium lp is solved so the model reprices the issue price on the issue date, with that day's curve and SOFR, then held.
- The issue price is the comp's, else 100.
- The issue comp is linked by site, type, coupon or margin, maturity year, DDTL number and phase number (score ≥ 7, unique).
- If the tenant's curve starts after the issue date, its first value is used and the basis says so.
- Without a comp, lp is solved on the first usable observation.
- With neither, the price is read off the tier curve, with a band of ±1.28 × the leave-one-out RMSE.

**Residual.** A residual spread r is filtered from the instrument's own observations. It is a random walk at 120bp a year, starting from 10bp sd at calibration. A fund mark of a loan is a stale reading (Getmansky–Lo–Makarov weights 0.6/0.3/0.1 on lags of 0, 1 and 2 months).

**Price and band.** Price = clean price at lp + r. The 80% band is the price at r ± 1.28 sd, with the tenant's intensity uncertainty added. `change1w` is measured against the stored output exactly 7 days earlier, else this run's filtered path.

Output `price`: `{price, spreadBp (yield − Treasury par at maturity), yieldPct, recovery, change1w, inputs: [{key, label, value, unit, tag, asOf, methodVersion?, sourceName?}], band: {low, high, level, label: "model v0"}, missing: [...], basis, label: "Marden model price", methodVersion, tier}`. The input keys include `issuerCdsBp`, `tenantCdsBp`, `obligorCdsBp`, `permitDenials`, `powerDelays`, `liquidityPremiumBp`, `residualBp`, `treasuryYieldPct`, `sofrPct` and `impliedDefaultOdds`.

Not priced in v0.1, each listed in `missing`: convertible notes (no equity-option model), non-USD notes, and instruments without maturity or coupon terms.

## 5. Runs and triggers (`price_run`)

**Scheduled run.** Daily at 09:40 UTC (05:40 EDT). That is after the 09:35 cushion run and before the 09:53 status run, which reads the price.

**Triggered runs.** Within a minute of any of these inputs (`marden.models.triggers`, every mark re-runs the whole engine):
- an observed `@price` other than the model's own `marden` rows (so the model never triggers itself);
- a series `value` or `close` of `ust_par_*`, `sofr`, `ice_bofa_*`, `equity:*` or `gpu_residual:*`;
- an issue comp;
- an instrument term;
- a signal fact, an event or `@model:cushion` on a site with debt;
- an entity's XBRL debt or shares.

**After each run.** The run marks `@model:price` (instrument), `@model:cds` (entity) and `@model:curve` (series `all`). The read models `instruments`, `instrument_detail`, `curve`, the site views and the payer views then rebuild.

## 6. Validation (grading only; `price_validate`)

[RESULTS]

## 7. Known gaps

- **ICE BofA HY OAS.** The FRED fetch returned FRED's "Site Maintenance" page for BAMLH0A0HYM2 on 2026-10-21, so no HY values are stored. The tier curves are fitted without the market term and do not move with HY.
- **CoreWeave equity moves.** CoreWeave has no XBRL `shares_outstanding` or `total_debt_usd` facts, so CreditGrades cannot run and its loading is 0.
- **N-PORT before mid-2026.** Marks for Oct 2025 – Apr 2026 are not loaded (DERA 2025q4 and 2026q1 data sets). Two of the seven CoreWeave dates cannot be graded, and Oracle has no intensity before TRACE starts (2026-01-02).
- **Tenants with no straight bonds.** Meta, Microsoft, Amazon, Google, OpenAI, Fluidstack and xAI have no straight bonds in the instrument table, so their legs and the Google backstops are not priced (named in `missing`).
- **Jupiter signals.** The three dated signals the methodology uses (2026-03-31 right-of-way rejection, 2026-07-16 reconsideration denied, 2026-08-17 FERC in-service slip) are not stored as dated records. Marden's records state them only from 2026-09-24 (Bloomberg Tax) and 2026-10-17 (research archive). The production path therefore shows no early warning.
- **Unvalidated parameters.** The locked signal parameters (1.75, 0.25, 0.15) and the v0.1 judgment values (post-rent protection 0.5, ρ = 0.3, GLM weights, state noises) are not estimated.
