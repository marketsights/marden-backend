# Marden V1 Product Spec

Oct 20, 2026 · @Cody Lister

## Positioning

Marden prices and stress-tests AI data center and GPU debt by linking each loan to the tenant chain that pays it. The site map is the foundation; the paid product is the computed answers on top of it.

| Competitor | What they have | Gap Marden fills |
| --- | --- | --- |
| [Atrium](https://commercialobserver.com/2026/08/atrium-launches-ai-platform-data-center-development-credit/) (launched Aug 2026) | $1.3T of data center debt across \~4,300 facilities from property records, SEC credit agreements, CMBS and loan syndications; maturities | Real-estate lens. To verify in a demo: tenant and end-user chain, GPU and chip-lease debt, fund holders and marks, power and permit signals |
| [SAVRN Capita](https://savrn.com/data-center-capital-atlas)[l Atlas](https://savrn.com/data-center-capital-atlas) | 453 named sites, 600 financings, 98 capital providers | A tracker: no prices, holders or stress |
| [Telborg](https://telborg.com/dc-intel/briefings/vantage-frontier-shackelford) | Site briefings from permits, dockets and SEC filings | No lender or debt layer |
| Octus, 9fin | Issuer and deal debt, news, covenants; Octus tracks \~$125B of non-IG AI-infra debt | No site-level chain; little private SPV or bank construction debt |
| [SynMax Vulcan](https://blog.synmax.com/vulcan/beyond-the-visual-confirming-energization-with-thermal-intelligence-1) | Satellite and thermal-drone checks of construction and energization | No debt |
| S&P (datacenterHawk, 451, Ratings, Capital IQ) | Capacity, power, rated deals, issuer capital structures | Unrated private debt tied to leases and holders (unconfirmed) |

**What only Marden owns:** the end user → tenant → SPV → lender chain, GPU and chip-lease debt, holders and their marks, and computed price, recovery, stress and schedule risk.

## Buyers and pricing

Start with buyers who trade on the answer; add buyers who must defend a mark once the price has a track record.

| Buyer | Question Marden answers | Price |
| --- | --- | --- |
| Credit hedge funds, distressed desks | Which AI-infra instruments are mispriced, and what happens to them in a stress | $15–25k/yr as design partners; $50k+ once pricing is proven |
| BDC and interval-fund shorts | Which funds mark AI paper above market, by how much, and the NAV hit | Same |
| Insurers, ABS buyers, private credit managers | Fair value and recovery on illiquid SPV and GPU debt they hold or are offered | $50–100k/yr, after 6+ months of price history |

No holdings uploads. Watchlists only.

## V1 coverage

V1 covers the 50 largest AI-linked instruments of $1B or more, US only, 2024–2026 vintages. Stabilized colocation, CMBS and non-US deals wait for V2.

| Segment | Examples | Target count |
| --- | --- | --- |
| Construction and project loans | Jupiter, Frontier, Lighthouse, Abilene, The Barn | 15 |
| Chip and GPU lease vehicles | Anthropic Big Sky and North Star, Meta Hyperion (Beignet) | 8 |
| GPU delayed-draw facilities | CoreWeave DDTL 1.0–5.0, IREN, Nebius, Nscale | 12 |
| Neocloud and developer bonds | CoreWeave 2031s and 2032s, Vantage sub loan | 8 |
| Tenant and end-user credits | Oracle, CoreWeave, OpenAI, Anthropic, Microsoft (reference only) | 7 |

Every site on these chains is covered too, about 25 at launch.

## Data model

Seven objects, linked as Site → Contract → Instrument → Holder, with Events and Prices attached. Every field stores its value, source URL, as-of date and a tag: Filed, Reported or Marden-modeled.

| Object | Key fields |
| --- | --- |
| Site | Location, MW (IT and power), power source and status, interconnection queue ID, permits, construction stage, expected energization date |
| Party | Legal entity, parent, role (end user, tenant, developer, SPV, lender, guarantor), rating, CDS, funding needs |
| Contract | Counterparties, type (lease, compute, take-or-pay), term, rent start, outside date, termination and force majeure terms, rent amount |
| Instrument | Type, size, seniority, coupon or spread, maturity, amortization, collateral, guarantee or backstop, recourse, DSCR tests, CUSIP |
| Holder | Fund, BDC or bank, amount, mark, mark date, filing |
| Event | Date, source, type (permit, docket, rating, notice, price), link affected, status change |
| Price | Marden price and spread, recovery value, inputs, method version, date |

Each chain link (end user, tenant, developer, lender) gets a status from fixed rules, as on today's site pages. A chain's status is its worst link.

## Computed outputs (the paid product)

Every output ends in a number tied to an instrument or a fund mark. If it doesn't, it isn't sold.

| Output | What it answers | Built from | Cadence |
| --- | --- | --- | --- |
| Marden price and spread | What an untraded loan or note is worth today | Existing pricing engine: tier curves, bond-implied CDS, fund marks, chain signals | Daily |
| Mark gap | Which funds hold it above market, by how much, and the NAV hit | Price vs N-PORT and BDC marks | Daily, and on each filing |
| Recovery value | What lenders get if the tenant or contract counterparty fails | GPU collateral at current rental and resale values; powered-shell value; seniority | Weekly |
| Stress engine | If X happens, which instruments lose rent coverage, how much value, and who holds them | Linked chains; scenarios such as an end-user funding miss, a tenant downgrade, a power delay of N months | On demand |
| Schedule cushion | Months between projected completion and the lease outside date | Permits, interconnection milestones, construction progress | Weekly |
| Repricing risk list | The instruments most likely to reprice in the next 90 days, with trigger, evidence and holders | All of the above | Weekly, with the AI-written note |

Proof before selling: backtest 15 or so past repricings (Jupiter, CoreWeave unsecured, Anthropic junior, Nscale) and publish signal date vs price-move date.

## Sources

Primary records first; press is allowed but always tagged Reported.

| Source | Feeds | Notes |
| --- | --- | --- |
| SEC EDGAR: 8-K exhibits, 10-K/10-Q, offering docs, ABS filings | Contracts, instruments, recourse, terms | Core |
| SEC N-PORT and BDC schedules of investments | Holders and marks | Quarterly, \~60-day lag |
| State air permits (e.g. TCEQ) | Power build-out, expansion timing | Strong lead signal |
| Interconnection queues (ERCOT, PJM, MISO, SPP) and FERC eLibrary | Power status and dates |  |
| State utility commission dockets, county agendas | Large-load deals, tax abatements, local blocks |  |
| County recorder and state UCC filings | Lenders, liens, GPU collateral | Atrium covers property records; Marden pulls only what links a chain, plus UCC for GPU debt |
| Rating agency releases | Tenant and deal ratings |  |
| Public GPU rental price data | GPU collateral and recovery values |  |
| TRACE | Trade prints once 144A notes trade | Later |
| Free satellite imagery (Sentinel-2, Landsat) | Construction progress only | Thermal tested Oct 2026 on Abilene: no usable signal |

## Delivery

- **Web app:** the existing screens (sites, site detail, watchlist, payers, credit curve), plus a stress page and the repricing list.
- **Excel export** of every table.
- **Alerts** in-app and by email on watchlist names: a link changes status, a new record is filed, a fund mark is published, the Marden price moves past a threshold.
- **Weekly note** written by the AI agent from the repricing list.
- **API and MCP:** after V1 ships; Marden has no API yet.
- **Fix before any demo:** the Jupiter page shows "4 positions · $93mm" while the watchlist says no positions are stored. Drive it from the watchlist or remove it.

## Out of scope for V1

- Utilization from thermal or grid data, and drones. Revisit with paid nighttime thermal once there is revenue.
- A capacity database. S&P owns datacenterHawk.
- Stabilized colocation, CMBS and non-US deals.
- News reporting and covenant analysis. That is Octus and 9fin's ground.
- Holdings uploads.

## Build order and definition of done

V1 is done when a fund can open any of the 50 instruments and get a sourced chain, a Marden price, a recovery value and a stress result.

1. **Check Atrium.** Book a demo and confirm what it lacks: tenant chain, GPU debt, holders and marks, signals. Adjust this spec to the real gap.
2. **Lock the universe** of 50 instruments and \~25 sites.
3. **Fill the chains** from filings, permits, dockets and UCC, with no unexplained n/d.
4. **Holders and marks** from N-PORT and BDC filings for every instrument.
5. **Run the pricing engine** on all 50 daily; publish the method.
6. **Recovery and stress engines**, starting with three scenarios: OpenAI funding miss, Oracle downgrade, 6-month power delay.
7. **Backtest** about 15 past repricings for lead time.
8. **Alerts, Excel export, weekly note.** Then the API.

**Done when:**

- [ ] 50 instruments and their sites covered
- [ ] Every field has a source, date and Filed / Reported / Modeled tag
- [ ] Daily Marden price on all 50, with method and backtest published
- [ ] Recovery value on every GPU and construction loan
- [ ] Three stress scenarios live
- [ ] Alerts firing on watchlist names
- [ ] "In your book" contradiction fixed

Every screen and feature, led by the Completion monitor: Screens and features

## Sources

- [Commercial Observer: Atrium launch](https://commercialobserver.com/2026/08/atrium-launches-ai-platform-data-center-development-credit/)
- [SAVRN: Data Center Capital Atlas](https://savrn.com/data-center-capital-atlas)
- [Telborg: Vantage Frontier briefing](https://telborg.com/dc-intel/briefings/vantage-frontier-shackelford)
- [SynMax: energization via thermal](https://blog.synmax.com/vulcan/beyond-the-visual-confirming-energization-with-thermal-intelligence-1)
- [Octus: AI Infrastructure Quarterly Q1'26](https://octus.com/resources/articles/sector-insights-tech-hardware-ai-infrastructure-quarterly-q126/)
- [S&P Global:](https://press.spglobal.com/2026-07-28-S-P-Global-to-Acquire-datacenterHawk-Connecting-Global-Data-Center,-Power-and-Infrastructure-Markets)[ datacenterHawk acquisition](https://press.spglobal.com/2026-07-28-S-P-Global-to-Acquire-datacenterHawk-Connecting-Global-Data-Center,-Power-and-Infrastructure-Markets)
