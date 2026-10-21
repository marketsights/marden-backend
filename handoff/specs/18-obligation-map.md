# 18 · Obligation map (`/obligations`, `/obligations/:company`)

**Job:** who owes whom for AI infrastructure. Every lease, compute contract, chip purchase, backstop, investment and loan between the AI companies, their suppliers and their landlords, with the debt and holders behind each. Answers: "If company X misses, who gets hurt, how much, and through which instruments?"
**V1 companies:** OpenAI, Anthropic, Oracle, Microsoft, CoreWeave, Nvidia, plus every counterparty their filings name (Broadcom, AMD, Google, Meta, Nscale, Nebius, IREN, Crusoe, Vantage, Blue Owl and others) as they appear.

## Data model

- **Node:** a company or vehicle (`entity`, shared with the debt chains), with type (AI lab, cloud, neocloud, chipmaker, landlord/developer, SPV, lender, guarantor).
- **Edge:** one obligation from payer to payee.

| Edge field | Meaning |
| --- | --- |
| type | Lease · Compute purchase (take-or-pay) · Chip purchase · Equity investment · Loan or credit facility · Guarantee or backstop · Warrant · Revenue share · Power purchase |
| total amount and annual amount | Committed dollars, total and per year where disclosed |
| start, term, end | Dates |
| conditions | Termination rights, outside dates, force majeure, minimum-take, step-downs |
| status | Announced · Signed · Started · Amended · Disputed · Terminated |
| linked sites and instruments | Data center sites, plants, loans and bonds that depend on this edge |
| source and tag | Filing (F), reported (R) or Marden estimate (M) |

## Where each piece of data comes from

| Data | Source | How pulled |
| --- | --- | --- |
| Lease and purchase commitments, including leases signed but not yet started | 10-K and 10-Q notes: commitments, contractual obligations, leases (Oracle, Microsoft, CoreWeave, Meta, Amazon, Alphabet) | EDGAR API; extraction agent reads the commitment and lease notes each quarter |
| Remaining performance obligations (backlog) and customer concentration | 10-K and 10-Q revenue notes (Oracle, CoreWeave, Nebius, IREN) | Extraction agent; concentration percentages (e.g. Microsoft at 62% of CoreWeave 2024 revenue) become edge weights |
| Specific contracts (OpenAI–Oracle, OpenAI–CoreWeave, OpenAI–AMD warrants, Anthropic–Google TPU leases, Microsoft–Nebius, Microsoft–IREN) | Counterparty 8-Ks and material-contract exhibits (EX-10); press releases | EDGAR full-text search on each counterparty name; extraction agent |
| Private companies' obligations (OpenAI, Anthropic) | Their counterparties' filings (above); S-1 or other filings once public; rating agency reports where published; reported deals (tag R) | Edges built from the public side of each contract |
| Backstops and guarantees (e.g. Broadcom on Anthropic chip-lease debt) | Guarantor's 10-Q/8-K; offering documents and rating reports for the vehicle | Extraction agent |
| Equity stakes between companies | 10-K investment notes, 13F, 13D/13G, Form D | EDGAR API |
| Credit facilities and loans | Credit agreement exhibits (EX-10), 8-Ks, prospectuses | Extraction agent |
| Who holds the debt | N-PORT, BDC schedules of investments (as in the debt chains) | Code |
| Power purchase contracts | Utility commission dockets, filings (spec 20) | Collectors + extraction |

Every edge keeps its quote and document link; a reported edge stays tagged R until a filing confirms it.

## Screens

### Map (`/obligations`)

- Network graph: nodes sized by total obligations owed to them, colored by the payer's credit status (same status colors); edges thickness by annual amount, style by type. Filter chips: type, status, minimum amount, company. Click a node → company page; click an edge → side panel with terms, conditions, source and linked debt.
- Table view of all edges (export to Excel).
- KPI strip: total committed obligations mapped, share filed vs reported, edges added or amended this month.

### Company page (`/obligations/:company`)

- **Owes:** every outgoing edge, by year (stacked bars of annual commitments by counterparty) and as a table.
- **Is owed:** every incoming edge, with each payer's status.
- **Concentration:** share of revenue or backlog from each payer.
- **Funding gap:** committed outflows by year vs disclosed cash, credit lines and announced raises (tag M for the gap).
- **If this company misses:** link to the contagion stress below.

### Contagion stress (on Stress, spec 09)

New scenario: "Company X pays only N% of its obligations." The model walks the graph:
1. First order: payees lose that share of their incoming edges (revenue, rent).
2. Each payee's own coverage of its outgoing obligations and debt is recomputed; payees that fall below coverage pass losses on (second order), up to three hops.
3. Output: companies hit, dollars at risk per hop, instruments affected with Marden price change, and holders (funds from N-PORT and BDC filings).

## Acceptance

- Every edge opens its source passage; no edge without a document.
- Annual amounts sum to the company's disclosed totals where a total is disclosed; mismatches go to the review queue.
- The contagion run returns in under 5 seconds for any V1 company.
