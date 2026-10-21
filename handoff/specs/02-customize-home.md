# 02 · Customize Home (`/home/customize`)

**Job:** let the user add, remove and reorder Home widgets. Nothing else is configurable (no resizing, no custom charts).
**Reference:** screens/02-customize-home.html · **Endpoints:** `GET/PUT /api/home/layout`, widget data via the same endpoints as the source screens.

## Layout

- PageHeader: breadcrumb Home / Customize; actions **Reset to default**, **Done** (saves, returns to Home).
- Left rail "On your Home": current widgets in order, each with a drag handle and × remove. Note: "Home is a two-column grid. Wide widgets take both columns. Your layout is saved per user."
- Main "Widget library": each add-on widget rendered at real size with **+ Add**.

## Widget catalogue

| Widget | Default | Size | Content | Data |
| --- | --- | --- | --- | --- |
| Headline numbers | Yes | Strip | See Home | `/api/home` |
| What changed on your watchlist | Yes | Wide | See Home | `/api/home` |
| Alerts | Yes | Narrow | See Home | `/api/alerts` |
| Tightest cushions | Yes | Narrow | See Home | `/api/completion` (watchlist only) |
| Top 5 of the risk list | Yes | Narrow | See Home | `/api/risk-list` |
| Coming up | Yes | Narrow | See Home | `/api/watchlist` catalysts |
| Mark gaps by fund | Add-on | Narrow | Funds with the biggest NAV hit if marked to Marden, $mm bars (M) | `/api/instruments/:id/holders` aggregated over watchlist |
| Saved stress result | Add-on | Narrow | Scenario name, site-debt loss, tenant-notes loss, top 3 instruments hit | `/api/stress/saved/:id` |
| Payer exposure | Add-on | Narrow | Site debt on one payer's rent, stacked by site | `/api/payers/:slug` |
| Credit curve, 5-year point | Add-on | Narrow | Yield at 5y per tier, horizontal bars | `/api/curve` |
| One site's schedule | Add-on | Narrow | Cushion, mini timeline, last evidence. User picks the site. | `/api/sites/:slug/schedule` |
| One instrument's price | Add-on | Narrow | Marden price, fund marks, gap, sparkline. User picks the instrument. | `/api/instruments/:id` |

## Acceptance

- Drag reorder works with mouse and keyboard (handle is a button; arrow keys move).
- Removing the last widget shows an empty Home with an "Add widgets" button.
- Reset restores the six defaults in the order above.
