# 13 · Account (`/account`)

**Job:** team seats, alert delivery, plan, sign-in.
**Reference:** screens/13-account.html · **Endpoints:** `GET /api/account`, `POST /api/account/invites`, `PATCH /api/account/members/:id`, `GET/PUT /api/alert-settings`

## Layout

Tabs: Team · Alerts · Plan and billing · Security (designed as one page of four cards; split into tabs in the build).

- **Team:** "{used} of {seats} seats", Invite button; table person (name, email), role (Owner / Admin / Member / Viewer), last active.
- **Alert delivery:** instant email when a watched name turns red; daily digest 7:00 ET; weekly risk list note on Mondays; default price-move threshold (1 / 2 / 3 pts).
- **Plan:** plan name, price, renewal date, invoices (Stripe invoices by email; link to past invoices).
- **Security:** sign-in methods and MFA status (Clerk), single sign-on (connect identity provider), data held ("Your watchlist and alert settings. Never your holdings.").

## Acceptance

- Only Admins can invite, remove or change roles.
- SSO is a Clerk enterprise connection on the organization (spec 23). Roles are Owner, Admin, Member and Viewer (spec 23); only Owners and Admins invite, remove or change roles; the last Owner cannot be removed.
