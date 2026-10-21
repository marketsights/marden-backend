# 20 · Power plant debt (additions to On-site power, spec 15)

**Job:** the debt and contracts behind each on-site plant: who financed the turbines and engines, on what terms, who buys the power, and who loses if the data center it serves slips.

## New fields on each plant

| Field | Source | Notes |
| --- | --- | --- |
| Owner of the plant (developer, power provider, or data center SPV) | Air permit applicant; state business filings; utility commission filings | Often a separate power company (e.g. VoltaGrid, Crusoe) |
| Equipment liens: secured party, collateral described, filing date | UCC-1 financing statements at the Secretary of State of the owner's state of organization (search by owner and SPV names) | Shows who lent against the turbines or engines, even when the loan isn't public |
| Land and plant mortgages | County deed of trust and mortgage records | Lender and recorded amount where stated |
| Project or equipment financing: lender, amount, type (loan, lease, vendor financing) | Owner's or sponsor's SEC filings and credit agreement exhibits; supplier filings (e.g. vendor financing disclosed by equipment makers); press (tag R) | n/d where private |
| Power purchase or energy services contract: buyer, term, minimum payments | Buyer's and seller's filings; utility commission dockets; press | Links to the obligation map (spec 18) as a Power purchase edge |
| Equipment orders and deliveries | Supplier filings and announcements (GE Vernova, Siemens Energy, INNIO, Caterpillar/Solar Turbines, Bloom Energy) | Delivery dates feed the site schedule |
| Fuel supply contract and pipeline in-service date | FERC (interstate) and state pipeline permits (e.g. Texas Railroad Commission) | Already in spec 15 |

## Screen changes

- **On-site power table (spec 15):** add columns Owner, Financed by (lien holders or lenders), Debt ($, tag), Power buyer.
- **Plant detail:** new section "Debt and contracts": liens (secured party, date, collateral text, link), loans and leases, power purchase contract, and "Who loses if the served site slips": plant debt + the site's chain.
- **Obligation map (spec 18):** plants appear as nodes; power purchase and equipment financing appear as edges.

## Acceptance

- Every lien links to its UCC filing record; secured parties are matched to entities by the Linker agent and approved once.
- Plant debt shows n/d, not zero, where nothing is filed.
