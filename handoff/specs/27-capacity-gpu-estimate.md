# 27 · Capacity and GPU estimate (per building and campus)

**Job:** estimate each building's and campus's facility MW, IT MW and number of GPUs from the physical equipment on public permits, using several independent methods. Show the range and confidence, and replace the estimate with a filed figure the moment one exists.

**Used by:**
- Data centers (spec 14) and site detail
- delivered vs energized (spec 19: the installed-capacity ceiling)
- lease sizing (spec 25: MW × rent)
- cost per MW and the funding gap (spec 26)
- GPU collateral value per site (spec 19)

## Step 1 · Facility MW, triangulated

Every method gives a low–high range with its evidence. Methods are listed from strongest to weakest:

| Method | Inputs | Notes |
| --- | --- | --- |
| Filed figure | Developer, tenant, utility or landlord filing stating facility or IT MW | Tag F; when present it is the answer and the others become checks |
| Utility load | Large-load request or agreement MW; substation and transformer MVA in utility or commission filings | Strong; dates come with it |
| Prime on-site generation | Turbines and engines by count × rating, minus units the permit marks as spare (e.g. VoltaGrid Frontier: 197 primary, 13 backup) | Spec 15 data |
| Backup generators | Count × standby rating from the air permit, less redundant units. The redundancy design comes from permit language ("maximum units operating simultaneously", annual hour limits, block or distributed redundant) where stated. Otherwise the range runs from 2N (low) to N+1 (high). | The most common source; the redundancy assumption drives the range |
| Cooling | Chiller and cooling-tower capacity on air or building permits (tons → kW of heat) | Cross-check on IT heat load |
| Footprint | Building area from footprints and permits × the power density of the same developer's disclosed designs | Weakest; fills gaps only |

**Combining:** use the filed figure if there is one. Otherwise take the overlap of the stronger methods. If the methods disagree by more than 25%, flag it to the review queue with each method's figure.

## Step 2 · IT MW

**IT MW = facility MW ÷ PUE**, where PUE (power usage effectiveness) is the ratio of total facility power to IT power.

- PUE assumption comes from cooling type (liquid vs air, from permits and equipment) and climate, using the developer's or tenant's disclosed PUE when published.
- The assumption is shown, never buried.

## Step 3 · GPUs

**GPUs = IT MW × share of IT load used by GPU racks ÷ kW per GPU at rack level**

- **kW per GPU by generation:** from OEM rack specifications (e.g. per-rack power for GB200 and GB300 NVL72), quoted with source. Never from memory.
- **Generation:** from contract and delivery disclosures, the tenant's announced chips, or the energization date. Unknown generation gives one estimate per likely generation.
- **GPU share of IT load** (vs networking, storage and CPU): an assumption, shown, calibrated by the backtest.
- **Cross-checks:**
  - disclosed GPU counts for the site or tenant (8-Ks, earnings calls, press)
  - GPUs financed in the facility's collateral (spec 19)
  - implied GPU spend (GPUs × unit value) vs the size of the GPU financing

## Output per building and campus

Each of the following has a low / base / high range, a confidence, every method's figure with its evidence, and a "replaced by filing" marker when a filed figure arrives:
- facility MW
- IT MW
- GPU count
- implied GPU value

## Calibration (before "v0" comes off)

- Backtest on every site where a credible IT MW or GPU count has been disclosed or reported.
- Track error by method and tune the redundancy, PUE and GPU-share assumptions.
- Publish the error band on the Track record page.

## Acceptance

- Every V1 building has a facility MW and IT MW range with at least two methods, or says which inputs are missing.
- Every GPU estimate shows its generation assumption and kW per GPU source.
- Generator sums are computed in code from unit rows (spec 15), never typed.
