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

## Stopped by the org spend limit (2026-10-21 ~16:30 UTC) — re-run after the limit resets/raises
Finished before the limit: seed load (all loaders + verify), structured collectors (all 9 built and run live; only their review failed),
grid coverage, capacity auctions, web screens + auth review, per-product access (backend + web built; ERCOT parts and review failed),
price pulls (built; finra.org refuses non-browser clients, so live TRACE needs the Rule 7730 licence; Frankfurt + Yahoo work; review failed),
read models: instruments area only (instruments, instrument_detail, curve, gpu_collateral).
Re-run with Workflow({scriptPath, resumeFromRunId}) so finished agents replay from cache:
| Workflow | Run id | What still needs to run |
| --- | --- | --- |
| marden-models | wf_3e2f467f-9d1 | all 6 model agents + review (status model was mid-way: framework, rules done) |
| marden-read-models | wf_50c221f6-5dd | sites, facilities, payers, monitor builders + end-to-end review |
| marden-product-access-and-ercot | wf_5a24c3b6-9b2 | ercot:prices, ercot:gis, review |
| marden-collectors-a | wf_c66027de-835 | collectors review |
| marden-market-price-pulls | wf_af9f5953-8a8 | review; then run legacy_trace/legacy_frankfurt/legacy_yahoo on the main DB |
Known open items from the reports: FRED ICE BofA series collected by owner decision but FRED was in maintenance on 2026-10-21 (retry scheduled); Treasury + SOFR loaded;
tests/test_worker.py needs updating for the new models.* and collect.* tasks; Yahoo robots.txt disallows all (owner decision covers it);
API should return 404 (not 503) for collateral on non-GPU instruments.

## Spend limit hit again (2026-10-21 ~21:50 UTC); resumed 22:10 UTC
Resumed with Workflow({scriptPath, resumeFromRunId}): marden-saas-live-v2 (wf_c6559cea-094; deploy built; alerts, agents core/ops, reviews, integration re-running), marden-web-remaining-screens (wf_6239c834-ac7; 6 groups built; reviews, GPUs+Leases build, final re-running), marden-models (wf_3e2f467f-9d1; all 6 models built; review re-running), marden-web-mockup-fidelity (wf_32d85200-a86; completion-sites, site-tabs-a, site-tabs-b audited; the rest re-running). Deferred to save CPU: marden-data-gaps (wf_5fca7cc9-15c): plant locations done and verified (23 of 74 placed, 27 counties); air permits build and the dc lastChange verify still to resume.
After a container restart: setsid pg_ctlcluster 16 main start; then the server supervisor (scratchpad ops/supervise.sh) restarts API :4000 and web :3000.

## Running as of 2026-10-21 ~18:45 UTC (re-run with Workflow({scriptPath, resumeFromRunId}) if lost)
| Workflow | Run id | What |
| --- | --- | --- |
| marden-web-remaining-screens | wf_6239c834-ac7 | build + review Home, Customize, Watchlist, Payers/Payer, Stress, Risk list, Track record, Obligations, GPUs, Leases; final route walk + build |
| marden-web-mockup-fidelity | wf_32d85200-a86 | element-by-element audit of every built screen/tab vs its mockup with real data; fix; verify; critic |
| marden-saas-live-v2 | wf_c6559cea-094 | Railway/Neon/R2 deploy setup; alert engine + email; spec-22 agent system on the Anthropic API (intake incl. upload API/CLI/R2 inbox, router, per-area specialist extractors with domain briefs, verifier, linker, approvals, eval set; then researcher, watchdog, note writer); reviews; end-to-end integration. Replaces wf_49c2d294-54c (stopped). Launched before the Anthropic key arrived, so its live proof may use the claude CLI fallback: run the live API proof with the key afterwards. |
| marden-readiness-a | wf_2ca3d0cf-311 | org roles Marden-owned (no Clerk roles; admins can't change owners); resolve the 7 grid-region review items |
| marden-data-gaps | wf_5fca7cc9-15c | power-plant locations/county/EIA match (all 74 had none), plant air permits from state records, dc lastChange ordering bug, favicon |
Shared local dev servers: API :4000 (dev auth), web :3000. FRED was in maintenance at 18:14 UTC; retry scheduled 20:16 UTC (ICE BofA OAS needed by the tier curves).
Done: mockup fidelity audit, all 8 groups (wf_32d85200-a86; one pass each). Running: marden-readiness-b (wf_8ebd131f-882): Claude reviews the 21 proposed site-campus links and approves clear ones (actor claude-review, audited, reversible); licence switch end to end; main-DB Yahoo/TRACE cleanup.
Done: status v0.3 (wf_b56a94d4-2cd): L1 also rate-neutral; today 6/12/11. Spread tightening checked: TeraWulf, Applied Digital, xAI AH1 and CoreWeave widening all supported by TRACE/N-PORT; real bug fixed: three instruments linked to another deal's issue comp (price_terms.link_comp). Open: Kalman filter lags steady sell-offs (Edged −88 bp, Helios II −52 bp) — calibration, not changed.
Done: ICE BofA spread proxy while FRED is down (wf_c4ed641c-d67): collector etf_oas reads fund OAS from iShares USHY (ICE BofA US HY Constrained, 307.7 bp), HYBB (BB, 200.7 bp), USIG (US Corporate, 84.6 bp); licence-flagged (BlackRock terms: personal non-commercial use, no robots) under the owner's standing decision; model input only. Tier curves +6.2 bp; no instrument price changed (none priced from the tier curve). BBB, single-B, CCC proxies not available. Open: the two riskiest tier levels are ~100 bp under the methodology targets (not an HY-input issue).
Done: status L2 rate-neutral (v0.2, wf_f56277df-295): today 6 Broken / 12 Strained / 11 Holding (was 11 / 10 / 8 on rate moves alone). Running: L1 gets the same spread test + check of large since-issue spread tightening (wf_b56a94d4-2cd). Disk filled once (Postgres crashed and recovered); stale scratch copies removed.
Done: models reviewed (wf_3e2f467f-9d1): look-ahead leaks fixed (XBRL filing dates, stress deal records, fund NAV lag), n/d never stored as 0, docs/models.md. Price validation: CoreWeave 5y CDS 10.3% mean error vs ~12% target (5 of 7 dates), leave-one-bond-out 0.81 pt vs 0.76 target, Jupiter first below 95 on 08-17 (94.30; target <95) and 94.42 on 09-18 (target 94.1), Jupiter 09-24 91.81 vs 89.0 (miss); higher tier levels 352/414 bp vs 462/505 targets (HY OAS input missing while FRED is down). Decided: L2 made rate-neutral (wf_f56277df-295) because the raw-price rule broke 11 of 29 chains on rate moves alone.
Done: read models end to end (wf_50c221f6-5dd): rebuild all 0 errors; 896 API calls, no 500s; JSON Schema from types.ts passes with n/d nulls allowed; 30 values traced to facts/model outputs; live path fact -> dirty -> rebuild -> SSE proven on a DB copy; fixed per-user alert privacy in the stream and alerts API.
Done: org roles Marden-owned (Clerk only for identity/membership/invites with org:member; admins can't manage owners; OrganizationSwitcher 'Manage' goes to /account) and all 16 grid-region review items on the 7 sites resolved from 8 SEC filings (wf_2ca3d0cf-311, reviewed). Open: apps/api stream-python test needs stream_event.user_id in the API test schema.
Done: structured collectors reviewed (wf_c66027de-835): all 11 idempotent on a second live run; N-PORT 22,720 holdings; new index 20261021200000_fact_subject_known (lookups 157 ms -> 0.26 ms); FERC retries Cloudflare 52x; N-PORT fx_convention now tag M.
Done: live updates (patchOn/mergeOn, structural sharing; e2e/live-updates.mjs; 23/23 screens load real data or an honest not-built state).
Done: per-product access + ERCOT prices/load zones + ERCOT GIS from MIS (wf_5a24c3b6-9b2, reviewed). Decided: trial orgs get no product until staff grant it; one shared permission list is fine until a second product ships.
Done since the table below: price pulls reviewed and fixed (commit 454271d); FRED ICE series unblocked (7ef0e37). Open: clean Yahoo duplicates and the 107 intraday TRACE rows in the main DB after the price model run; rotate the Neon app_rw password (it was printed into a local agent log).

Goal set by the owner 2026-10-21: everything working today, selling to funds from 2026-10-22.
Release-readiness pass to run when the workflows above finish: mark the n/d-able fields nullable in types.ts (handoff/api/types.ts, web src/lib/types.ts, API contracts) — instrument chainStatus/fundMarks/nextMarksPublic, plant lat/lon/county/technology/airPermit/permit/fuel and unit oem/model/fuel/permitRef/totalMw, completion driver, schedule projection, project-file drawPaceFlag and equipment late — and make every screen render them as n/d (decided: null = n/d wins over the old non-null types); wire MARDEN_SHOW_UNLICENSED_PRICES into API/read models/models; clean the main-DB Yahoo duplicates and intraday TRACE rows; fix tests/test_worker.py for the new tasks; API 404 (not 503) for collateral on non-GPU instruments; ~~resolve the 7 grid-region review items~~ (done, next step 2); review-queue UI (staff); weekly note; full suites; route walk of every screen with real data; deploy per docs/DEPLOY.md once the owner adds credentials.

### Earlier runs
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
2. ~~Resolve the 7 grid-region review items from filings (Abernathy, Childress, Helios 1/2, Polaris Forge 1/B4, Stingray).~~ Done 2026-10-21: `collect grid_evidence` settled all 16 site items (and 17 on their campuses/buildings) from 8 SEC filings (docs/grid-coverage.md section 1).
3. Extraction workers (Claude API; claude CLI fallback for local runs), alert engine + email (Postmark or Resend), weekly note, review queue UI.
4. Remaining screens: Home, Customize Home, Payers, Payer, Stress, Risk list, Watchlist, Track record, Obligations, GPUs, Leases.
5. Deploy: Dockerfiles + Railway config (`web`, `api`, `worker`), `dbmate up` on Neon (strip `channel_binding` for dbmate), smoke tests, then re-run `seed all` + collectors against Neon.

## Owner decisions on record
- Build directly (not factory.py); backend must run on real data; no demo data served.
- Facility/debt coverage and pricing are equal priority.
- Collect FINRA TRACE (finra.org), Börse Frankfurt, Yahoo, SPP, El Paso Electric and ERCOT MIS despite noncommercial terms/robots; never evade bot protection; everything licence-flagged (`MARDEN_SHOW_UNLICENSED_PRICES`).
- Clerk: share the marden.ai production instance with the other app; Marden permissions live in Marden's DB per product (no Clerk roles).
- Hosting: Railway paused all builds/deploys on 2026-10-21 (major outage), so the first deploy goes to Render via `render.yaml` (core-api, core-worker, core-web; ~$39/month; docs/DEPLOY-render.md). Same Docker images; Railway config kept for later. Originally: Railway Hobby now (Pro once a fund uses it); Neon Postgres only; R2 for raw files and backups; no Redis, no Neon Functions/Realtime/AI gateway.
- Names: Neon `core`/`app`/`app_rw`; R2 `core-raw`, `core-snapshots`, `core-backups`; Railway project `core` with `web`, `api`, `worker`.

## Credentials (values are NOT in git)
Stored only in this container under `/root/.config/marden/` (lost on reset); `anthropic.env` there holds `ANTHROPIC_API_KEY` (verified 2026-10-21 against /v1/models): source it in the same shell command that needs it, never print it. Add them as environment variables in the cloud environment settings and in Railway: `DATABASE_URL`, `DATABASE_URL_UNPOOLED` (app_rw, from Neon), `NEON_API_KEY`, `CLERK_SECRET_KEY`, `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` (+ dev-instance keys), `ERCOT_API_KEY`, `ERCOT_API_USERNAME`, `ERCOT_API_PASSWORD`. Still needed: `R2_*`, `RAILWAY_TOKEN`, `POSTMARK_TOKEN` or `RESEND_API_KEY`, `FRED_API_KEY`. Rotate the keys that were pasted into chat (Clerk secret, Neon owner password and API key, ERCOT primary key and password).

## Inputs outside git
The seed archive `marden_data_2026-10-20.zip` (re-attach if the container resets; set `MARDEN_SEED_DIR` to its extracted folder). The handoff package is in `handoff/` in both repos.
