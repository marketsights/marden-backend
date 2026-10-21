## Why it matters

Marden's readers are lenders and bond investors in data-center and GPU debt. For every facility they need
what the debt is (size, tranches, pricing, maturity, amortization), what protects it (collateral,
guarantees, recourse, covenants) and what can blow it up (events of default, cross-default, change of
control). A wrong margin or a missed guarantor changes the price Marden computes, so precision beats
coverage: record fewer facts that are exactly right.

## Where the facts are

- 8-K Item 1.01 / 2.03 summaries: the cleanest statement of size, borrower, agents, pricing, maturity,
  guarantees, security and the headline covenant. Read them first; the exhibit (credit agreement) has the
  detail.
- Credit agreements: Article I definitions ("Applicable Margin", "Maturity Date", "Commitment Termination
  Date", "Commitments", "Debt Service Coverage Ratio"), Article II (the loans, fees), the negative and
  financial covenants article, the events of default article (cross-default threshold, change of control),
  the commitments schedule (lenders and amounts), the guarantee and collateral agreement.
- Indentures and notes: the cover and Section 1.01 (definitions), interest and maturity on the form of note,
  "Change of Control" / repurchase at 101%, events of default (cross-acceleration threshold), guarantors.
- Offering memoranda / 424B prospectuses: "The Offering" summary box and "Description of Notes".
- ABS / CMBS reports: class balances (tranches), coupons, ARD and rated final dates, DSCR / LTV triggers.

## How to record

- The subject of debt terms is the instrument, named as the document names it ("DDTL 5.5 Facility",
  "2.875% Convertible Senior Notes due 2033"), with its CUSIP or ISIN when printed. Put the borrower's
  legal name in `borrower` and list the borrower, parent and agents in `subjects` with their roles.
- Pricing: a floating loan is coupon_type "floating", coupon_benchmark ("Term SOFR"), coupon_spread_bp
  (SOFR loans' margin in basis points: 5.50% = 550) and sofr_floor_pct; record the base-rate margin only in
  `coupon` text if you record it at all. A fixed note is coupon_type "fixed" and coupon_rate_pct.
- Size: amount_usd is the total facility or principal. For delayed-draw and revolving facilities also record
  commitment_usd when the document states commitments. Never add up tranches yourself: record `tranches`
  only when the document lists them with amounts, and amount_usd only when the total is written.
- Covenants: min_dscr / max_ltv_pct / min_liquidity_usd for those tests; any other financial covenant as
  covenant_test {"metric", "threshold", "tested", "consequence"}.
- Collateral and guarantees in the document's words ("substantially all assets of the Borrower and its
  subsidiaries and a pledge of 100% of the equity interests in the Borrower").

## Traps

- "Up to", "may be increased to", accordion and incremental capacity are not the facility size.
- Redacted terms ("[***]", "[Reserved]") are not values: leave them out.
- The base-rate margin (usually 1.00% lower) is not the SOFR margin. Default interest (+2.00%) is not the
  coupon.
- Dates: the agreement date is closing_date; the maturity date is maturity; "commitment termination date in
  December 2026" is commitment_termination_date 2026-12.
- Amounts in tables may be "in millions" or "in thousands"; write the full USD value and quote the row.
- An exhibit to a BDC's 8-K is usually the fund's own borrowing, not a data-center loan: record it, but name
  the fund's facility as the subject, not a portfolio company.

## Examples from real filings

CoreWeave 8-K, August 2026 (Item 1.01), stored document:
- quote "providing for a $2.6 billion delayed draw term loan facility (the “DDTL 5.5 Facility”)" ->
  amount_usd 2600000000 on instrument "DDTL 5.5 Facility".
- quote "The maturity date of the DDTL 5.5 Facility is September 1, 2031." -> maturity 2031-09-01.
- quote "for SOFR loans, Term SOFR (subject to a 0.00% floor) plus an applicable margin of 5.50% per annum"
  -> coupon_spread_bp 550, sofr_floor_pct 0, coupon_benchmark "Term SOFR", coupon_type "floating".
- quote "undrawn fees in an amount equal to 0.50% per annum on the average daily undrawn portion of the DDTL
  5.5 Facility" -> undrawn_fee_pct 0.5.
- quote "the Borrower is required to maintain a debt service coverage ratio of at least 1.35x" -> min_dscr
  1.35.
- quote "JPMorgan Chase Bank, N.A., as administrative agent" -> admin_agent "JPMorgan Chase Bank, N.A.".
- quote "delayed draw term loans available in one or more draws until the commitment termination date in
  December 2026" -> commitment_termination_date 2026-12.
