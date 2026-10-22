# Payer capacity, obligation map and lease map v0.1 (model v0)

Written 2026-10-21. Code: `services/marden/models/graph.py`, `obligations.py`, `credit_inputs.py`, `payer_years.py`, `payer_capacity.py`, `lease_map.py`. Models `obligations`, `obligation_company`, `payer_capacity`, `leases`, version `0.1`. Specs 18, 24, 25.

## Inputs (all point in time, `marden.models.pit`)

- **Obligation edges**: `obligation` rows and their facts. An edge is on the map once its type and a source passage are public.
  - Total: `total_usd`. Else the payer's `funding_fact` whose label names the payee ("Oracle contract", value "~$300bn", tag R). "up to $22.4bn" reads as the stated maximum.
  - Term: `term_years`, else the funding note ("~5 years").
  - Start year: the first year in `start`, else the note's "start(s) 2027".
  - Annual: `annual_usd`, else the note's "~$60bn a year", else total ÷ term (tag M, straight line).
  - The schedule runs from the start year for the term, with a fractional last year.
  - Guarantees, warrants and equity stakes are on the map but carry no schedule.
  - An edge with no amount, start or term has no schedule. It is listed in `missing`, never spread by guesswork.
- **Financials**: SEC XBRL companyfacts (`collectors/sec_xbrl_parse.FIELDS`), each usable 40 days after the period end.
  - Added 2026-10-21 for EBITDA and backlog: `operating_income`, `depreciation_amortization`, `depreciation`, `amortization_intangibles` (3/6/9/12-month durations) and `remaining_performance_obligation_usd`.
  - Trailing twelve months: fiscal year + year-to-date − prior year-to-date (the standard LTM build), else the latest fiscal year (the basis says so).
  - Debt: `total_debt_usd`, else long-term debt (current maturities included) + short-term borrowings.
  - Leases: operating + finance lease liabilities (IFRS: the single lease liability).
  - EBITDA: operating income + D&A, or depreciation + intangible amortization when the filer tags them separately (Oracle, Microsoft, Alphabet).
- **Ratings**: the R-tagged rating texts on the company's own unsecured notes (or texts that state "Issuer:"), and the tenant rating of the sites it leases.
  - A secured facility's own rating is not the company's.
  - A text that rates a backstop or calls the tenant unrated is skipped.
  - An agency the text does not name is not guessed. A Moody's-scale grade like "Baa2" can only be Moody's.
  - Outlook and last action ("cut 2026-07-09") are taken when stated.
- **Bond-implied CDS**: the price engine's `cds` output.

## obligations / obligation_company

- `ObligationGraph` for subject ('series', 'all'), and one `ObligationEdge` per obligation. Its `annual` feeds the obligations read model.
- KPIs:
  - totalB is the sum of edge totals.
  - filedPct is the share of those dollars with a filed (F) total.
  - changedThisMonth counts edges with a fact dated this month.
- `CompanyObligations` per entity at either end of an edge:
  - **concentration**: a payer's edge total ÷ the company's filed remaining performance obligations. When none is filed, ÷ the incoming totals mapped (the basis says which).
  - **fundingGap** per year, this year and the next four: committed = outgoing scheduled payments; funded = LTM operating cash flow (held flat: no guidance collected) + cash in the first year; gap = committed − funded (M). Companies without filed cash flow (OpenAI, Anthropic) have no rows.

## payer_capacity (PayerCapacity per payer)

- **leaseAdjLeverageToday** = (debt + lease liabilities) ÷ LTM EBITDA.
- **Path, per fiscal year after the latest filed one (3 years):**
  - leaseCommencingB: the totals of the payer's lease edges starting that year (undiscounted; agencies capitalize the present value, so v0 is an upper bound).
  - computeCommitmentsB: compute and chip purchase payments that year.
  - fundingGapB = LTM capex (held flat) + compute payments + lease payments in force − LTM operating cash flow.
  - leaseAdjLeverage = (debt + leases + leases commenced by then) ÷ EBITDA.
  - leaseAdjLeveragePeak is the maximum along the path. fundingGap3yB is the sum over the three years.
- **triggerHeadroomTurns / breachYear**: n/d. The agencies' stated downgrade thresholds are not collected yet, and a threshold is never taken from memory (spec 25). Every output lists this in `missing`.
- **aiRevenueCoverage**: AI labs only. Their reported annualized revenue (funding fact) ÷ compute payments next year. n/d elsewhere; never estimated.
- **topCounterparty**: the filed customer concentration (`top_customers`: "Customer A 36%"), else the largest payer's share of the filed backlog.
- **downgradeRisk** (a Marden view, never a rating). One point each for:
  - a negative outlook (up to 2);
  - a downgrade in the last 12 months;
  - sitting on the last investment-grade rung (BBB- / Baa3);
  - leverage rising one turn or more along the path;
  - a 3-year funding gap above a quarter of three years' operating cash flow.
  - Bands: 3+ High, 1–2 Watch, 0 Low. n/d with no rating and no filed financials.
- **exposed**: the debt behind leases, compute contracts and guarantees the payer owes. Each row carries the instrument's rating-tied term (`agreement_term` / `covenant_test`, F) and its holder count.

### Backtest (spec 24 acceptance)

S&P cut Oracle to BBB- on 2026-07-09. On data public on 2026-04-01, one quarter ahead, Oracle is **Watch**:

- 3-year funding gap of $39.5B against LTM operating cash flow of $22.3B and capex of $35.5B (XBRL to 2025-11-30).
- Lease-adjusted leverage 4.54x.

No rating fact was public then, so the call rests on the filings alone. On 2026-10-21 Oracle is **High**: Moody's negative outlook, the S&P cut, the last IG rung, and an $87.2B funding gap. Leverage is 5.11x.

## leases (LeaseMapPage)

- One LeaseRecord per lease and compute contract on the map.
- **turnsAdded** = edge total ÷ lessee LTM EBITDA (undiscounted, as above).
- **lesseeBand** = payer_capacity downgradeRisk.
- **priorityScore** = P(cut | band) × dependent debt $B × (1.5 when that debt quotes a rating-linked term) ÷ (1 + years until commencement). P(cut): High 0.6, Watch 0.3, Low 0.1. These are uncalibrated v0 weights, shown in the basis.
- **headroomBeforeTurns** is n/d and `triggerLease` is false until agency triggers are collected.
- **Reconciliation** needs each lessee's 10-K/10-Q "leases not yet commenced" total. us-gaap has no non-dimensional element for it, so it waits for the lease-note extraction. Until then:
  - there are no reconciliation rows;
  - disclosedB and unattributedB are 0;
  - the attributed totals cover the mapped edges only.
  - The residual is never estimated or spread across sites.
- Inferred leases (the researcher's trail) are not built in v0.

## Runs

- obligations / obligation_company: daily 09:25 UTC. payer_capacity: daily 09:29. leases: daily 09:33.
- Each also runs within a minute of its inputs through `models.run_dirty`:
  - edge facts and funding facts;
  - XBRL fields, rating texts and `cds` outputs;
  - a lessee's payer_capacity output.
