# 01 · Home (`/`)

**Job:** the landing page after login. In one screen, show what moved on the user's names since their last visit.
**Reference:** screens/01-home.html · **Endpoint:** `GET /api/home` (+ `GET/PUT /api/home/layout`)

## Layout

1. PageHeader: "Home", subtitle "What moved on your names since your last visit, {date}". Actions: **+ Add widget** (opens the library on Customize Home), **Edit layout** (turns on drag handles and ⋯ menus).
2. Widget grid: 3 columns at 1440 px, 16 px gap. Widgets are placed in the user's saved order.

## Default widgets (in order)

| # | Widget | Width | Content | Opens |
| --- | --- | --- | --- | --- |
| 1 | Headline numbers | Full strip | 4 numbers: watched debt with cushion under 3 months ($, M); status changes this week (count + names); fund marks 5+ pts above Marden (count + top name); unread alerts (count + latest time). | — |
| 2 | What changed on your watchlist | 2 columns | Up to 5 rows: name (link), change "from → to", current status pill, record (date · what · tag). Ordered by severity, then date. | Watchlist |
| 3 | Alerts | 1 column | Latest 4 alerts (AlertItem). | Watchlist |
| 4 | Tightest cushions | 1 column | 4 watched sites with lowest cushion; diverging bar around 0 (negative red, 0–3 amber, else grey). | Completion |
| 5 | Top 5 of the risk list | 1 column | Rank, instrument, odds bar, %. Header shows the week ("Risk list, Oct 13–19"). | Risk list |
| 6 | Coming up | 1 column | Next 4 catalysts on watched names: when (Late Nov, Mid-Dec, H2 '26, Pending) + what. | — |

Strip at the bottom: "Add a widget" with the six library widgets as buttons (shortcut to Customize Home).

## Rules

- "Since your last visit" = previous session start, stored per user.
- Widget 2's "change" types: cushion change, price change ≥ threshold, link status change, new filing on a watched name.
- Counts in the sidebar badges come from the same endpoint.

## Acceptance

- A new user with an empty watchlist sees each widget's empty state with an "Add to watchlist" prompt, not zeros.
- Layout changes persist per user across devices.
- Every number has a tag; every row opens its detail page.
