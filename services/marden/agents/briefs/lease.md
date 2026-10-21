## Why it matters

Most data-center debt is repaid from one tenant's rent. A lender's whole case is the lease: who the tenant
is, how many MW it takes, when rent starts, the outside date after which the tenant can walk or claim
credits, what the tenant can terminate for, who guarantees it, and whether payment is take-or-pay. The
cushion model measures the months between likely completion and the outside date, so rent start and outside
date are the two most valuable facts you can find, and the two most often hidden.

## Where the facts are

- "Basic Lease Information" / "Summary of Key Terms" at the front: landlord, tenant, effective date, term,
  extension options, premises, rent, MW (often "Electricity Demand Threshold", "Critical IT Load",
  "Contracted Capacity" in kW), guaranty.
- Definitions: "Commencement Date", "Rent Commencement Date", "Outside Completion Date", "Outside Date",
  "Delivery Date", "Target Commencement Date".
- Delivery / delay sections: tenant remedies for late delivery (rent credits, termination right after the
  outside date).
- Termination, casualty, condemnation and force majeure sections.
- Compute / colocation / master services agreements and offtake contracts: capacity, term, minimum or
  take-or-pay payments, customer termination rights.
- 8-K and press summaries of a lease: tenant, MW, term, total contract value.

## How to record

- The subject is the site or campus the lease is for (named as the document names the building or campus),
  or the obligation when Marden tracks the contract itself.
- lease_mw in MW: 25,000 kW is 25. Record it per phase only as the document states it (one fact per phase
  is fine); never add phases up.
- Dates: rent_start / outside_date as YYYY-MM-DD or YYYY-MM when only the month is given. When the document
  defines a date only relative to another one ("[***] after the Target Commencement Date"), there is no date
  to record: leave the field out (a delay remedy can still go in lease_termination as text).
- lease_renewal, lease_termination, force_majeure, take_or_pay and lease_guarantee are short texts in the
  document's words.

## Traps

- Redactions ("[***]") are everywhere in filed leases; a redacted value is not disclosed. Never guess it from
  context.
- Landlord guaranty (by the developer's parent) is not a tenant guarantee; say whose guarantee it is.
- "Effective Date" is the signing date, not rent start.
- Examples inside a lease ("For example, if the Commencement Date ... is January 30") are not facts.
- Square feet are not MW.

## Examples from real filings

Applied Digital Datacenter Lease, Building 4 (EX-10.1), stored document:
- quote "COREWEAVE, INC., a Delaware corporation (“Tenant”)" -> lease_tenant "CoreWeave, Inc." on site
  "Building ELN04" / the Polaris Forge 1 Building 4 campus named in the lease.
- quote "APLD ELN-02 C LLC, a Delaware limited liability company (“Landlord”)" -> lease_landlord "APLD
  ELN-02 C LLC".
- quote "Three (3) Extension Options, each to extend the Term of all the Data Halls for an Extension Term of
  sixty (60) months" -> lease_renewal with that text.
- quote "Phase 1: 25,000 kW" under "Electricity Demand Threshold" -> lease_mw 25 (and the same for each
  phase listed; not a total).
- quote "a guaranty by Applied Digital Corporation, a Nevada corporation (“Landlord Guarantor”)" ->
  lease_guarantee "Landlord guaranty by Applied Digital Corporation".
- "Target Commencement Date: Phase 1: [***]." is redacted: no fact.
