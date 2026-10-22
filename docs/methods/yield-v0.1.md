# Yield to maturity, yield to worst and discount margin v0.1

Written 2026-10-22. Code: `services/marden/models/yields.py`; read model `services/marden/readmodels/views/instrument_yields.py`. Model `yield`, version `0.1`, method label `yield v0.1`. Every yield is a Marden model value (tag M).

## Question

What does the debt yield at its price, to what date, and, for a floating-rate loan, what spread over SOFR does the price imply? Shown on Instrument detail (the YTW tile, with YTM beside it; the price chart's yield overlay) and on the Instruments list (YTW / YTM column).

## Inputs (point in time)

| Input | Source | Usable from |
| --- | --- | --- |
| Price | The Marden daily price (`price_obs` kind `marden`, tag M, one per day). Else the latest observed price (TRACE trade, exchange or dealer quote), licence switch applied. | Its date |
| Coupon, maturity, coupon type, margin | The price engine's terms (`price_terms`): instrument facts, else the linked issue comp. A maturity given to the month is the 15th, a bare year July 1. | The issue date (static terms) |
| Extension options | The coupon text, maturity text or research terms: "two 1-yr extensions", "two one-year extension options". | As above |
| Calls | Research terms: "Callable at par from 15 May 2042" (counted); "First call presumed about Aug 2028 at 104.94 (model assumption)" (shown, not counted); "make-whole ... before <date>" (noted, never counted). | As above |
| SOFR floor | Fact `sofr_floor_pct`, else "<x>% floor" in the term texts. | As above |
| SOFR | NY Fed fixing (`series` `sofr`, tag F) on or before the price date, within 7 days. | Its date |

## Cash flows

- Fixed: the coupon ÷ 2 on dates counted back from maturity, accrued 30/360 (as `price_bonds.flows`).
- Floating: the **current coupon held flat**: the latest SOFR fixing (floored if a floor is stated) plus the margin, every quarter, Act/360. This is the street convention for a loan's yield and discount margin. (The price engine itself projects SOFR along the Treasury forward curve; its `yieldPct` therefore differs from this model's YTM for floaters, and agrees for fixed coupons.)
- Principal: bullet at the redemption date, as in price v0.1. Where the research terms describe amortization the assumptions say the yield ignores it.
- Settlement on the price date; the price is clean and the accrued interest is added.

## Yields

- Yield: the internal rate of the flows against the dirty price, compounded twice a year (bond-equivalent) on the engine's Act/365.25 time axis, so fixed and floating yields compare.
- YTM: redeemed at par at the stated maturity.
- YTW: the lowest yield over every date the issuer or borrower may choose: the stated maturity; each extended maturity (the borrower's option; extension fees are not in the sources and are left out); the first date of a stated call (snapped to the next interest date), at its call price; and for a bank loan (construction loan, term loan, DDTL) prepayment at par on any interest date (assumed: the credit agreements' prepayment terms are not public). A presumed call is worked and shown but not counted. With no call or extension in the sources, YTW = YTM and the assumptions say so.
- Discount margin (floating): the spread `dm` with Σ CF_i × Π_j 1 / (1 + (SOFR + dm) × τ_j) = dirty price, τ Act/360, the first period from the settle day. At par on a coupon date it equals the margin.

## Not worked (n/d, with the reason in plain words)

Convertible notes (the equity option is not modelled), securitizations (ABS, CMBS: principal pays down early on a schedule the sources do not give), non-USD notes, perpetuals (maturity recorded as 3999-12-31), instruments without a coupon or maturity in the sources, and instruments with no Marden or observed price yet.

## Output

One `model_output` per instrument (model `yield`): `ytw`, `ytm`, `dmBp` (Sourced, tag M), `ytwTo` / `ytmTo` (the workout), `workouts` (every date worked; of the prepayment dates only the lowest-yielding), `basis` (the price used), `points` (one per Marden daily price: price, ytm, ytw, ytwTo, dmBp), `observed` (the yields at the latest observed price and the yield the source itself reports), `assumptions`, `inputs` (each sourced), `why`, `missing`.

## Check

On Oct 22, 2026, across 108 bonds with a TRACE print that states a yield, the median gap between this model's YTM of the print's price and FINRA's own yield is 0.4bp. The larger gaps are bonds priced to a call whose schedule is not in the sources, or amortizing bonds (bullet assumption).

## Runs

Daily 09:44 UTC (after the 09:40 price run) and within a minute of a new price output, a price observation, an instrument's terms or a SOFR fixing (`models.run_dirty`). `python -m marden model yield [--as-of D] [--instrument ID ...] [--json]`.
