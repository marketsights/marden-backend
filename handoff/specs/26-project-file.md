# 26 · Project file: project-finance-level, 360° view (Site detail tabs + completeness score)

**Job:** for every V1 site and financing, give a holder what a project-finance lender's information package would give: who the parties are, how the deal is structured, how construction is going and what it costs, and what could stop it. Everything is rebuilt from public records. Anything that isn't public is marked as such, never guessed.

## The checklist (one row per item, per project)

| Area | Items | Public sources | Tag |
| --- | --- | --- | --- |
| **Parties** | Sponsor and equity investors, developer, landlord SPV and parents, tenant and end user, guarantors, general contractor and major subcontractors, equipment makers (turbines, transformers, switchgear, chillers, GPUs), utility and grid operator, lenders, agents, trustees, holders | Secretary of State records, permits (contractor of record), state contractor licenses, Form D, 8-Ks, credit agreements, offering docs, utility filings, N-PORT/BDC | F/R |
| **Lease or offtake** | Term, rent, escalators, commencement, outside date, termination rights, force majeure, residual value guarantees | Spec 25 and the lease tab (spec 04g) | F/R/M |
| **Structure** | Debt tranches and sizes, seniority, security package, completion guarantees, sponsor support, reserves (debt service, capex), sizing covenants (DSCR, loan-to-cost, loan-to-value), cash waterfall, hedges, rating triggers (spec 24), intercreditor terms | Credit agreement exhibits (EX-10), offering memoranda, ABS/CMBS documents, rating agency presale and new-issue reports where public | F |
| **Budget and cost** | Declared construction value per building permit; capital investment committed in tax abatement agreements; announced project cost; cost per MW vs peers; equipment cost signals | Building permits (valuation field), county abatement agreements, press, landlord filings | F/R, M for the comparisons |
| **Draws and funding** | Amount drawn vs committed per facility over time; equity contributed | 10-Q debt notes, 8-Ks, ABS/CMBS servicer reports, BDC schedules (funded vs unfunded commitments) | F |
| **Construction progress** | Milestones, satellite change, building footprints, certificates of occupancy, inspections passed (where published) | Spec 17 | F/M |
| **Contractor health** | **Mechanic's liens** (contractors not being paid), lien releases, contractor disputes and lawsuits, change of contractor, workforce signals (hiring, layoffs, WARN notices) | County recorder, court dockets, job postings, state WARN | F/R |
| **Equipment** | Turbine, transformer and switchgear orders and delivery dates; GPU deliveries (spec 19) | Air permits (unit make and model), OEM filings and backlog disclosures, 8-Ks, press | F/R |
| **Power** | Interconnection and energy supply agreements, on-site generation, fuel supply and pipeline laterals | Spec 17 | F |
| **Permits and land** | Air, building, water, zoning, environmental; land ownership and title | Specs 17, 20 and 25 | F |
| **Opposition and politics** | Agendas, petitions, lawsuits, moratoria, state legislation | Spec 17 collectors | F/R |
| **Insurance** | Builder's risk and delay-in-start-up cover | Rarely public: shown as not public unless an offering document discloses it | F or n/p |
| **Independent engineer** | IE reports and lender site visits | Not public. Marden's evidence trail is the substitute, and the screen says so | n/p |

Two ways a value can be missing:
- **n/d (not yet found):** counts against completeness and goes to the researcher agent.
- **n/p (not public):** a known limit, labeled as such, and doesn't count against completeness.

## Derived signals (M v0, with inputs on hover)

- **Cost-to-complete gap:** declared and announced cost vs peer cost per MW for the same design and power type.
- **Funding gap:** remaining cost vs undrawn debt plus committed equity.
- **Draw pace vs build pace:** money drawn vs physical progress. Draws running ahead of satellite and permit progress is a red flag.
- **Contractor stress:** new mechanic's liens or disputes in the last 90 days, by dollar amount.
- **Equipment risk:** long-lead items not yet delivered vs the milestone that needs them.

Each signal feeds the cushion model (spec 17) and the status rules, so a lien wave or a draw-pace break can move a site's band and fire an alert.

## Screens

- **Site detail, new tabs:**
  - **Parties:** who's who, with entity links and holders.
  - **Structure:** tranche diagram, security, reserves, covenants, triggers, waterfall.
  - **Construction:** budget, draws vs progress chart, contractor health, equipment.
- **Site header, 360° completeness score:** % of checklist items sourced, not counting n/p. Clicking it lists every n/d with what the researcher agent is doing about it.
- **Completion monitor:** new columns for completeness, contractor stress and draw-vs-build flag.
- **Alerts:** new mechanic's lien over $1M; contractor change; draw pace breaks from build pace; long-lead equipment slips.

## Acceptance

- Every V1 site has a completeness score, and every n/d has an open research item.
- Every structure term opens its source passage in the credit agreement or offering document.
- Lien and draw signals backtest on any V1 site where a slip was later confirmed.
