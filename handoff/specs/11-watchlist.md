# 11 · Watchlist and alerts (`/watchlist`)

**Job:** the names the user follows (sites and instruments), what is happening to them, and the alerts they get. This replaces any holdings upload: Marden never asks for holdings.
**Reference:** screens/11-watchlist.html · **Endpoints:** `GET /api/watchlist`, `POST /api/watchlist/items`, `DELETE /api/watchlist/items/:id`, `GET /api/alerts`, `POST /api/alerts/read`, `GET/PUT /api/alert-settings`

## Layout

1. Header: "Watchlist", subtitle "{n} names you follow. Alerts fire only on these, by email and in the app." Add box ("Add site, issuer or CUSIP", typeahead) + Excel.
2. Summary cards (4): debt on watched sites; names with a red link; fund marks 5+ pts high; catalysts in 45 days.
3. Table with tabs All / Sites / Instruments. Columns: Name (link + type chip; sub-line chain or size) · Status · Cushion · Price · 1w · Mark gap · Next catalyst. Row menu: Remove, Alert settings.
4. Right rail (drops below the table under ~1300 px): **Alerts this week** (AlertItems, Mark all read) and **Email me when** (one checkbox per alert type; price threshold set in Account).

## Acceptance

- Adding an issuer adds its instruments and the sites they depend on (user can uncheck).
- Alerts list only watched names; each alert links to the record and the page.
- Unread alert count drives the sidebar badge ("5 new", tooltip "5 unread alerts") and the Home widget. The badge never shows the number of names.
