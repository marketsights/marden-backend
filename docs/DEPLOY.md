# Deploying Marden (Railway + Neon + R2)

Owner decisions (docs/STATUS.md): Railway Hobby now (Pro once a fund uses it), one Railway project `core` with three services `web`, `api`, `worker`; Neon Postgres only (project `core`, branch `production`, database `app`, role `app_rw`); Cloudflare R2 for raw files and backups (buckets `core-raw`, `core-snapshots`, `core-backups`); no Redis, no Neon Functions / Realtime / AI gateway; email via Postmark or Resend.

Everything below is config as code in the two repositories. Nothing has been deployed from this sandbox (it has no Railway token and cannot open TCP connections to Neon). The images were built and run locally (see "What was proved locally" at the end).

| Service | Repository | Config file | Image | Start command | Public |
| --- | --- | --- | --- | --- | --- |
| `api` | marden-backend | `/apps/api/railway.json` | `apps/api/Dockerfile` (context: repo root) | `node --enable-source-maps dist/server.js` | yes: `api.marden.ai` (webhooks, health) |
| `worker` | marden-backend | `/services/railway.json` | `services/Dockerfile` (context: repo root) | `python -m marden worker` | no |
| `web` | marden-front-end | `/railway.json` | `Dockerfile` | `node server.js` (Next standalone) | yes: `app.marden.ai` |

The `api` and `worker` services both run `marden-migrate` (dbmate) as their **pre-deploy command**, so a push that adds a migration applies it before the new code takes traffic. Two concurrent runs are safe: dbmate holds a Postgres advisory lock and the second run finds nothing to apply.

## 1. Neon (once)

1. Neon console → project `core` → branch `production` → database `app`, role `app_rw`. **Rotate the `app_rw` password first** (it was printed into a local agent log; STATUS.md).
2. Connect → copy two connection strings for `app_rw` / `app`:
   - **pooled** (host contains `-pooler`) → `DATABASE_URL` (API queries only);
   - **direct** (pooling off) → `DATABASE_URL_UNPOOLED` (migrations, the worker and its queue, the API's LISTEN for the live stream, pg_dump). PgBouncer's transaction mode breaks LISTEN, advisory locks and pg_dump, which is why the direct endpoint exists.
   Keep `sslmode=require`. Neon adds `channel_binding=require`: libpq (worker, pg_dump) uses it; `marden-migrate` strips it for dbmate, whose driver would otherwise send it to the server as a setting and fail.
3. Compute: 0.25–2 CU autoscaling. Scale-to-zero is fine: `/healthz` waits up to 5 s for a waking compute, and the worker reconnects.
4. Keep Neon's point-in-time restore window (history retention) at the plan's maximum: it is the fastest restore for a recent mistake (section 9).

## 2. Cloudflare R2 (once)

1. Cloudflare dashboard → R2 → Create bucket, three times: `core-raw` (every fetched file, raw-first rule), `core-snapshots` (reserved for the read-model snapshot archive; no code writes it yet), `core-backups` (nightly `pg_dump`). Location hint: Eastern North America. Public access: **off** on all three.
2. R2 → Manage API tokens → Create **Account API token** (or a user token): permission **Object Read & Write**, restricted to these three buckets. Copy the **Access Key ID** and **Secret Access Key** (shown once) and the **Account ID** (R2 overview page). The S3 endpoint the code derives is `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`.
3. `core-backups` → Settings → Object lifecycle rules:
   - "Delete objects" after **90 days** for prefix `pg/` (about three months of nightly dumps; change to taste);
   - "Abort incomplete multipart uploads" after **1 day** (a dump interrupted mid-upload leaves parts behind).
   The backup code never deletes; retention is only this rule.
4. `core-raw`: no lifecycle rule. Raw files are evidence and are kept.

Without all three R2 variables the worker still runs: the raw store refuses local disk on Railway (a container's disk does not survive a deploy), so collectors record a failed run that names the missing variables, and the nightly backup is recorded as `not_configured`. Nothing pretends to succeed.

## 3. Railway project and services (once)

1. railway.com → New project → name it `core` (Hobby plan). It starts with environment `production`; add `staging` later (spec 21) by duplicating it.
2. **api**: New → GitHub repo → `marketsights/marden-backend`, branch `main` (until then: `claude/modest-bardeen-tkbmaq`). Service name `api`. Settings:
   - Source → Root directory: leave **empty** (repo root: the Dockerfile needs the pnpm workspace lockfile and `db/migrations`).
   - Config as code → Railway config file: `/apps/api/railway.json`.
   - Networking → Public networking → Custom domain `api.marden.ai` (or "Generate domain" first). Private networking is on by default: `api.railway.internal`.
3. **worker**: New → same repo and branch, service name `worker`, root directory empty, config file `/services/railway.json`. No public domain.
4. **web**: New → GitHub repo `marketsights/marden-front-end`, service name `web`, root directory empty, config file `/railway.json` (the default). Custom domain `app.marden.ai`.
5. Variables: section 4. Set them **before** the first deploy: a web deploy without Clerk keys fails its healthcheck (fail closed), and an API without them refuses to start.
6. DNS (wherever marden.ai is hosted): add the two CNAME records Railway shows for `app` and `api` (if the zone is on Cloudflare, start with the proxy off, "DNS only", until Railway has issued the certificates).

What the config files set (all three are validated against Railway's schema in the tests):

| | api | worker | web |
| --- | --- | --- | --- |
| builder / Dockerfile | `apps/api/Dockerfile` | `services/Dockerfile` | `Dockerfile` |
| watch patterns (what triggers a deploy) | `apps/api/**`, `db/migrations/**`, `scripts/migrate.sh`, workspace lockfiles | `services/**`, `db/migrations/**`, `scripts/migrate.sh` | every push |
| pre-deploy | `marden-migrate` (timeout 900 s) | `marden-migrate` (timeout 900 s) | none |
| healthcheck | `GET /healthz` (200 only when the database answers), 120 s | none (no HTTP server) | `GET /healthz` (200 only when Clerk sign-in is configured), 120 s |
| restart policy | on failure, 10 retries | on failure, 10 retries | on failure, 10 retries |
| overlap / draining | 20 s / 30 s (the API drains in ≤ 20 s) | 0 s / 120 s (the worker drains in ≤ 90 s) | 20 s / 30 s |
| sleep when idle | off | off | off |

On Pro, consider `restartPolicyType: ALWAYS` for the worker.

## 4. Environment variables

Credentials live only in Railway variables (and Neon / R2 / Clerk dashboards), never in git or logs. Use **shared variables** (project Settings → Shared variables) for values two services need and reference them with `${{shared.NAME}}`; reference another service's values with `${{api.NAME}}`. Every name below was found by reading `apps/api/src/config.ts` (+ `queries/stress.ts`, `lib/health.ts`), `services/marden/core/config.py`, `services/marden/alerts/settings.py`, `services/marden/core/logs.py`, `services/marden/worker.py`, `scripts/migrate.sh`, and the web app's `next.config.ts`, `src/lib/api-url.ts`, `src/lib/auth/auth-mode.ts`.

### api

| Variable | Value | Required | Notes |
| --- | --- | --- | --- |
| `NODE_ENV` | `production` | set by the image | production refuses dev auth and requires both Clerk keys |
| `DATABASE_URL` | `${{shared.DATABASE_URL}}` (Neon **pooled**) | yes | |
| `DATABASE_URL_UNPOOLED` | `${{shared.DATABASE_URL_UNPOOLED}}` (Neon **direct**) | yes | the stream's LISTEN and the pre-deploy `marden-migrate` |
| `MARDEN_AUTH_MODE` | `clerk` | default | `dev` is refused when NODE_ENV is not development/test **and** on any Railway service (`RAILWAY_ENVIRONMENT*` / `RAILWAY_PROJECT_ID` set) |
| `CLERK_SECRET_KEY` | Clerk production `sk_live_…` | yes | |
| `CLERK_PUBLISHABLE_KEY` | Clerk production `pk_live_…` | yes | same value as the web's `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`; the issuer is derived from it |
| `CLERK_WEBHOOK_SIGNING_SECRET` | `whsec_…` from the webhook endpoint (section 6) | yes for sync | without it `/api/webhooks/clerk` answers 503 `webhook_secret_not_configured` |
| `CLERK_JWT_KEY` | PEM public key (Clerk → API keys → JWT public key), `\n` for newlines | optional | networkless token verification |
| `WEB_ORIGIN` | `https://app.marden.ai` | yes | the token's authorized party (`azp`) and the CORS allow-list; comma-separated for several |
| `TRUST_PROXY` | `1` | yes | one proxy hop (Railway's edge appends the client address). Next's rewrite forwards `X-Forwarded-For` unchanged, so requests through the web app key on the same client address. Never `true` (a client could pick its own rate-limit key) |
| `PORT` | `4000` | recommended | set it explicitly so `API_ORIGIN` on the web can reference `${{api.PORT}}`; Railway routes the public domain to it |
| `HOST` | `::` | set by the image | IPv6 + IPv4 (Railway's private network is IPv6) |
| `RATE_LIMIT_PER_MINUTE` | `600` | default | per signed-in user, else per client address |
| `STREAM_HEARTBEAT_MS` | `25000` | default | keep **below 30000**: Next's `/api` rewrite proxy drops a connection idle for 30 s |
| `STREAM_RECHECK_MS` | `600000` | default | SSE session re-check (spec 23: at least every 10 min) |
| `SHUTDOWN_TIMEOUT_MS` | `20000` | default | SIGTERM drain budget; must stay below `drainingSeconds` (30 s) |
| `LOG_LEVEL` | `info` | default | JSON logs (pino); `authorization`, `cookie` and `svix-signature` headers are redacted |
| `MARDEN_STRESS_WAIT_MS` | `20000` | default | how long POST /api/stress/run waits for a custom run |
| `MARDEN_MIGRATE_WAIT`, `MARDEN_MIGRATE_LOCK_TIMEOUT` | `60s`, `10m` | defaults | `marden-migrate` waits for the database / for a concurrent migration |
| `RAILWAY_GIT_COMMIT_SHA` | set by Railway | — | shown (12 characters) by `/healthz` |

### worker

| Variable | Value | Required | Notes |
| --- | --- | --- | --- |
| `DATABASE_URL` | `${{shared.DATABASE_URL}}` (pooled) | yes | must be set; every worker connection (pool, queue, LISTEN) actually uses `DATABASE_URL_UNPOOLED` when it is set (`require_session_database_url`) |
| `DATABASE_URL_UNPOOLED` | `${{shared.DATABASE_URL_UNPOOLED}}` | yes | queue LISTEN, pg_dump, `marden-migrate` |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | section 2 | yes | raw store and backups; without them: failed collector runs and `not_configured` backups (never local disk on Railway) |
| `R2_BUCKET_RAW` | `core-raw` | default | |
| `R2_BUCKET_BACKUPS` | `core-backups` | default | |
| `SEC_USER_AGENT` | `Marden research cody@marden.ai` | default | SEC fair-access policy: a contact address |
| `ANTHROPIC_API_KEY` | Claude API key | for extraction / agents | without it the agents report `not_configured` and nothing is enqueued (a deployed service never uses the local `claude` CLI fallback) |
| `MARDEN_AGENT_*` (`BACKEND`, `CONCURRENCY`, `DAY_TOKENS`, `RUN_TOKENS`, `SOURCE_CAP(S)`, `MAX_SECTIONS`, `SECTION_CHARS`, `FILES_API_MIN_BYTES`, `CLI_TIMEOUT`) | defaults | optional | agent backend choice, concurrency and token budgets: `services/marden/agents/config.py` and docs/agents.md |
| `FRED_API_KEY` | FRED key | for FRED series | ICE BofA OAS, Treasuries via FRED |
| `ERCOT_API_KEY`, `ERCOT_API_USERNAME`, `ERCOT_API_PASSWORD` | ERCOT Public API subscription key and account | for ERCOT GIS / API collectors | all three or the collector records "needs key" |
| `FINRA_CLIENT_ID`, `FINRA_CLIENT_SECRET` | FINRA API | only once TRACE is licensed | |
| `MARDEN_SHOW_UNLICENSED_PRICES` | `true` | default | `false` hides every source flagged unlicensed-commercial (docs/methods/licensing.md) |
| `ALERT_EMAIL_PROVIDER` | `postmark` or `resend` | when both keys are set | |
| `POSTMARK_TOKEN` or `RESEND_API_KEY` | server token / API key | for alert email | without a key, sending records "not configured" |
| `ALERT_FROM_EMAIL` | a sender verified with the provider, e.g. `alerts@marden.ai` | for alert email | |
| `ALERT_LINK_SECRET` | 32+ random characters (`openssl rand -base64 48`) | for alert email | signs one-click unsubscribe links |
| `MARDEN_APP_URL` | `https://app.marden.ai` | for alert email | links in emails |
| `MARDEN_LOG_FORMAT` | `json` | set by the image | one JSON object per line; credential values, URL passwords and secret query parameters are masked |
| `MARDEN_LOG_LEVEL` | `INFO` | default | |
| `MARDEN_WORKER_SHUTDOWN_TIMEOUT` | `90` | default | seconds running jobs get after SIGTERM; must stay below `drainingSeconds` (120) |
| `MARDEN_SEED_DIR` | — | only for `seed` | not set on Railway (section 7) |
| `MARDEN_RAW_DIR` | `/tmp/marden-raw` | set by the image | scratch only; ignored once R2 is configured |

### web

| Variable | Value | Required | Notes |
| --- | --- | --- | --- |
| `API_ORIGIN` | `http://${{api.RAILWAY_PRIVATE_DOMAIN}}:${{api.PORT}}` | yes, **at build and at start** | the `/api/*` rewrite is fixed when the image is built (the Dockerfile declares `ARG API_ORIGIN`, so Railway passes it to the build and the build fails without it); server-side prefetches read it at runtime. Changing it needs a redeploy (rebuild) |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | same `pk_live_…` as the API | yes | read at **runtime** only (no build arg), so the image holds no Clerk key |
| `CLERK_SECRET_KEY` | same `sk_live_…` as the API | yes | runtime only; never reaches the browser |
| `CLERK_JWT_KEY` | PEM public key | optional | |
| `PORT` | set by Railway | — | |
| `NODE_ENV`, `HOSTNAME`, `NEXT_TELEMETRY_DISABLED` | `production`, `::`, `1` | set by the image | |

Without both Clerk keys every page renders "Sign-in is not configured" and `/healthz` answers 503, so Railway never switches traffic to such a deploy. The development sign-in (no Clerk, API dev auth) exists only under `next dev` on a machine without `RAILWAY_*` variables.

## 5. Domains, same origin, CORS, cookies

- The browser only ever talks to `https://app.marden.ai`. `/api/*` on the web app is rewritten by Next.js to `API_ORIGIN` (the API's private address), so Clerk's `__session` cookie reaches the API on fetches, Excel downloads and the SSE stream with no CORS. The API accepts that cookie only on GET/HEAD; writes must carry `Authorization: Bearer` (CSRF).
- `https://api.marden.ai` is public for the Clerk webhook, uptime checks and `scripts/smoke.sh`. CORS there allows only `WEB_ORIGIN`; every data route needs a session. (Same-origin only is also possible: skip the API's public domain and point the Clerk webhook at `https://app.marden.ai/api/webhooks/clerk`, which the rewrite forwards byte for byte.)
- Cookies: Marden sets none of its own. Clerk's production cookies on `marden.ai` are `Secure`; the session cookie is short-lived and refreshed by Clerk's middleware.
- Headers: the API sends helmet's set (HSTS, `X-Content-Type-Options`, `default-src 'none'` CSP, `frame-ancestors 'none'`); the web sends HSTS, `X-Frame-Options: DENY`, `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`, and no `X-Powered-By`.
- Rate limits: 600 requests per minute per signed-in user (per client address before sign-in), on every route including `/healthz`.

## 6. Clerk production (shared marden.ai instance)

The owner decided to share the `marden.ai` production instance with the other app; Marden's permissions live in Marden's database (no Clerk roles).

1. Clerk dashboard → the production instance → API keys: copy `pk_live_…` and `sk_live_…` into the api and web variables above.
2. Domains: the instance's Frontend API is on `clerk.marden.ai` (DNS already set up for the other app). `app.marden.ai` is a subdomain of the instance's domain, so no satellite domain is needed. Paths: sign-in `/sign-in`, sign-up `/sign-up`, after sign-out `/signed-out` (the app passes these itself).
3. Webhooks → Add endpoint `https://api.marden.ai/api/webhooks/clerk`, events: `user.*`, `organization.*`, `organizationMembership.*`, `organizationInvitation.*`, `session.revoked`, `session.ended` (spec 23). Copy its signing secret into `CLERK_WEBHOOK_SIGNING_SECRET` on the api. Because the instance is shared, the endpoint also receives the other app's users and organizations; they get no Marden product until staff grant one (docs/access-control.md), so they see nothing.
4. Settings to confirm (spec 23): sign-up restricted (invitation only), organizations on, personal accounts off, MFA for owners/admins/staff.
5. `marden-front-end/scripts/clerk-setup.mjs` checks the instance settings (dry run by default) and prints the webhook URL from `MARDEN_API_ORIGIN`.

## 7. First deploy, in order

Install the Railway CLI on your machine (`npm i -g @railway/cli`, `railway login`, then `railway link` in each repository and pick project `core`).

1. **Variables** for all three services (section 4).
2. **api** → Deploy. Its pre-deploy runs `marden-migrate` against Neon: watch the deploy log for `migrate: dbmate migrate (N migration files …)` and one `Applied:` line per migration. `/healthz` must turn 200. Check: `railway ssh --service api -- marden-migrate status` shows `Pending: 0`.
3. **Seed** (`seed all`: sites, instruments, holdings, facilities, cases) from your machine, because the seed archive `marden_data_2026-10-20.zip` is not in any image. `railway run` injects the worker's variables (Neon direct URL, R2) into a local process:
   ```sh
   unzip marden_data_2026-10-20.zip -d ~/marden-seed
   cd marden-backend/services
   railway run --service worker -- env MARDEN_SEED_DIR=~/marden-seed uv run python -m marden seed all
   ```
   It prints each loader's exit code and the table counts at the end; any non-zero exit stops it. Then the legacy price loaders (docs/methods/licensing.md): `seed legacy_trace`, `seed legacy_frankfurt`, `seed legacy_yahoo`, the same way.
4. **worker** → Deploy. From now on the schedules run by themselves (every collector at its cadence, models every minute on dirty inputs and on their daily/weekly clocks, read models every 30 s, nightly rebuild, nightly backup).
5. **Collectors once now**, so the first day has data without waiting for each cadence. Inside the worker container (its own network and R2 credentials):
   ```sh
   railway ssh --service worker
   python -m marden collect --help        # every collector
   python -m marden collect <source>      # one at a time, in docs/collectors.md order
   python -m marden sources               # heartbeat table: last_ok_at / last_error per source
   ```
   A source that is blocked or missing a credential prints and records why; that is expected (e.g. FINRA TRACE without a licence).
6. **Models once now**, in dependency order: `python -m marden model --help` lists them; run `status`, `cushion`, `price`, then the collateral and holder models (`mark_gap`, `gpu_index`, `gpu_model`, `gpu_collateral`, `gpu_deployment`, `recovery`), then `obligations`, `payer_capacity`, `leases`, `capacity`, `stress`, `risk_list`, `track_record` (docs/ARCHITECTURE.md, "Queue and schedule").
7. **Read models**: `python -m marden rebuild all`. Every view prints `built / changed / errors`; the command exits 1 if any builder failed.
8. **web** → Deploy. `/healthz` must say `{"ok":true,"auth":"clerk"}`.
9. **Smoke test** from your machine:
   ```sh
   cd marden-backend
   scripts/smoke.sh --api https://api.marden.ai --web https://app.marden.ai
   # signed-in checks too: copy a session token from a signed-in tab's console
   #   await window.Clerk.session.getToken()
   SMOKE_TOKEN='<token>' scripts/smoke.sh --api https://api.marden.ai --web https://app.marden.ai
   ```
   It checks `/healthz` (database reachable), that `/api/me`, `/api/sites` and `/api/stream` refuse requests without a session (dev auth off), CORS, security headers, that `/` redirects to sign-in, that `/api/*` through the web app reaches the API, and with a token that `/api/me` answers 200 and `/api/stream` opens an event stream. Exit 0 = all passed.
10. Sign in as staff and grant the first fund its product (docs/access-control.md).

Later deploys: push to the branch. The api and worker migrate in their pre-deploy step (only when their watch patterns match), Railway waits for `/healthz`, then sends SIGTERM to the old deployment: the API stops accepting connections, closes SSE streams (clients reconnect to the new deploy and replay from `Last-Event-ID`) and finishes in-flight requests within 20 s; the worker stops fetching jobs and gives running ones 90 s. A job killed anyway (crash, SIGKILL) is requeued within 5 minutes by `queue.retry_stalled`.

## 8. Operations

- Logs: Railway → service → Logs. API and worker log one JSON object per line; filter by `level` or the API's `reqId` (also returned as `x-request-id`).
- Source health: `python -m marden sources` in the worker, or `GET /api/sources/health` (the app's sidebar footer turns amber when a source is late).
- Backups: `python -m marden backup list` (latest runs: ok / failed / not_configured with the reason). The nightly job (`ops.backup_db`, 07:23 UTC) streams `pg_dump --format=custom` of the direct endpoint straight into `r2://core-backups/pg/<db>/<YYYY>/<MM>/<db>-<timestamp>.dump`, records size and sha256 in `backup_run`, and aborts the upload if pg_dump fails. Queue tables are dumped without their rows. Run one now with `python -m marden backup run`.
- The worker image's `pg_dump` is PostgreSQL 18 (build arg `PG_CLIENT_MAJOR`); it must be at least the Neon server's major version.

## 9. Restore

Pick the restore that fits:

- **A recent mistake (minutes to days)**: Neon's point-in-time restore. Neon console → Branches → `production` → Restore (or create a branch from a past time), check it, then switch. Fastest; no dump involved.
- **Older than Neon's history window, or Neon itself is gone**: the R2 dumps, below.

Restore an R2 dump into a fresh database (never over `app`), from inside the worker container, which has `pg_restore` 18 and the R2 credentials:

1. Neon console → `production` branch → Databases → New database `restore_check`, owner `app_rw`. Copy its **direct** connection string.
2. ```sh
   railway ssh --service worker
   python -m marden backup list                         # pick a run, or use 'latest'
   python -m marden backup fetch latest /tmp/restore.dump
   # -> fetched r2://core-backups/pg/app/… : sha256 matches backup_run, N archive entries
   RESTORE_URL='<direct URL of restore_check>'          # paste; do not echo it
   pg_restore --no-owner --no-privileges --exit-on-error --jobs 2 --dbname "$RESTORE_URL" /tmp/restore.dump
   ```
   (`fetch` refuses a key that does not look like a backup key and stops if the sha256 does not match the one recorded at backup time.)
3. Check it: `DATABASE_URL_UNPOOLED="$RESTORE_URL" marden-migrate status` shows `Pending: 0`; compare a few counts with production (`SELECT count(*) FROM fact`, `… FROM read_model`, `… FROM source_run`). Queue tables come back empty by design.
4. Only in a real disaster: point `DATABASE_URL` / `DATABASE_URL_UNPOOLED` of api and worker at the restored database (Neon: rename databases or create a new branch from it), redeploy both, run `scripts/smoke.sh`.
5. Otherwise drop `restore_check`. Do this whole drill monthly (spec 21, rule 9: "backups you've tested").

## 10. Still needed from the owner

| What | For | Without it |
| --- | --- | --- |
| `RAILWAY_TOKEN` or a logged-in Railway CLI | creating project `core` and deploying | nothing deploys (this file's steps are manual until then) |
| Rotated Neon `app_rw` password, pooled + direct URLs | api, worker | API refuses to start; worker cannot connect |
| Clerk production `pk_live_` / `sk_live_` and the webhook signing secret | api, web | API refuses to start; web shows "Sign-in is not configured" and fails its healthcheck |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (+ the three buckets) | worker | collectors fail their runs (raw-first needs R2), backups `not_configured` |
| `ANTHROPIC_API_KEY` | worker (extraction / agents) | agent jobs record "not configured" |
| `POSTMARK_TOKEN` or `RESEND_API_KEY`, `ALERT_FROM_EMAIL`, `ALERT_LINK_SECRET` | worker | in-app alerts only; email records "not configured" |
| `FRED_API_KEY`, ERCOT API key/account (and FINRA once licensed) | worker | those collectors record a missing credential |
| DNS CNAMEs for `app` and `api` | custom domains | use the `*.up.railway.app` domains meanwhile (set `WEB_ORIGIN` to the web's) |

## What was proved locally

See the deploy section of docs/STATUS.md for the latest run; the commands were: building all three Dockerfiles; `marden-migrate` from the image against an empty database (all migrations applied once, the second run applied nothing, a Neon-style `channel_binding=require` URL accepted); the worker image's start command for 60 s against a scratch database (jobs picked, periodic tasks run, backup recorded `not_configured`, SIGTERM drained and exited 0); the API image in production mode (fail-closed config, `/healthz`, 401s, SIGTERM drain); the web image without Clerk keys ("Sign-in is not configured", `/healthz` 503, `/api/*` reaching the API); and `scripts/smoke.sh` against those containers.
