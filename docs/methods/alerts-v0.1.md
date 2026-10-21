# Alerts and alert email, v0.1

Code: `services/marden/alerts/` (engine, rules, email); API: `apps/api/src/routes/alert-delivery.ts`,
`apps/api/src/queries/alerts.ts`; tables: `db/migrations/20261021223000_alert_engine.sql`.
Specs: 00-global "Materiality and alerts", 11 (watchlist), 13 (account, alert delivery).

Firms are told when a material matter comes through on a name they watch: in the app at once (the Alerts
lists, the sidebar badge, a live `alert` stream event), and by email (instant for red, a daily digest at
07:00 ET for the rest, a weekly summary on Mondays). Every title and body is a code template filled from
the computed numbers and the fact references. No language model writes alert or email text.

## What is evaluated (queue `alerts`, task `alerts.scan`, every minute)

| Trigger | Before / after | Rerun guard |
| --- | --- | --- |
| A published event (not a scheduled `catalyst`) dated in the window, or a late one (below) | the event text (`is_material`) | `alert_trigger` row `event:<id>` |
| A published `rating` / `rating_summary` fact (same window and late rule) | the subject's previous rating fact | `alert_trigger` row `fact:<id>` |
| A model output: `status`, `cushion`, `price`, `mark_gap` | the last output the engine evaluated for that subject (`alert_state`); a subject seen for the first time starts from its newest output dated before the window (a baseline, not news) | `alert_state` hash; findings also write `alert_trigger` `model:<model>:<type>:<id>:<as_of>:<hash>` |

A live scan looks back 3 days for anything not yet evaluated, so an event approved in the review queue or a
model run reaches watchers within about a minute. Outputs dated before the evaluated one (a backtest run for
a past day) are never news.

Late arrivals: an event or rating fact dated up to 14 days back (`LATE_DAYS`) is still news when Marden learned
it (`learned_at`) after the engine's first evaluation (the oldest `alert_trigger.evaluated_at`; a backfill
counts): an 8-K filed four business days after the event, a candidate approved in the review queue days after
extraction. It is alerted live (timed when it fires, so it is in that day's digest) and dated on its own day.
Rows Marden learned before the engine first ran are history; before any evaluation exists, nothing older than
the 3-day window is evaluated. Anything dated more than 14 days back is not alerted.

## Rules (codes)

Materiality is `marden.models.materiality.is_material`, the same test the status model and the review queue
use (docs/methods/status-rules-v0.1.md): M1 projected completion moves 3+ months; M2 cushion turns negative;
M3 power denied or withdrawn; M4 tenant notice (force majeure, termination, contract cut); M5 lender action;
and a chain link changing status (E1/E2, T1/T2, D1/D2, L1/L2; none <-> green is not news). The engine adds:

| Code | When | Alert type | Severity |
| --- | --- | --- | --- |
| C3 | cushion falls under 3 months (and was not already) | cushion | amber |
| CX | cushion back above 0 or above 3 months | cushion | green |
| PX | Marden price moved at least the user's threshold (1, 2 or 3 pts) from the price at that threshold's last alert | price_or_mark | amber down, green up |
| MK | fund marks for a period after the newest the engine had seen (N-PORT, BDC); amber when any sits 5+ pts above the Marden price | price_or_mark | amber / green |
| RT | a rating fact that differs from the previous one on record | tenant | amber |
| OP | an `opposition` event (county vote, moratorium, lawsuit) | opposition | the collector's grade (red, amber); ungraded = green |

Severity of a material code is the one in `RULE_CODES` (red: M2, M3, M4, M5, E2, T2, D2, L2). A finding
with several codes takes the worst; its alert types are the union (a user gets it when any is on).

## Who gets it

Every member of every firm watching the affected name, directly or through the chain: a site reaches watchers
of the site and of the instruments financing it; an instrument, watchers of it and its site; an entity, the
sites it is a chain party of and the instruments it issues; a campus, building or plant, the sites linked to
it by an approved site link; an obligation, its site and instruments. A member qualifies when the firm's
`marden` product is live (`org_product` active and within its dates) and they hold a role on it (the API's
grant test), and their alert settings take one of the finding's types (price moves: at their threshold).
The body ends with the watched names the alert reached them through.

One alert row per (org, user, matter): `alert` is unique on `(org_id, user_id, dedupe_key)`; the key is the
trigger key plus the link role or price threshold. A material event repeating a matter already alerted to
that user (same subject and rule within 7 days, e.g. five articles on one force majeure notice) is added to
that alert's sources instead of becoming a new alert. Each new alert emits a stream event `{type: 'alert',
alert: Alert}` scoped to that org and user; the web's `alertsPatch` puts it on top of the list in place.

## Email

| Kind | When | Holds | Idempotency key |
| --- | --- | --- | --- |
| `instant` | each scan, for red alerts of the last 24 h not yet in an instant email (users with `instantRed`) | the user's new red alerts | `instant:<org>:<user>:<first alert id>` |
| `daily_digest` | 07:00 ET (built at 07:03, two minutes' grace for in-flight scans) | the day's alerts not emailed and not in an instant email | `daily:<org>:<user>:<date>` |
| `weekly_digest` | Mondays 07:00 ET | the week's alerts, plus a link to that week's risk list when published | `weekly:<org>:<user>:<Monday>` |

Rows go to `email_outbox` (`queued` -> `sent` | `failed` | `not_configured` | `cancelled`). At send time the
member's access and settings are checked again (switched off or product lapsed: `cancelled`). The provider is
chosen by environment: `POSTMARK_TOKEN` (Postmark, server API) or `RESEND_API_KEY` (Resend), with
`ALERT_EMAIL_PROVIDER` deciding when both exist, plus `ALERT_FROM_EMAIL`, `ALERT_LINK_SECRET` and
`MARDEN_APP_URL`. Anything missing: the row becomes `not_configured` with the missing variable names (never a
value) and `email_provider_state` records it, so Account > Alerts says email is not set up. Network errors, 429
and 5xx retry after 1, 2, 4, 8 and 16 minutes, then `failed`; other 4xx fail at once. Resend dedupes on the
`Idempotency-Key` header; a worker dying mid-send can repeat a Postmark email (at least once). A sent email
marks its alerts `emailed_at` and re-sends them on the stream with `emailed: true`.

Each email has HTML and plain text: every alert's headline linked to its screen, rule, date, the body lines,
and up to five sources linked to the filings and articles; the footer has "Manage alert email"
(`/account?tab=alerts`) and two signed one-click unsubscribe links (this kind of email; all alert email), and
the `List-Unsubscribe` / `List-Unsubscribe-Post` headers (RFC 8058) carry the first.

### Unsubscribe

Token = base64url(JSON `{iat, o, s, u, v: 1}`) + "." + base64url(HMAC-SHA256(`ALERT_LINK_SECRET`, first
part)); scope `s` is `all`, `instant`, `daily` or `weekly`. Python signs (`marden.alerts.links`), the API
verifies (`apps/api/src/lib/unsubscribe-token.ts`); both test suites check one shared vector.
`GET /api/email/unsubscribe?token=` shows a confirm page and changes nothing (mail scanners follow links);
`POST` (the page's button, or a mail client's one-click POST) switches the scope's settings off
(`instantRed`, `dailyDigest`, `weeklyNote`), cancels the member's queued emails of those kinds and writes
`audit_log` `alert_settings.unsubscribe`; a second POST changes nothing. Without the secret on the API the
link answers 503 and says nothing was changed.

`GET /api/alert-delivery` (Account > Alerts) returns whether email is configured (as the worker last saw it),
the address, the caller's last 30 days of emails by status and the 10 newest; the missing variable names
are shown to staff only.

## Backfill

`python -m marden alerts scan --since-days 30 --dry-run` evaluates the window and writes alerts only: dated
on the day the change happened, `backfill = true`, landed read (history, not news for the badge), no stream
event, never emailed. The triggers it evaluated are not evaluated again by the live scan.

Run on the local database, 2026-10-21 (window 2026-09-21 to 2026-10-21; one firm, `dev`, watching 4 sites
and 2 instruments): 3,051 triggers (1,201 events, 415 rating facts, 1,435 model-output changes), 528 material.
Findings by rule (all names): PX 417, MK 252, L2 27, L1 18, M1 13, M4 10, T1 6, E1 5, OP 5, D1 3, M3 3,
D2 1, M2 1, T2 1. Alerts to the watching member: 31 (PX 6, L2 5, E1 4, L1 4, M1 3, M4 3, MK 2, D1 1, M2 1,
OP 1, T2 1; 9 red), with 6 repeat articles joined to an earlier alert. A second run wrote nothing.
Rerun at 23:06 UTC the same day, after the price, mark-gap, cushion and status models had re-run: 20 changed
model outputs evaluated, none material, no alert, no stream event, no email row (outbox empty).

On a new database (Neon) the backfill is optional: the worker starts before any firm holds the product or
watches a name (docs/DEPLOY.md section 7), so its first scans evaluate the recent rows with nobody to alert
and start the engine (the late-arrival line). A firm gets alerts for what comes in after it starts watching;
the history is on the screens, not in Alerts.

## Limits (v0.1)

- "No visible construction for 8 weeks" (`construction_stall`) fires only through D2 (construction halted, an
  event or the developer link); there is no satellite-based stall rule yet.
- Status changes are taken from consecutive outputs as they are. A past day's status output computed before
  its inputs existed (e.g. "no Marden price yet" before a price backfill) can show a link turning green and
  back; live daily runs read the latest inputs and do not do this. Rerun `python -m marden model status` for those days
  before a backfill to avoid it.
- Only `marden` product access is checked; per-product alert routing waits for a second product.
