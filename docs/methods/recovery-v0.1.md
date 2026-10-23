# Recovery v0.1 (model v0)

Written 2026-10-21; v0.2 on 2026-10-22 ("v0.2" below). Code: `services/marden/models/recovery.py`, `recovery_shell.py`, `recovery_progress.py` (v0.2), and `gpu_facilities.py` for GPU-backed loans. Model `recovery`, version `0.2`. Since price v0.2 the price model prices each facility with this model's central estimate (`basis.baseCents`; GPUs: `basis.pathCents`), so it is the recovery behind the Marden price wherever it gives a number.

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
     - With nothing delivered and no stated completion, v0.1 left the value of unfinished works n/d; v0.2 estimates the share from capex, schedule and imagery (below), never a flat assumption.
   - **Claims.** The site's other instruments (excluding GPU facilities) at their filed drawn amount, else their amount, ranked by `seniority`: secured 1, junior 2, unsecured 3. Guarantees and leases are not claims on the buildings.
   - **Recovery** = (value − claims ahead) ÷ claims at the instrument's rank, between 0 and 100c.
3. **Corporate unsecured notes and converts.** n/d in v0.1 and v0.2; since v0.3 the issuer balance-sheet waterfall (below).

## v0.2: an estimated share finished, unfinished works and unspent proceeds (2026-10-22)

Owner feedback: a site under construction must never show n/d next to a flat 55c. v0.2 estimates what v0.1 left n/d.

- **Share finished when no source states it** (`recovery_progress.py`; tag M; every estimator in `basis.completionEstimators`, shown on hover). A stated `pct_complete` or delivered MW still wins. Otherwise:
  1. **Capex spent vs budget**, when a filed or reported `cost_to_complete_usd` and `total_project_cost_usd` are on file: 1 − cost to complete ÷ budget. It stands alone.
  2. Else the mean of:
     - **Schedule:** months elapsed since construction started over the months from start to the campus's planned completion. Start: the earliest building `construction_start`, else a start month stated in the site's records (`risk_construction`: "Construction began ~Sep/Q4 2025"), else the financial close (issue date of the site's `debt_facility` comp). End: the last date of the campus `capacity_plan`, else the cushion P50 rent start.
     - **Imagery:** the roofed share of the planned footprint (campus `imagery_note.roof_ha` over `sqft_total`; else Sentinel-2 `imagery_stage` of the planned buildings, roofed 1, under construction ½) × ⅓, the shell's share of a data center's cost (the fit-out is not visible from orbit).
  3. **Energization:** with no MW delivered, at most 90%.
- **Value if the tenant walks** = gross MW × comp value per MW × (energized share + unfinished share × (1 − 25%)) + unspent proceeds. The 25% haircut on works not yet energized (a buyer must finish, power and re-let them) is judgment, like the GPU model's 20% resale haircut.
- **Unspent proceeds.** A project bond (`kind` bond, sized at its amount) is funded at close and spent pro rata with the works: its amount × (1 − share finished) is still in the project accounts. Counted only when the share is a stated or estimated share of the works (not delivered MW, which can trail the works). Construction loans draw as they build, so none.
- **Not counted (named in the basis):** land (no land values on file), equipment on site (no invoices or liens on file), re-letting time and carry, third-party support (residual value guarantees, backstops).
- Every parameter (⅓, 90%, 25%) is uncalibrated ('model v0').

On 2026-10-22: Jupiter (EX-JUPITER-190) 41–45c, base 43c (share finished 34%, schedule only: Sep 2025 → Dec 2028; no footprint on file for the imagery estimate; $18B of claims at its rank). The Barn (EX-SALINE-308) 96–99c, base 97c (share 26%: schedule 18% from the Apr 2026 close, imagery 33%; $10.4B of the $14B notes still unspent).

## v0.3: the issuer balance-sheet waterfall (2026-10-22)

Corporate notes, converts, unsecured loans and parent-level secured facilities priced at a flat 40c. v0.3 gives
each one its rank's recovery in its issuer's balance sheet (`recovery_issuer.py`; one output per issuer entity,
one per instrument, method `issuer_waterfall`).

- **Issuer.** The top of the issuer's parent chain; a holding company without a CIK takes the filer with its ticker
  (Cipher Digital takes Cipher Digital Inc.'s filings).
- **Inputs, point in time (tag F, each linked to its filing).** The latest 10-Q / 10-K public on the day (XBRL,
  collector `sec_xbrl`, which now also collects total assets and liabilities, equity, receivables, restricted
  cash, right-of-use assets, goodwill, intangibles and tagged secured / unsecured / convertible debt): total assets,
  cash, restricted cash, receivables, PP&E net, total liabilities (else liabilities and equity less equity), lease
  liabilities, balance-sheet debt. The debt stack is the instrument table (drawn, else amount; seniority; collateral
  and ranking text), left out before its issue date; leases signed but not commenced from the lease curation. A
  balance sheet without total assets, cash, PP&E or total liabilities is not run.
- **Assets after haircuts (tag M).** Cash and restricted cash 100%; receivables 80%; PP&E split into GPUs and
  servers (the stated share, else by role: GPU cloud 80%, GPU owner with its own data centers 60%, cloud
  provider 50%, landlord 0%) at resale over depreciated cost (the issuer's own `gpu_collateral` pools: value per
  GPU less the 20% resale haircut over new price × life left; else the GPU model's value at 1.5 years old), and
  buildings, power and works at the site model's works value of the issuer's own sites (at most their cost, else
  the works value stands for the cost) and 55% beyond them; right-of-use assets, goodwill and intangibles 0%; other
  assets 25%. Debt raised after the balance-sheet date adds its proceeds at the PP&E blend.
- **Claims by priority.** (1) SPV / opco secured debt (DDTLs, site notes) on its own collateral, ahead of the
  holding company: each pool takes the lesser of its claims and its collateral value (gpu_collateral or
  powered-shell value; no collateral model: taken as covered); a shortfall is a parent claim only when the parent
  guarantees it. (2) Parent secured debt and finance leases. (3) Unsecured, pro rata: notes, converts, unsecured
  loans, balance-sheet debt not itemised, lease rejection claims capped as 11 U.S.C. 502(b)(6) caps them (rent for
  the greater of one year and 15% of the remaining term, at most three; commenced leases: rent = current portion +
  7% of the liability, term from the liability as its present value; not commenced: commitment ÷ stated term),
  guaranteed shortfalls, other liabilities. (4) Subordinated debt from what is left.
- **Recovery** = value left for the rank ÷ claims at the rank, clamped to 5–95c; the range re-runs it with every
  non-cash asset 20% lower and higher.
- **No filings.** An issuer without an SEC balance sheet on file keeps the 40c senior unsecured convention, stated
  with why (private, or a filer not linked).

The price model (price v0.3) prices issuer bonds at the issuer's unsecured recovery and solves the issuer's
bond-implied curve from its bonds at that same recovery (pricing-v0.1.md "v0.3").

On 2026-10-22: CoreWeave unsecured 48c (39–61c; $40.8B of assets after haircuts, $14.6B secured ahead, $54.2B of
unsecured claims), Oracle 49c (42–57c), Applied Digital 95c (its site notes are covered by their sites; the parent's
claims are $1.4B against $3.9B left).

## Not in v0 (listed in each output's basis)

- Land and equipment on site (no values on file).
- Re-letting time and carry.
- Third-party support: Meta residual value guarantees, Google backstops, SpaceX lease guarantee.

## Runs

- Weekly, Monday 09:41 UTC (spec 22 "Weekly").
- Within a minute of an input changing: instrument terms, site capacity, cost or completion, any site's cost comp, GPU market inputs, the `gpu_collateral` output, or an issuer's XBRL balance sheet, lease commitments or role.
- A run for today marks `@model:recovery` on the instrument.
