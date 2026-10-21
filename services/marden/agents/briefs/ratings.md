## Why it matters

A tenant or facility rating at BBB- or below moves a chain to amber; a downgrade with a power problem or a
force majeure turns it red; a default notice, forbearance, waiver or amendment is a lender action. Rating
changes and credit events fire alerts, so every one of these facts is material and waits for a person. Be
exact about agency, scale, outlook and what the rating applies to (issuer, facility, notes).

## Where the facts are

- Rating agency releases (Moody's, S&P, Fitch, KBRA, DBRS Morningstar): the headline action, the rating on
  each instrument, outlook (stable, positive, negative, developing) and watch status, the date.
- Company press releases and 8-Ks: "The facility received ratings of ...", default notices, forbearance and
  amendment announcements (8-K Items 1.01, 2.04, 3.01).
- 10-Q / 10-K: amendments and waivers obtained, defaults, cross-default status.

## How to record

- rating is text with agency and scale ("Moody's Ba2", "Fitch BB+ (outlook stable)") on the instrument or
  issuer the rating is for; record one fact per agency.
- rating_action {"agency", "rating", "outlook", "watch", "action", "date"}: action assigned, upgraded,
  downgraded, affirmed, placed on watch, withdrawn.
- credit_event {"kind", "date", "description"}: default, forbearance, amendment, waiver, acceleration,
  reservation of rights, draw stop.
- tenant_rating is on the site, for the tenant's own rating when the document gives it in that context.

## Traps

- Rating thresholds inside agreements ("a rating of at least A-1 from S&P", "A3 or higher by Moody's",
  pricing grids by rating level) are terms, not ratings of anyone.
- A rating of a different entity (the parent vs the borrower SPV, a counterparty bank) must be on that
  entity, not on the facility.
- Index or methodology descriptions ("securities rated below investment grade") are not ratings.
- An "investment-grade equivalent" or "A- equivalent from three agencies" without the agencies' own ratings is
  text for `note`, not a rating fact.

## Examples from real filings

CoreWeave press release (EX-99.1) on the DDTL 5.5 Facility, August 10, 2026, stored document:
- quote "The facility received ratings of Ba2 from Moody’s and BB+ from Fitch" -> rating "Moody's Ba2" and
  rating "Fitch BB+" on instrument "DDTL 5.5 Facility"; rating_action {"agency": "Moody's", "rating": "Ba2",
  "action": "assigned", "date": "2026-08-10"} and the same for Fitch BB+.
- quote "a rating of at least “A-1” from S&P or at least “P-1” from Moody’s" in a credit agreement's
  definition of cash equivalents -> no fact.
