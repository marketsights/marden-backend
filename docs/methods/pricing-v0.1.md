# Marden Pricing Methodology v0.1 (Oct 19, 2026)

Copied from the Marden Pricing Methodology v0.1 doc (Cody Lister). This is the spec the price model implements. Numbers in "Results" are the validation targets the rebuilt engine must reproduce from the same inputs.

Marden prices AI-infrastructure project and SPV debt from public data alone: fund filings, traded bond prices, SEC filings and public site records.

## Results to reproduce

| Test | What was compared | Result |
| --- | --- | --- |
| CoreWeave 5y CDS (7 dates, Sep 2025 – Oct 2026) | Marden bond-implied CDS vs vendor composites reported in the press | 12% average error, +1% bias |
| Oracle 5y CDS (7 dates with a filing within 20 days) | Same method | 21% average error, −10% bias |
| CoreWeave leave-one-bond-out (17 prices, Apr–Jul 2026) | Each bond priced from the other CoreWeave bonds vs its actual fund-reported price | 0.76 pt MAE, 0.87 pt RMSE, 65% within 1 pt |
| Jupiter construction loan (~$18B) | Locked model path vs first public price (FT, 2026-09-18) | Below 95 on 2026-08-17; 94.1 on 2026-09-18 vs 89–91 quoted (post-hoc) |

CoreWeave detail, Marden vs vendor-reported 5y CDS (bp), grading only (never an input):

| Reported date | Vendor | Marden | Error |
| --- | --- | --- | --- |
| 2026-10-15 | 1,194 | 1,019 | −15% |
| 2026-10-14 | 1,050 | 1,176 | +12% |
| 2026-07-29 | 855 | 736 | −14% |
| 2026-06 (mid) | 452 | 436 | −4% |
| 2025-12-12 | 774 | 765 | −1% |
| 2025-11-20 | 675 | 766 | +13% |
| 2025-09-26 | 371 | 432 | +16% |

Vendor composites disagree with each other by ~13% on the same day. Three benchmark points are weak (371 and 774 trace to one X post; the 1,050 is undated in its source). Reported levels are matched to the nearest month-end bond prices, 2–15 days apart.

## The model

Reduced-form credit (Duffie–Singleton 1999, Jarrow–Turnbull 1995). Price = survival-weighted cash flows + expected recovery, discounted on the Treasury curve plus a tier liquidity premium:

    P = Σ_t CF_t S(t) D(t) + Σ_t R(t) N [S(t−1) − S(t)] D(t),   S(t) = exp(−∫_0^t λ(u) du)

S survival, λ default intensity, R recovery share of par, N notional, D discount factor.

1. **Tier curves.** Comparable spreads by tier from deal pricing at issue, moving with the HY market.
2. **Facility adjustments.** λ built up along the payment chain: tenant credit implied from the tenant's own bonds; contract protection; construction-stage completion risk with jumps on public power and permit events; force majeure delay risk; backstops priced as joint default with the guarantor (Gaussian copula); recovery with buildings flat and GPUs/TPUs depreciating on a half-life.
3. **Calibration.** A Kalman filter fuses sparse, noisy, stale observations into a daily estimate. Bond-implied levels anchor it; daily equity-implied moves update it in between. Stale fund marks are a smoothed, lagged reading (Getmansky, Lo and Makarov 2004).
4. **Backtesting.** Every input carries the date it became public; assumptions are locked in writing before a backtest runs.

## v0.2: recovery from the site collateral model (Oct 22, 2026)

Owner decision 2026-10-22 ("Do what is proper"). Code: `services/marden/models/price_chain.py` (`site_recovery`, `recovery_fn`), `price_facility.py` (`_recovery_sensitivity`). Version `price v0.2`; the Jupiter backtest keeps its locked 55% (below).

- **Which recovery.** A facility (not an issuer bond) prices with the `recovery` model's central estimate for the instrument when that model has an output on or before the pricing day, at most 14 days old (it runs weekly and on its inputs), with a number (`recoveryBasis: "site_waterfall"`):
  - **Powered shell:** the base case (50th-percentile cost comp), flat over the life. The waterfall already takes the claims ranking ahead of the instrument and shares the value with the claims at its rank, from the seniority facts; a claim of unknown rank or size makes it n/d. When no source states how much is built, the recovery model estimates it (recovery-v0.1.md "v0.2").
  - **GPU collateral:** the recovery at each scheduled balance over the loan's life (`pathCents`: the GPUs age while the loan amortizes), interpolated in time and held flat after the last point. This replaces the half-life decay, which the waterfall covers.
- **Fallback.** Only without such an estimate (no output, an n/d waterfall, or a waterfall of other collateral than the price chain's) the v0.1 defaults apply (`recoveryBasis: "default_assumption"`): buildings flat 55%, GPUs 55% halving on the chip half-life. Issuer bonds keep 40%: their intensity is solved from their own prices at that recovery, so it is a convention, not an estimate.
- **Point in time.** The recovery is the pricing day's estimate. It is applied to the calibration at issue as well (as the GPU half-life already was), so the liquidity premium is solved with it. A backtest reads only the recovery output dated on or before its day.
- **Outputs.** `recoveryBasis`, `recoveryInputs` (method, cents used, range, path, the waterfall's inputs: collateral value, debt ahead, claims at the rank, lien, completion and how it is estimated; the recovery output it came from; why the default applies), an input `recoveryCents` (sourced to the recovery model, or to the price engine for a default) and `recoverySensitivity`.
- **Sensitivity.** `per10cPts`: the price change per 10c of recovery, by central difference of ±5c (one-sided at 0c / 100c), with the whole model re-run: the liquidity premium re-solves to the issue price and the residual re-filters. `holdingSpreadPer10cPts`: the same at today's spread, without re-solving. Because the premium absorbs the recovery at calibration, a recovery change moves the price mostly through the change in default intensity since issue: little for a facility whose tenant curve has not moved, several points where it has (Jupiter, the CoreWeave DDTLs). With no tenant, obligor or guarantor intensity, recovery does not move the price at all.
- **Triggers.** A recovery run marks the price model dirty (`@model:recovery`).

## v0.3: issuer recovery from the balance sheet (Oct 22, 2026)

Issuer bonds priced at a flat 40% recovery. v0.3 prices them at the issuer's unsecured recovery from its
balance-sheet waterfall (recovery-v0.1.md "v0.3"), `recoveryBasis` 'issuer_waterfall'. To keep the bond-implied
CDS calibration consistent, each issuer's curve is solved from its bond prices at that same issuer unsecured
recovery (one recovery for all its notes, not each bond's own), and the ISDA conversion uses it too; the CDS
output's `recovery` says which. A bond's model price therefore barely moves (the curve absorbs the recovery), while
the issuer's default intensity rises with it (CoreWeave 0.176 → 0.210 at 48c), so facilities with a CoreWeave
tenant or obligor leg price lower (up to about 2 points on Oct 22). An issuer without a waterfall keeps 40% and
says why. A day's Marden price row is the latest price version's.

## Data (all public, point in time)

| Input | Source | Frequency | Use |
| --- | --- | --- | --- |
| Bond prices (CoreWeave 4 notes; Oracle 14–20 notes) | SEC N-PORT holdings, median across holders (≥3 for CoreWeave, ≥5 for Oracle) | Monthly, ~60-day lag | Bond-implied default intensity |
| Treasury curve (1–30y) and SOFR | FRED | Daily | Discounting |
| ICE BofA HY, CCC, single-B, BBB, IG spread indexes | FRED | Daily | Tier-curve market factor |
| Stock prices (23 tickers) | Exchange data | Daily | Equity-implied (CreditGrades) daily moves |
| Debt and shares outstanding | SEC XBRL 10-Q/10-K, effective 40 days after period end | Quarterly | Equity-implied model |
| Facility terms and issue pricing (42 facilities, 108 dated observations) | Company releases, 8-Ks, rating releases, press; each row sourced | Event | Tier curves, facility inputs |
| Site signals (permits, dockets, FERC filings) | State agencies, FERC, press | Event | Completion and force majeure risk |

An N-PORT print may be used only once public: 60 days after its report date. Bond prices from market reports were used only for CoreWeave on 2026-10-14/15 (one derived, not a print). Vendor CDS levels are used only to grade, never as inputs.

## Bond-implied CDS

Same conversion math as vendors (ISDA CDS Standard Model) with traded bond prices instead of dealer quotes:

1. For each bond and date, solve a flat λ so survival-weighted cash flows with 40% recovery of par reproduce the clean price on that day's Treasury curve (handles pull-to-par; not a YTM or z-spread shortcut).
2. Interpolate λ across the issuer's bonds by maturity to the standard CDS maturity date (e.g. 2031-12-20 for an Oct 2026 trade, 5.18y). Extrapolate when all bonds sit on one side.
3. Convert with QuantLib's ISDA engine: standard quarterly schedule, Act/360 premium accrual, 40% recovery. Output the conventional spread and the upfront on a 500bp running coupon.

Locked rules: recovery 40% (moving to 25% shifts CoreWeave 2026-10-14 by 58bp); interpolate to the CDS maturity date, don't average bonds (12.3% vs 13.6% error); bond-only, not blended with equity (blending doubles error to 29%; equity alone misses 46%) — equity only updates between bond prints; Treasuries for discounting for now (SOFR would add ~30–35bp; needs a SOFR swap history source).

## Tier curves

29 issue pricings (May 2025 – Oct 2026). Spread over Treasuries = tier level + 33bp per 1 point of ICE BofA HY spread + Nelson–Siegel short-end term (τ = 3y), ridge-regularized.

| Tier | Level (bp at sample-mean HY spread 2.89%) | Deals |
| --- | --- | --- |
| IG project bond | 239 | 3 |
| Backstopped senior | 252 | 7 |
| Contract-backed loan | 295 | 8 |
| Construction loan | 311 | 3 |
| Junior chip paper | 462 | 2 |
| Neocloud bond | 505 | 6 |

Fit: residual SD 91bp; leave-one-out RMSE 111bp. Backstopped deals span 100bp (Anthropic A1, Broadcom guarantee) to 415bp (TeraWulf, Google backstop); contract-backed loans 160bp (IREN on a Microsoft contract) to 468bp (CoreWeave DDTL 5.5). Floating-rate deals convert to a Treasury-equivalent spread using SOFR minus the Treasury yield at that tenor on the issue date.

**Inputs when FRED is unavailable** (added 2026-10-21; code `services/marden/models/price_tiers.py`). The ICE BofA HY OAS comes from FRED (series `ice_bofa_us_hy_oas`) and is always used when it has a value on, or within a week before, the day. On a day it has none, the engine uses the fund OAS of USHY (iShares Broad USD High Yield Corporate Bond ETF, series `etf_oas_ushy`, collector `etf_oas`), which tracks the ICE BofA US High Yield Constrained Index. The model's inputs record it as a proxy with that method note. It is a proxy, not the index: it is the fund's own portfolio OAS, and the constrained index caps each issuer at 2%. FRED was down on 2026-10-21 (USHY 307.70bp as of 2026-10-20). The 33bp-per-point coefficient is defined against the HY spread's sample mean over the deals' issue dates, and the proxy history only starts in October 2026. So while any deal lacks an HY value at issue, the coefficient is not fitted. The tier levels are fitted without the market term and taken to sit at the stated sample mean, 2.89%. The curve then moves by 33bp × (HY today − 2.89). The fit output records this as `hyMode: "fixed"`; a fitted coefficient is `hyMode: "fitted"`. Once FRED serves the ICE history again, the coefficient and mean are fitted from it as above. Single-B, CCC and BBB have no ICE-tracking fund that publishes its OAS on a site whose terms and index fit, so those series stay missing.

## Jupiter backtest (locked assumptions)

- Floating SOFR+250 at par on 2025-11-07, 4-year term.
- Tenant: Oracle bond-implied intensity, each N-PORT print used only 60 days after its report date; daily equity-implied moves fused in (loading 0.018, estimated from monthly data).
- Lease protection before rent starts 0.5; completion target 2028-06-30; base completion-failure rate 1.5%/yr; recovery 55% (locked: `price_validate.JUPITER_RECOVERY`; price v0.2's site recovery does not apply to this path). Liquidity premium solved once so issue price = 100 (172bp), then held fixed.
- Signal rules from the public date: each agency denial of a critical-path power permit multiplies completion risk by 1.75 (cap 6×); each confirmed power delay ≥ 3 months adds 0.25 to the probability of force majeure delay, charged at 0.15/yr.

| Public date | Event | Marden price |
| --- | --- | --- |
| 2026-09-24 | Oracle force majeure notice reported | 89.0 |
| 2026-09-18 | FT reports loans quoted 89–91 | 94.1 |
| 2026-08-17 | FERC filings show pipeline in-service slips to Feb 2027 | 94.3 |
| 2026-07-16 | Right-of-way reconsideration denied | 96.7 |
| 2026-03-31 | State Land Office rejects pipeline right-of-way | 98.2 |
| 2025-11-07 | Issue, SOFR+250 | 100.0 |

Corrections applied: the 2026-04-27 turbine event was a voluntary switch to fuel cells, not an agency withdrawal (moves first sub-95 from 07-14 to 08-17); N-PORT prints now used 60 days after report date (Oracle input 125bp on 09-18 vs 149bp before). The early warning comes from the hand-set signal rules, not market inputs; a true test needs rules estimated on other sites, run forward.

## Limits and next steps

| Gap | Effect | Fix |
| --- | --- | --- |
| Month-end N-PORT prices vs mid-month CDS reports | Most of the remaining 12% error | Daily trade prints for 21873SAG3, 21873SAC2, 21873SAB4, 21873SAK4 |
| CDS-bond gap flips sign between calm and panic | ±15% on individual dates | Model the gap from market stress; needs CDS history |
| Treasury curve, not SOFR | ~+30–35bp | SOFR swap curve source |
| Oracle tenant leg stale after 2026-07-31 | Understates late stress | September N-PORT (late Nov) or daily bond prints |
| Signal rules (1.75, 0.25, 0.15) are judgment | Jupiter lead time not validated | Estimate across many site events |
| Tier curves rest on 29 deals; 111bp LOO error | Wide bands on untraded facilities | More issue pricing; guarantor and tenant credit as regressors |
| Bond trade data licensing | FINRA public data is noncommercial only | License TRACE data for the commercial product |

Outputs are labeled "Marden bond-implied CDS" and "Marden model price", with inputs and error bands shown — an independent valuation input, not a dealer quote.

References: Duffie & Singleton (1999); Jarrow & Turnbull (1995); Finger et al. (2002) CreditGrades; Getmansky, Lo & Makarov (2004); Longstaff, Mithal & Neis (2005).
