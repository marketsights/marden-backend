# 24 · Payer capacity and downgrade risk (`/payers`, Payer page, Stress)

**Job:** can the companies whose leases and compute contracts back the debt actually afford them? And if one is downgraded, which loans, SPV bonds and GPU facilities reprice, and who holds them?

**Why:** GPU facilities and SPV bonds are underwritten on the payer's credit. For example, CoreWeave's $8.5B March 2026 delayed-draw facility was rated investment grade on the strength of a Meta contract. If the payer keeps signing leases faster than AI revenue grows, its ratings fall, and every instrument that relies on its rating or its payments reprices together.

**Payers in V1:**
- Microsoft, Meta, Alphabet, Amazon, Oracle (hyperscalers and clouds)
- OpenAI, Anthropic, xAI (AI labs)
- CoreWeave, Nebius, IREN, Nscale, Lambda (neoclouds as obligors)
- any other payer the V1 chains name

## Data and where it comes from

| Data | Source | How pulled | Tag |
| --- | --- | --- | --- |
| Operating cash flow, capex, free cash flow, cash, debt, lease liabilities, by quarter | SEC XBRL company facts (10-K/10-Q) | API, no parsing | F |
| Leases signed but not yet commenced, by year; purchase and compute commitments | 10-K/10-Q lease, commitment and contractual obligation notes; S-1 for Anthropic once public | Extraction agent | F |
| Remaining performance obligations and customer concentration | 10-K/10-Q revenue notes (e.g. Oracle: OpenAI about half of $638B RPO, per S&P) | Extraction agent | F |
| AI revenue where disclosed (run-rates, segment lines) | Earnings releases, 10-Q MD&A, S-1 | Extraction agent | F or R |
| Private payers' revenue, burn and commitments | Counterparty filings; draft S-1 coverage; press | Extraction agent | R until filed |
| Ratings, outlooks and actions; the downgrade triggers each agency states | Agency press releases (public pages) and company filings | Collector + extraction | F |
| Bond prices and spreads | Legacy TRACE refresh and fund marks; CDS only if a feed is licensed | Code | F or n/d |
| Rating triggers inside the debt | Credit agreement exhibits (EX-10) and offering docs: minimum counterparty rating for eligible contracts, advance-rate step-downs, cash sweeps, events of default tied to the payer | Extraction agent | F |

## The model (v0, labeled M until checked against past rating actions)

1. **Committed outflows by year:** leases commencing plus compute and purchase commitments plus guided capex, set against trailing and guided operating cash flow. The difference is the **funding gap** for each year.
2. **Lease-adjusted leverage:** (debt plus lease liabilities) ÷ EBITDA, computed today and then year by year as signed leases commence, the way agencies treat them (Moody's calls unrecorded leases "implicit debt").
3. **Headroom to the trigger:** compare that path with each agency's stated downgrade trigger, for example S&P on Oracle: leverage sustained above 4.5x, or no path to positive free operating cash flow by fiscal 2029. Output: turns of headroom today and the year the trigger is breached.
4. **AI revenue coverage:** disclosed AI revenue ÷ committed AI-related payments. Shows n/d where not disclosed. Never estimate a figure the company doesn't report.
5. **Concentration:** largest counterparty's share of the payer's backlog or revenue.
6. **Downgrade risk band:** Low / Watch / High, from headroom, breach year, outlook and recent actions. Shown as a Marden view and never presented as a rating.

## Anticipating agency actions (the point of the model)

Agencies cut when **their own forecast** of an adjusted metric crosses **a threshold they have published**, usually after a negative outlook. Marden rebuilds that forecast from filings and recomputes it the day new information lands, which is often weeks before the agency acts.

**Worked example: S&P on Oracle (cut to BBB- on July 9, 2026)**
- **The event:** Oracle raised its fiscal 2027 capex guidance to $90–95B, against S&P's assumption of $60B.
- **What S&P did with it:** its forecast for fiscal 2027 free operating cash flow went from about −$24B to about −$42B, and adjusted leverage moved into the mid-4x area, past the 4x downgrade threshold it had set at BBB.
- **Leases count as debt:** S&P adds lease and purchase commitments to debt, including about $260B of leases starting in fiscal 2027–29.
- **Qualitative factors:** OpenAI is about half of Oracle's $638B backlog, the leases can't be exited, and new compute lessors are competing.
- **The clock:** S&P had put Oracle on negative outlook in July 2025, so the cut came 12 months later.
- **Mitigants that held it at investment grade:** equity raises ($5B mandatory convertible, $20B planned), customer prepayments, and Oracle's stated commitment to investment grade.

**What the model tracks per payer and per agency:**
1. **Stated triggers,** quoted with date. For Oracle now: S&P leverage above 4.5x or no path to positive FOCF by fiscal 2029; Fitch above 3.5x EBITDA leverage or 4x including rents; Moody's Baa2 negative, leverage "could approach 5x".
2. **The agency-adjusted metric, rebuilt:** debt plus leases plus purchase commitments, per that agency's published method, with the adjustments listed. Where an agency's exact adjustment isn't public, the model shows a range.
3. **Forward path from the company's own guidance:** capex guidance, lease commencement schedule, debt maturities, announced financing.
4. **Event recompute:** any earnings guidance change, new lease or commitment disclosure, 8-K contract, debt or equity raise or buyback change reruns the path the same day. If the forecast now crosses a trigger, a **"likely to act" alert** fires with the reason.
5. **Outlook clock:** months since a negative outlook or CreditWatch was assigned.
6. **Mitigants:** equity raised, buybacks cut, prepayments, asset sales. These delay or prevent cuts.
7. **Market signal:** bond spreads vs same-rated peers (and CDS where licensed). Markets usually move first; a wide gap to the current rating supports the call.
8. **Fallen-angel flag:** two of three agencies would be below investment grade. That forces index exits and selling by investment-grade-only holders, so the alert lists every holder of the payer's bonds and every instrument with rating-linked terms. Example: Oracle's $14B RD Michigan project financing and its $10B revolver pricing are tied to Oracle's rating.

**Honest limits:**
- Agencies also weigh judgment ("a negative view of the AI industry") that can't be computed.
- Their exact adjustments are only partly published.

So the output is a probability band with its reasons, and the backtest (Oracle July 2026 and every other action on V1 payers) decides whether the v0 label comes off.

## Propagation (where it hits the debt)

- Every instrument whose repayment depends on the payer is linked through the obligation map (spec 18): lease, compute contract, guarantee, residual value guarantee or GPU contract backlog.
- For each one, show the rating trigger in its documents if there is one, what happens when it trips (eligibility lost, advance rate cut, cash sweep, default), and the holders from N-PORT and BDC filings.
- **New stress scenario: "Payer X downgraded N notches" or "to below investment grade".** Output:
  - which instruments hit a trigger
  - the Marden price change
  - recovery change for GPU facilities (spec 19)
  - the holders and their mark gaps

## Screens

- **Payers index (`/payers`):** one row per payer:
  - rating and outlook per agency
  - lease-adjusted leverage today and at peak
  - headroom to the trigger and the breach year
  - funding gap over the next 3 years
  - AI revenue coverage
  - top counterparty share
  - downgrade risk band
  - instruments exposed, with total $ and holder count

  Sorted by downgrade risk.
- **Payer page (spec 08), new "Capacity to pay" section:**
  - committed outflows vs cash flow by year (stacked bars)
  - leverage path vs the agency trigger line
  - rating history
  - exposed instruments with their triggers and holders
  - a "Run downgrade stress" button
- **Risk list:** a payer moving to Watch or High raises every linked instrument.
- **Alerts:** rating or outlook action on a watched payer; headroom under 0.5x; new lease commitments disclosed above 10% of the prior total.

## Acceptance

- Every input opens its filing or release passage; derived values are M v0 with the formula on hover.
- Trigger thresholds quote the agency's own words and date.
- **Backtest before the "v0" label comes off:** past rating actions on V1 payers (e.g. S&P's July 2026 Oracle downgrade to BBB-) must show as Watch or High at least one quarter ahead.
