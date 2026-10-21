# Build status and handoff (2026-10-21, ~16:00 ET)

Branch `claude/modest-bardeen-tkbmaq` in both `marketsights/marden-backend` and `marketsights/marden-front-end`. Commits labelled "WIP: snapshot" are unreviewed agent work saved so nothing is lost; reviewed work has descriptive commit messages.

## Done and reviewed
- Core schema (fact store, read models, queue, stream), Python core, Fastify API with every endpoint, SSE, Clerk auth, default-deny permissions (`docs/ARCHITECTURE.md`).
- Neon wiring: pooled `DATABASE_URL` for API queries, direct `DATABASE_URL_UNPOOLED` for workers/queue/LISTEN/migrations. Neon project `core` (orange-grass-89481271), branch `production`, database `app`, role `app_rw`, compute 0.25–2 CU. Migrations not yet applied on Neon (this sandbox cannot open TCP to Postgres; run `dbmate up` from Railway).
- Collectors (structured): rates/SOFR/FRED, SEC index, N-PORT, BDC, XBRL, EIA-860M, ERCOT large load, Sentinel-2, FERC — see `docs/collectors.md` when its review lands.
- Grid coverage: all US interconnections, 7 ISO/RTOs, 81 BAs, utility territories; queues PJM, NYISO, ISO-NE, SPP, CAISO, BPA, El Paso Electric; LBNL fallback; MISO blocked by Cloudflare (not bypassed) — `docs/grid-coverage.md`.
- Capacity auctions: PJM, ISO-NE, NYISO, MISO, capacity zones per site.
- Web: design system, shell, all routes, Clerk login (ported from frontend-app-core), permission gates, Completion, Sites, Site detail (all tabs), Data centers, On-site power, Instruments, Instrument detail, Account.
- Methods: `docs/methods/pricing-v0.1.md`, `price-sources.md`, `licensing.md`.

## Running when this was written (check with the workflow journals; rerun if lost)
| Workflow | What | Script |
| --- | --- | --- |
| marden-seed-load | seed loaders waves 2–3 + verify (sites, instruments done) | `~/.claude/projects/.../workflows/scripts/marden-seed-load-*.js` |
| marden-collectors-a | remaining structured collectors + review | `marden-collectors-a-*.js` |
| marden-market-price-pulls | FINRA TRACE / Börse Frankfurt / Yahoo daily pulls + legacy loaders (scratch DB `marden_prices`) | `marden-market-price-pulls-*.js` |
| marden-product-access-and-ercot | per-product entitlements + roles (backend, web), ERCOT prices, ERCOT GIS from MIS | `marden-product-access-and-ercot-*.js` |
| marden-models | status, cushion, price engine, mark gap/recovery/GPU, stress/contagion/risk/payer/leases/capacity, backtest | `marden-models-*.js` |
| marden-read-models | builders for every view + end-to-end API review | `marden-read-models-*.js` |

If a new session starts: read this file, check `git log`, then re-run any workflow whose review did not finish (the scripts are under the session's `workflows/scripts/`; otherwise re-create from the task descriptions above).

## Next steps (in order)
1. After the seed load: run `seed legacy_trace`, `legacy_frankfurt`, `legacy_yahoo`, then `collect finra_trace`, `boerse_frankfurt`, `yahoo_equity` on the main DB (`docs/methods/licensing.md`).
2. Resolve the 7 grid-region review items from filings (Abernathy, Childress, Helios 1/2, Polaris Forge 1/B4, Stingray).
3. Extraction workers (Claude API; claude CLI fallback for local runs), alert engine + email (Postmark or Resend), weekly note, review queue UI.
4. Remaining screens: Home, Customize Home, Payers, Payer, Stress, Risk list, Watchlist, Track record, Obligations, GPUs, Leases.
5. Deploy: Dockerfiles + Railway config (`web`, `api`, `worker`), `dbmate up` on Neon (strip `channel_binding` for dbmate), smoke tests, then re-run `seed all` + collectors against Neon.

## Owner decisions on record
- Build directly (not factory.py); backend must run on real data; no demo data served.
- Facility/debt coverage and pricing are equal priority.
- Collect FINRA TRACE (finra.org), Börse Frankfurt, Yahoo, SPP, El Paso Electric and ERCOT MIS despite noncommercial terms/robots; never evade bot protection; everything licence-flagged (`MARDEN_SHOW_UNLICENSED_PRICES`).
- Clerk: share the marden.ai production instance with the other app; Marden permissions live in Marden's DB per product (no Clerk roles).
- Hosting: Railway Hobby now (Pro once a fund uses it); Neon Postgres only; R2 for raw files and backups; no Redis, no Neon Functions/Realtime/AI gateway.
- Names: Neon `core`/`app`/`app_rw`; R2 `core-raw`, `core-snapshots`, `core-backups`; Railway project `core` with `web`, `api`, `worker`.

## Credentials (values are NOT in git)
Stored only in this container under `/root/.config/marden/` (lost on reset). Add them as environment variables in the cloud environment settings and in Railway: `DATABASE_URL`, `DATABASE_URL_UNPOOLED` (app_rw, from Neon), `NEON_API_KEY`, `CLERK_SECRET_KEY`, `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` (+ dev-instance keys), `ERCOT_API_KEY`, `ERCOT_API_USERNAME`, `ERCOT_API_PASSWORD`. Still needed: `ANTHROPIC_API_KEY`, `R2_*`, `RAILWAY_TOKEN`, `POSTMARK_TOKEN` or `RESEND_API_KEY`, `FRED_API_KEY`. Rotate the keys that were pasted into chat (Clerk secret, Neon owner password and API key, ERCOT primary key and password).

## Inputs outside git
The seed archive `marden_data_2026-10-20.zip` (re-attach if the container resets; set `MARDEN_SEED_DIR` to its extracted folder). The handoff package is in `handoff/` in both repos.
