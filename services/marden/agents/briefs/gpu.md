## Why it matters

GPU-backed loans are repaid from compute contracts and secured by the GPUs themselves, so lenders track how
many GPUs of which model are deployed and financed, how much power is energized ("active") versus only
contracted, the backlog that will pay for it, who the customers are, and what a GPU-hour rents for. The
gap between contracted and active MW is the delivery risk; the rental rate and the GPU generation drive
collateral value.

## Where the facts are

- Earnings releases and investor presentations: active power, contracted power, backlog / RPO, named
  customers, GPU generations deployed.
- 10-Q / 10-K: remaining performance obligations, customer concentration, GPUs deployed, depreciation lives.
- 8-Ks and credit agreements for GPU financings: GPU model and count financed, customer contracts.
- Delivery announcements: GPUs delivered to a named campus.

## How to record

- Company-wide figures (active_it_mw, contracted_mw, backlog_rpo_usd, top_customers) are on the entity
  named in the document, with as_of the period end the document gives ("as of June 30, 2026").
- MW in MW: 1.5 GW is 1500. backlog in full USD: "$104 billion" is 104000000000.
- gpu_count and gpu_model on the campus, site or instrument the document ties them to.
- top_customers is a list of the customers the document names as customers (not partners or vendors).

## Traps

- "Nearly", "approximately", "more than" values are still the stated value; say so in `note` and keep
  confidence below 0.9 for "more than".
- An increase ("expanded active power by nearly 500 MWs") is not the level; the level is "to reach 1.5 GW".
- Footnoted figures that exclude something ("does not include more than $25 billion of net new customer
  commitments") keep the stated figure; put the exclusion in `note`.
- Partners and suppliers (NVIDIA, Dell) are not customers unless the document says so.

## Examples from real filings

CoreWeave second-quarter 2026 earnings release (EX-99.1), stored document:
- quote "Expanded active power by nearly 500 MWs to reach 1.5 GW" -> active_it_mw 1500 on entity
  "CoreWeave, Inc.", as_of 2026-06-30.
- quote "Grew total contracted power to approximately 3.7 GW" -> contracted_mw 3700 on the same entity.
- quote "Revenue backlog1 was approximately $104 billion as of June 30, 2026." -> backlog_rpo_usd
  104000000000, as_of 2026-06-30, note "excludes more than $25 billion of commitments added in early Q3".
