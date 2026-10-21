# 25 · Off-balance-sheet lease map and downgrade ranking (`/leases`)

**Job:**
1. Account for every dollar of signed-but-not-yet-commenced data center leases and compute commitments.
2. Tie each dollar to a lessee, a landlord, a site and the debt that depends on it.
3. Rank the leases by how likely they are to push the lessee into a downgrade.

**Scale:** Moody's counts $662B of future data center lease commitments across Amazon, Meta, Alphabet, Microsoft and Oracle alone (Feb 2026), with nearly $1T across all lessees. Marden's total comes from the filings themselves, not from that estimate.

## What "100% mapped" means

Filings disclose leases not yet commenced mostly as a company total with a range of terms, not lease by lease. So every lessee gets a reconciliation that must tie to the dollar:

> **disclosed in the latest 10-K/10-Q = filed or reported leases + inferred leases + not yet attributed**

- **Attributed:** a lease Marden can name (landlord, site, amount or share, start, term), from a filing (F), a landlord or developer filing, ABS/CMBS documents, county records, permits or an 8-K, or press (R).
- **Not yet attributed:** the residual, shown as a dollar amount with the filing it comes from. It is never hidden and never spread across sites by guesswork.
- The attributed share is a headline metric per lessee and for the whole map, and it rises as the researcher agent finds each lease.
- **Data gate:** the sums must tie exactly, or the build fails.

## Backing into the deals (shrinking "not yet attributed")

Big tech companies disclose totals, but every lease leaves a trail elsewhere. The researcher agent builds each lease from that trail.

**1. Find the site and the tenant behind the project LLC.** The project companies on these records are trailed back to the tenant through Secretary of State records (registered agent, officers, addresses), shared counsel and the entity linker:
- air permit applicants
- building permits and occupancy certificates
- county tax abatement agreements and agenda items
- utility large-load and interconnection agreements
- deeds and mortgages
- landlord and developer filings, and ABS/CMBS loan-level data (tenant, term, rent)

**2. Size it.** In order of preference:
- the filed rent and term
- the ABS/CMBS loan-level rent
- contracted MW × market rent per kW-month × term

Market rent comes from landlord REIT filings and other disclosed comparable leases, by region and year. MW comes from permits, utility filings and the building footprint (specs 14 and 17).

**3. Date it.** Commencement comes from construction progress and energization evidence (spec 17), checked against the company's disclosed commencement range.

**4. Match it to the filing,** the strongest check. Each quarter, the change in the lessee's disclosed not-commenced total is matched against the leases the trail shows were signed in that quarter. For example, a jump in Meta's total in the quarter it signed a campus lease is attributed to that lease. Leases that commence move out of not-commenced and into lease liabilities, and that move is matched too.

**5. Hold it to the total.** Inferred leases can never sum to more than the disclosed total. If they would, the sizes are scaled back to fit and the conflict goes to the review queue.

**Tagging:**
- Inferred leases are tagged **M**, shown as "Inferred", with each step's evidence and a confidence (High / Medium / Low).
- A lease moves to **R** or **F** when press or a filing confirms it.
- The reconciliation has three parts that must add up to the filed total: **filed + inferred + not yet attributed**.

Inferred leases count fully in the downgrade ranking, with their confidence shown, because the agencies count the dollars whether or not the lease is named.

## Sources

| Data | Source | Tag |
| --- | --- | --- |
| Leases not yet commenced (total, terms, commencement years), operating vs finance leases, maturity schedules | Lessee 10-K/10-Q lease notes: Microsoft, Meta, Alphabet, Amazon, Oracle, CoreWeave, Nebius, IREN and others; Anthropic S-1 once public | F |
| Purchase and compute commitments that behave like leases | Lessee commitment notes; counterparty filings (spec 18) | F |
| Leases from the landlord side: signed-not-commenced backlog by tenant, build-to-suit | Landlord and developer filings (Digital Realty, Equinix, Iron Mountain and other public owners), ABS/CMBS offering docs and servicer reports for private developers (Vantage, Stack, Aligned, QTS, etc.), JV disclosures (e.g. Meta–Blue Owl Hyperion) | F |
| Site, campus and buildings | Marden site and data center maps (specs 14, 16, 17): permits, county records, satellite imagery | F or M |
| Dependent debt and holders | Debt chains, N-PORT and BDC filings | F |
| Project LLC → tenant links | Secretary of State business entity records, registered agents, officers; county tax abatement agreements; utility large-load agreements | F |
| Market rent per kW-month by region and year | Landlord REIT filings and disclosed comparable leases | F inputs, M output |

## Ranking: which leases lead to downgrades

For each lessee, use the spec 24 machinery:
- the agency-adjusted leverage path
- each agency's published downgrade triggers
- headroom to those triggers

The criteria follow each agency's published corporate methodology: S&P's corporate criteria (business risk × financial risk, with debt/EBITDA and FFO/debt bands), Moody's sector scorecards, and Fitch's corporate criteria. Agents quote the current version of each document and the bands they use. Never use bands from memory.

For each lease:

| Measure | Meaning |
| --- | --- |
| Turns added | Lease liability the agency will add at commencement ÷ lessee EBITDA (agency method) |
| Year it lands | Commencement year |
| Headroom left at that point | Trigger minus forecast leverage just before this lease commences |
| **Trigger lease** | This lease's commencement takes forecast leverage past an agency trigger, i.e. it's the lease that tips the rating |
| Lessee downgrade band | Low / Watch / High (spec 24) |
| Debt exposed | Instruments whose repayment depends on this lease (landlord SPV bonds, construction loans, ABS, GPU facilities), with $ and holders |
| **Priority score** | Ranks by: probability the lessee is cut × dollars of dependent debt × whether the dependent debt has rating-linked terms × proximity of the commencement date. Labeled M v0, with the weights shown. |

The default sort is priority score, so the top of the page shows the leases most likely to cause a downgrade and the debt and holders that would reprice with them.

## Screens

- **Lease map (`/leases`):**
  - KPI strip: total disclosed, attributed, not yet attributed, attributed share, trigger leases, debt exposed
  - reconciliation table, one row per lessee
  - ranked lease table: lessee, landlord, site, $, start, term, turns added, headroom, trigger flag, band, debt exposed, holders, evidence
  - "unattributed" rows link to the filing passage
  - Excel export
- **Payer page (spec 08/24):** the payer's leases, ordered by commencement, with the leverage path showing each trigger lease as a step.
- **Site detail and Data center pages:** the leases on that site and their lessee's band.
- **Alerts:**
  - a lease becomes a trigger lease
  - a new filing changes a lessee's disclosed total by more than 10%
  - a lease commences
  - a residual is attributed

## Acceptance

- Each lessee's reconciliation ties to its latest filing to the dollar.
- Every attributed lease opens its source passage.
- Ranking inputs show on hover with the agency criteria document cited.
- Backtest: the leases that commenced before S&P's July 2026 Oracle cut rank as trigger leases on data available at the time.
