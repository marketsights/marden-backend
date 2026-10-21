# Global spec: applies to every screen

## Routes

| Screen | Route | Reference |
| --- | --- | --- |
| Home | `/` | screens/01-home.html |
| Customize Home | `/home/customize` | screens/02-customize-home.html |
| Completion monitor | `/completion` | screens/03-completion.html |
| Site detail | `/sites/:slug?tab=schedule\|evidence\|power\|buildings\|opposition\|events\|debt\|lease` | screens/04-site-detail.html |
| Sites | `/sites` | screens/05-sites.html |
| Instruments | `/instruments` | screens/06-instruments.html |
| Instrument detail | `/instruments/:id` | screens/07-instrument-detail.html |
| Payer | `/payers/:slug` | screens/08-payer.html |
| Stress | `/stress?scenario=` | screens/09-stress.html |
| Risk list | `/risk-list?week=YYYY-MM-DD` | screens/10-risk-list.html |
| Watchlist | `/watchlist` | screens/11-watchlist.html |
| Track record | `/track-record` | screens/12-track-record.html |
| Account | `/account?tab=team\|alerts\|billing\|security` | screens/13-account.html |
| Data centers | `/data-centers?view=table\|map`, `/data-centers/:id?tab=overview\|buildings\|power\|records\|history` | screens/14-data-centers.html … 14g |
| On-site power | `/power?view=table\|map`, `/power/:id` | screens/15-onsite-power.html … 15c |
| Obligation map | `/obligations`, `/obligations/:company` | specs/18-obligation-map.md (no design yet) |
| GPU index and collateral | `/gpus`, `/gpus/:model` | specs/19-gpu-collateral.md (no design yet) |
| Lease map | `/leases` | specs/25-lease-map.md (no design yet) |
| Payers index | `/payers` | specs/24-payer-capacity.md (no design yet) |

Filters, sort, tabs and selected week live in the URL so every view can be shared and bookmarked.

## Data rules

1. **Every value is a `Sourced<T>`**: value, tag (F/R/M), source URL, as-of date, and for M values the method version. The UI renders a SourceTag after the value. No untagged numbers.
2. **Point in time.** The API answers "as of" the latest snapshot. Track record and backtests read historical snapshots; never overwrite history.
3. **n/d** = not disclosed in any source yet. Show "n/d" in `ink-3`, never 0 and never blank.
4. **—** = not applicable (e.g. no cushion for a site with no lease).
5. **No holdings.** Exposure figures use the user's watchlist only.

## Status rules (fixed; shown on Track record)

Chain status = worst link.

| Link | Amber when | Red when |
| --- | --- | --- |
| End user | Its plan needs new funding | Contract cut, or end user exits |
| Tenant | Rated BBB- or lower, or must post contested collateral | BBB- or lower and power denied or withdrawn, or force majeure filed |
| Developer | In-service date slips 3+ months | Construction halted |
| Lenders | Price 95–98, or syndication stalled | Price below 95, or default notice |

Rule codes used in the UI: E1/E2 (end user), T1/T2 (tenant), D1/D2 (developer), L1/L2 (lenders), M1 (slip of 3+ months), M2 (cushion below zero).

## Materiality and alerts

An event is material when it moves projected completion 3+ months, turns the cushion negative, denies or withdraws a power source, comes with a tenant notice, or triggers a lender action. Every extracted event passes the human review queue before it changes a status or fires an alert.

Alert types (user can switch each on or off; defaults on): cushion under 3 months or under zero; milestone slip or later date filed; power event; opposition event (county vote, moratorium, lawsuit); tenant notice or rating change; fund mark filed or price move ≥ threshold (default 2 pts); no visible construction for 8 weeks.

Delivery: in-app (badge + Alerts lists) and email (instant for red; daily digest 7:00 ET for the rest; weekly risk list note on Mondays). Watchlist names only.

## Live updates

The app opens one `GET /api/stream` connection (Server-Sent Events) per session. Alerts, status changes, cushion changes, price and mark changes update the open screen and the sidebar badges without a reload. Prices refresh once a day after the 6:00 ET run; filings and alerts arrive as they are approved in the review queue.

## Model labels

Model outputs that are not yet calibrated carry their version in the label ("Odds · model v0"). Rent coverage is hidden until a filing discloses rent. When a lease date is known only to the year, the cushion assumes Dec 31 of that year and says so in the tooltip and on Site detail.

## Formats

- Money: `$18.0B`, `$412mm`, one decimal for B. Prices: one decimal (`88.6`). Spreads in bp. Yields one decimal with `%`.
- Dates: `Oct 13` within the current year, `Feb '29` for months, `Oct 13–19, 2026` for weeks. Times in ET.
- Signed changes always carry a sign (−0.9, +2). Use the true minus sign (−).
- Cushion in whole months.

## States (every screen and widget)

- **Loading:** skeleton rows/blocks in the final layout, no spinners over content.
- **Empty:** one line saying what would appear and how to get it ("Add a site or CUSIP to your watchlist to see changes here").
- **Error:** inline message in the card with Retry. Never a blank page.
- **Stale data:** if the last pipeline run is older than its expected cadence, the sidebar footer turns amber: "Data delayed · last update {time}".

## Navigation (sidebar)

MONITOR: Home · Completion · Risk list · Stress. BROWSE: Sites · Data centers · On-site power · Leases · Obligations · GPUs · Instruments · Payers. YOURS: Watchlist · Track record · Account.
"Sites" are the financed sites with debt chains; "Data centers" is every US campus and building; "On-site power" is every behind-the-meter plant (specs 14–17). The search box covers sites, issuers, instruments, CUSIPs, campuses, buildings (label, parcel ID), plants and permit numbers.

## Accessibility and responsiveness

- Real buttons, links and inputs; visible focus ring (2 px brand).
- Text contrast ≥ 4.5:1. Status never shown by color alone (pill text, tooltip text).
- Designed at 1440 px. At 1100–1300 px the right-hand rails drop below the main column. Below 800 px the sidebar stacks; tables scroll horizontally inside their card.
- Maps (Data centers, On-site power, Buildings): MapLibre with free vector tiles; markers carry status in a tooltip as well as color.

## Exports

Every table has an Excel button that exports exactly the rows and columns shown (respecting filters), with a second sheet of sources (field, tag, URL, as-of).
