## Why it matters

County commissions, planning boards and courts decide whether a data center gets its zoning, its tax
abatement or industrial revenue bonds, and whether a lawsuit or moratorium stops it. A vote, a moratorium or
a filed lawsuit is an opposition event on the site's Opposition tab and can trigger an alert. Agendas are
the earliest public signal: they show what will be voted or presented, and when.

## Where the facts are

- County / city agendas: consent agenda items, resolutions and ordinances (IRBs, tax abatements, PILOT
  agreements, zoning and annexation), presentations, appointments to project committees, public hearings,
  closed-session litigation lists (case names and numbers).
- Minutes: how each item was decided (approved, tabled, denied) and the vote.
- Court dockets and filings: court, case number, parties, claims, status, filing and hearing dates.

## How to record

- The subject is the project as the document names it ("Project Jupiter"), so the linker can tie it to the
  financed site; use the site or campus kind.
- opposition_event {"kind", "body", "date", "outcome", "case_number"}: kind is one of vote, moratorium,
  lawsuit, hearing, petition, appeal, presentation, appointment; outcome in the document's words
  ("scheduled", "approved 4-1", "tabled"). An agenda only schedules: outcome "on the agenda".
- tax_abatement {"body", "amount_usd", "term", "date", "status"} for IRBs, abatements and PILOTs.
- lawsuit {"court", "case_number", "parties", "claims", "status", "filed"} only for cases about a data
  center, its power or its permits.
- hearing_date for a scheduled hearing or vote about the project.

## Traps

- Most agenda items have nothing to do with data centers (grants, proclamations, road contracts): leave them
  out. An empty answer is correct for an agenda with no data-center item.
- A closed-session litigation list names cases the county is party to; record a lawsuit only when the case is
  about a data-center project.
- The meeting date is the date of the event; the agenda's posting date is not.
- Do not infer an outcome from an agenda.

## Examples from real records

Doña Ana County Board of County Commissioners, amended agenda for the regular meeting of October 13, 2026,
stored document:
- quote "Presentation and Update by Project Jupiter Representatives" -> opposition_event {"kind":
  "presentation", "body": "Doña Ana County Board of County Commissioners", "date": "2026-10-13", "outcome":
  "on the agenda"} on site "Project Jupiter".
- quote "Appointment of Don Bassett and Marcus Wells as the District 4 members to Community Monitoring
  Committee Related to Project Jupiter." -> opposition_event {"kind": "appointment", "body": "Doña Ana County
  Board of County Commissioners", "date": "2026-10-13", "outcome": "on the agenda"} on site "Project
  Jupiter".
- grant awards and proclamations on the same agenda: no facts.
