## Why it matters

Quarterly and annual reports are where a borrower's whole debt stack, its maturities, its commitments and
its own view of liquidity are filed under oath. Lenders read them for refinancing walls, for how much of
the debt each facility carries, for covenant compliance and, above all, for going-concern language. The
XBRL collector already writes the headline balance-sheet numbers; you add what lives in the footnotes and
in MD&A text.

## Where the facts are

- The debt footnote ("Debt", "Borrowings", "Notes payable"): one row per facility or note with maturity,
  rate and the balance at each date; the future principal payments table by year.
- Commitments and contingencies: purchase obligations, remaining performance obligations, guarantees.
- MD&A "Liquidity and Capital Resources": cash, availability under facilities, the statement that cash and
  facilities are (or are not) sufficient for at least the next 12 months.
- Going concern: a note titled "Going Concern" or language "substantial doubt about the Company's ability to
  continue as a going concern". Record going_concern true only when the filing says substantial doubt
  exists; false only when it says such doubt was alleviated.
- Covenant compliance: "As of June 30, 2026, we were in compliance with all covenants".

## How to record

- Company-wide figures (total_debt_usd, cash_usd, debt_maturities, purchase_obligation_usd,
  liquidity_text, going_concern) are on the entity: the registrant as named on the cover, with its CIK
  when printed.
- One facility's balance is outstanding_usd on that instrument (named as in the table: "DDTL 1.0 Facility",
  "2031 9.00% Senior Notes"), with as_of the balance-sheet date of the column you read.
- Tables state their scale ("dollars in millions", "in thousands"): write full USD. Quote the table row.
- debt_maturities is {"2026": usd, "2027": usd, ..., "thereafter": usd} exactly as the table lists it; use
  the year labels the table uses ("Remaining portion of 2026" is "2026").

## Traps

- Each table has two or more columns (this period, prior year end). Use the column for the latest date, and
  set as_of to that date.
- Carrying value net of discounts and issuance costs is not principal; prefer the principal column and say
  which in `note` when both exist.
- "Total liquidity" includes undrawn availability; it is not cash.
- Risk-factor sentences ("we may not be able to refinance") are not facts. Forward-looking plans are not
  facts.
- Do not recompute totals or subtract anything.

## Examples from real filings

CoreWeave 10-Q for the quarter ended June 30, 2026, stored document:
- under "The following table summarizes our principal sources of liquidity for the periods presented (in
  millions)", quote "Cash and cash equivalents $ 5,524 $ 3,127" -> cash_usd 5524000000 on entity
  "CoreWeave, Inc.", as_of 2026-06-30.
- in "10. Debt" ("dollars in millions"), quote "DDTL 1.0 Facility Mar 2028 15% $ 1,300 $ 1,553" ->
  outstanding_usd 1300000000 on instrument "DDTL 1.0 Facility", as_of 2026-06-30.
- the future principal payments table ("in millions"): quote its rows as one contiguous passage
  ("Remaining portion of 2026 $ 4,413 2027 6,184 2028 4,416 ...") -> debt_maturities {"2026": 4413000000,
  "2027": 6184000000, ...} on the entity, as_of 2026-06-30, with only the years the table lists.
