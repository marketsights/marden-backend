# Recovery v0.1 (model v0)

Written 2026-10-21. Code: `services/marden/models/recovery.py`, `recovery_shell.py`, and `gpu_facilities.py` for GPU-backed loans. Model `recovery`, version `0.1`.

## Question

What lenders get, in cents per dollar, if the tenant or customer walks (spec 22 "Recovery"; spec 04 "Recovery inputs"; spec 07 KPI "Recovery if default").

## Output

One output per instrument:

- `recovery`: Sourced<string>, e.g. '62–74' (cents per dollar), or one number when low and high round alike.
- `valueB`: collateral value, base case, $B.
- `method`: `gpu_collateral`, `powered_shell` or null.
- `missing`, `basis`.

## Collateral waterfall

1. **GPU-backed facilities.** The GPU collateral model (`gpu-collateral-v0.1.md`): units × value per unit × (1 − 20% haircut), over the pool's balance. The range covers a customer walking from today to a year out.
2. **Site-backed debt (powered shell).**
   - **Comps.** Every site with a filed or reported total project cost and gross MW gives a value per gross MW. This is replacement cost, the only value evidence on file. The archive's model costs (tag M) are not comps, and neither is a GPU-capex cost basis. At least 3 comps are needed. Low, base and high use the 25th, 50th and 75th percentile. On 2026-10-21: 6 comps, $11.6m / $12.1m / $12.8m per MW.
   - **Site.** Gross MW (else critical IT MW × the midpoint of the published PUE range, flagged in the basis) × completion.
     - Completion is `pct_complete`, else delivered MW ÷ critical IT MW (gross if delivered exceeds IT).
     - With nothing delivered and no stated completion, the value of unfinished works is n/d. It is never assumed.
   - **Claims.** The site's other instruments (excluding GPU facilities) at their filed drawn amount, else their amount, ranked by `seniority`: secured 1, junior 2, unsecured 3. Guarantees and leases are not claims on the buildings.
   - **Recovery** = (value − claims ahead) ÷ claims at the instrument's rank, between 0 and 100c.
3. **Corporate unsecured notes and converts.** n/d, because the issuer balance-sheet waterfall is not modelled in v0.

## Not in v0 (listed in each output's basis)

- Re-letting time and carry.
- Unspent proceeds held in project accounts (funded construction bonds).
- Third-party support: Meta residual value guarantees, Google backstops, SpaceX lease guarantee.

## Runs

- Weekly, Monday 09:41 UTC (spec 22 "Weekly").
- Within a minute of an input changing: instrument terms, site capacity, cost or completion, any site's cost comp, GPU market inputs, or the `gpu_collateral` output.
- A run for today marks `@model:recovery` on the instrument.
