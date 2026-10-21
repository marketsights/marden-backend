# GPU index, GPU value, collateral and deployment v0.1 (model v0)

Written 2026-10-21. This page covers spec 19. The assumptions below are locked for v0.1: changing one makes a new version.

Code, in `services/marden/models/`:

- `gpu_inputs.py`: inputs
- `gpu_value.py`: index and value per unit
- `gpu_facilities.py`: facilities
- one module per model: `gpu_index.py` (`gpu_index` and `gpu_model`), `gpu_collateral.py`, `gpu_deployment.py`

Method labels are `<model> v0.1`. Values carry `(model v0)` in `methodVersion` until they are back-tested against at least 50 resale prices per model (spec 19 Acceptance).

## Inputs (point in time, `marden.models.pit`)

- **Rental prices:** series `gpu_list_price:<provider>:<model>`, `usd_per_gpu_hr` + `label`.
  - Each provider's public price page, by capture day.
  - Every captured price is an on-demand list price with no region stated.
  - Silicon Data index points are a licensed product and are not loaded (`seed/cases_gpu.py`).
- **Chips:** series `gpu_model:<key>`.
  - `first_volume_ship`: 'YYYY-MM' is read as the 15th; 'YYYY-Qn' as the 15th of the quarter's middle month; 'YYYY' as Jul 1.
  - `new_price_usd {low, high}`: the midpoint is used.
  - `tdp_w`.
- **Resale and residual bands:** `gpu_resale:<key>` and `gpu_residual:<key>`, used as cross-checks only.
- **Useful life:** entity `gpu_useful_life_years`. A company's filed policy beats the reported schedule. Range text ('5-6') is not a policy.
- **Discount rate:**
  - The UST par curve (`ust_par_*`), interpolated at the remaining life.
  - Plus the median `issue_spread_bp` of `issue_comp` deals whose `deal_terms.type` is 'GPU loan', as the GPU tier rate.
- **Facilities:** instruments with `gpu_model` or `gpu_collateral` facts.
  - The record's base units, purchase month and amortization path (the archive's estimate, tag M, sourced to the credit agreement).
  - `drawn_usd` (F), `coupon_rate_pct`.
- **PUE:** midpoint of `data_center_pue.pue_range`.
- **Company CIP:** `construction_in_progress_usd`, `ppe_net_usd` (XBRL), usable 40 days after the period end.

## Method

1. **Index.** For each capture day, model, term ('on-demand') and region ('all'): the median, 25th and 75th percentile, and the number of providers (GpuPricePoint).
2. **Decline.** Spec 19 fits the decline from older models' price paths. Marden has no rental history yet, so v0 fits it across models on the newest capture day:
   - ln(median) = c + b × years since first volume shipment, by least squares over every model with a price and a launch date.
   - Annual decline = 1 − e^b. On 2026-10-17 this gives 22.1% (R² 0.90, A100 / H100 / H200 / B200 / GB200).
3. **Value per unit.** V = u × 8760 × P × e^{b s} × A(T), with A(T) = (1 − e^{−(ρ − b)T}) ÷ (ρ − b) and ρ = ln(1 + r).
   - P is the model's index price, rolled from the capture day at b.
   - A model with no captured price takes the launch rent its new price implies. Failing that, it takes the fitted line.
   - s is the years ahead. T is the remaining useful life.
4. **Utilization u is implied, not assumed.** It is the share of on-demand list rent the owner realizes: utilization × contract discounts, net of operating costs.
   - A unit new at launch is worth its published new price N.
   - For each model with an index price: u_m = N ÷ (8760 × P e^{−b·age} × A(L)).
   - u = the median u_m. On 2026-10-21 this is 0.158, with a median life L of 5 years.
5. **Collateral.**
   - Value = units × V × (1 − haircut).
   - LTV = the pool's balance ÷ value, today and at each scheduled quarter (the record's amortization path, scaled to the balance used).
   - A delayed-draw facility is sized in its record as if fully drawn. While the filed drawn balance is below that, the units financed are scaled by drawn ÷ record balance.
   - Facilities with one collateral record are one pari passu pool (IREN's DDTL and USPP notes).
   - A pool naming several models ('H100/H200') is valued at its lowest-valued model.
6. **Recovery if the customer walks** = collateral value ÷ pool balance, capped at 100c, for a default from today to four quarters out. No claims are known ahead of these first-lien SPV facilities. The output is the range text, e.g. '62–73'.
7. **Delivered vs energized.**
   - units energized = min(delivered, energized IT MW ÷ (kW per unit × PUE)).
   - Idle share, months idle and interest on the idle share follow (balance × idle share × coupon × months ÷ 12).
   - Status: Earning < 10% idle, Partly idle 10–50%, Idle > 50%.
   - Marden has no delivery counts and no OEM rack kW per unit yet, so every facility is n/d and lists what is missing. Site energized MW appears as evidence.

## Locked assumptions

| Assumption | Value | Why |
| --- | --- | --- |
| Liquidation haircut | 20% | Time to resell, de-rack, transport (spec 19 step 3) |
| Term / region of the index | on-demand / all | The only prices captured |
| Useful life | Borrower's (or parent's) filed policy, else the median policy | Spec 19 inputs |
| Default window for recovery | today to +4 quarters | Spec 19 step 5 needs an expected default date; none is modelled in v0 |
| Operating costs | Not deducted separately (absorbed in the implied u) | |

## Validation (2026-10-21 run)

- **H100 resale cross-check.** Model $6,254 for a unit bought at launch, valued on 2026-08-15, against $14,250 typical realized resale (American Compute, 2026-08-15). The ratio is 0.44: v0 is conservative against the resale market.
- **Residual bands (American Compute, tag M).** H100 2026: 33–46% of new. Model at age 4: 13% of new.
- **Archive LTVs (`gpu_collateral` facts, legacy).** CoreWeave DDTL 1.0: archive 232%, model 326%. DDTL 2.0: archive 174%, model 351%.

The gap comes from the cross-sectional decline (22%/yr) and the 5–6-year lives cutting value to zero at end of life. Resale markets price a longer economic life. This is why the values stay labelled model v0.
