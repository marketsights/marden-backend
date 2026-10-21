# 19 · GPU collateral (`/gpus`, `/gpus/:model`)

**Job:** what the GPUs behind AI loans are worth today and as they age, so every GPU-backed facility shows its loan-to-value and a recovery value if the customer walks. Built from public prices and filings only; no licensed index.

## Data and where it comes from

| Data | Source | How pulled | Tag |
| --- | --- | --- | --- |
| Rental prices by GPU model (H100, H200, B200, GB200, GB300…), on-demand and reserved, $/GPU-hour | Public price pages of clouds and neoclouds (AWS, Azure, Google Cloud, Oracle, CoreWeave, Lambda, Nebius, Crusoe and others) and public GPU marketplaces | Daily collectors; one row per provider, model, term and region | F (published price) |
| Contracted prices and terms | Neocloud 10-K/10-Q and 8-K contract disclosures; facility and ABS offering documents | Extraction agent | F |
| Useful life and depreciation policy | 10-K accounting notes (e.g. server and GPU useful lives at CoreWeave, Microsoft, Amazon, Alphabet, Meta, Oracle) | Extraction agent | F |
| Resale prices for used GPUs and servers | Public secondary-market listings and auctions; reseller price lists | Collectors; outliers removed in code | R |
| New-unit prices and launch dates | Nvidia and AMD filings and announcements; OEM server list prices where published | Extraction agent | F or R |
| Units per facility and collateral pool | Credit agreement exhibits and 8-Ks for each delayed-draw or GPU facility; ABS collateral descriptions; press for unit counts (e.g. GB300 deliveries) | Extraction agent | F or R |
| Facility terms: advance rate, LTV covenant, amortization, balance | Same documents; 10-Q debt notes for balances | Extraction agent | F |
| Contract that services the loan (customer, term, minimum payments) | Obligation map edges (spec 18) | Link | F or R |

## The model (v0, labeled until checked against resale data)

1. **Price index per model:** median of comparable rental prices each day (same model, term bucket, region), with the spread shown. Published as the Marden GPU price index, `/gpus`.
2. **Value per unit today:** present value of expected rental income over remaining useful life: index price × expected utilization × hours, declining as newer generations ship (decline rate fitted from older models' price paths, e.g. A100 and H100 history), discounted at the tier rate from the credit curve. Cross-checked against resale listings.
3. **Collateral value per facility:** units × value per unit, minus a liquidation haircut (time to resell, removal and transport).
4. **Loan-to-value:** facility balance ÷ collateral value, today and projected by quarter to maturity.
5. **Recovery if the customer walks:** collateral value at the expected default date ÷ claims at that level, after senior claims.

## Delivered vs energized (idle GPU collateral)

**Question it answers:** how many of the GPUs a loan financed are actually installed, powered and earning, and what does each month idle cost the lender? GPUs bought ahead of power and buildings sit in warehouses or dark halls. The borrower pays interest and the collateral depreciates while nothing earns. No other credit-data provider shows this per facility.

| Measure | How it's built | Tag |
| --- | --- | --- |
| Units financed | Credit agreement collateral schedules, ABS collateral descriptions, 8-Ks | F |
| Units delivered, with dates | 8-Ks, earnings calls and 10-Qs ("received", "accepted delivery", "placed in service"); customer and OEM announcements; press on rack deliveries (e.g. Amazon taking ownership of racks before they reach a data center) | F or R |
| Energized IT capacity at the sites those GPUs are destined for (MW) | Spec 17 evidence: utility energization and large-load reports (ERCOT, PJM), EIA-860M on-site generation online, air permit status, certificates of occupancy, Sentinel-2 change | F or M |
| Power per unit | OEM rack specifications (e.g. per-rack kW for GB300 NVL72) × PUE assumption | F/R input, M output |
| Units energized | min(delivered, energized IT MW ÷ power per unit) | M v0 |
| Idle units and idle share | delivered − energized; ÷ delivered | M v0 |
| Months idle | Delivery date to energization, actual or the cushion model's P50 for the site | M v0 |
| Interest on idle share | Facility balance × idle share × coupon × months idle ÷ 12 | M v0 |
| Value lost while idle | GPU value curve (above) over the idle months | M v0 |
| LTV today vs LTV if energized on schedule | Collateral model with and without the idle period | M v0 |
| Company level: construction in progress vs PP&E in service, quarterly | 10-Q/10-K balance sheet and PP&E notes (CoreWeave, Nebius, IREN, Oracle, Microsoft, Meta, Amazon, Alphabet, others as they disclose) | F |

**Status per facility:** Earning (idle share under 10%), Partly idle (10–50%), Idle (over 50%), or n/d. Facilities where neither deliveries nor energization are disclosed show n/d with what is missing. Never guess.

**Where it shows:**
- GPU index page: a "Delivered vs energized" section with a table by facility (financed, delivered, energized, idle share, months idle, interest on idle share, status) and a chart of construction in progress vs PP&E in service by company, quarterly.
- Instrument detail Collateral card: delivered / energized / idle units, months idle, interest on idle share, LTV today vs if on schedule.
- Risk list: idle share above 30% for 3+ months raises the rank. Alerts: a watched facility changes status. Stress: the power-delay scenario reprices idle cost and LTV.

**Acceptance:** every delivered and energized figure opens its source passage; all derived values are labeled M v0 with assumptions (kW per unit, PUE, coupon) on hover; company construction-in-progress figures tie to the filed balance sheet.

## Screens

- **GPU index (`/gpus`):** chart of rental price by model over time (on-demand and 1-year reserved), table of today's prices by provider with source links, value-per-unit curve by age for each model.
- **Model page (`/gpus/:model`):** price history, provider table, resale listings, depreciation assumptions with their filing sources, facilities secured by this model.
- **Instrument detail (spec 07):** for GPU-backed facilities, a "Collateral" card: units, model, value today, LTV today and at maturity, recovery if the customer walks, all tagged M v0 with assumptions on hover.
- **Recovery values everywhere (Site detail, Stress, Risk list)** read from this model for GPU facilities.

## Acceptance

- Every index point traces to the captured price pages behind it (raw page saved daily in the archive).
- LTV and recovery always show their assumptions (utilization, decline rate, haircut) and model version.
- Values switch from "M v0" to calibrated only after back-testing against at least 50 observed resale prices per model.
