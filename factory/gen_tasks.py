#!/usr/bin/env python3
"""Generate the Marden build task graph from the handoff package and the legacy code.

  python3 gen_tasks.py ../marden/handoff ../marden/legacy > tasks.json

Every screen, endpoint, collector, extractor, model, read model, port of existing
code and integration check becomes a task with an owner role, the files it may
touch, the gates it must pass and what it depends on. Agents can split large
tasks and add follow-ups while the factory runs.
"""
import json, re, sys
from pathlib import Path

handoff = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("../marden/handoff")
legacy = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("../marden/legacy")
T: list[dict] = []
py = lambda k: k.replace("-", "_")  # Python package folders must be importable


def task(id, title, role, phase, area, outputs, gates, deps=(), spec=(), inputs=(), params=None, priority=50,
         budget=8.0, timeout=45, attempts=3, needs_human=False):
    T.append({"id": id, "title": title, "role": role, "phase": phase, "area": area, "outputs": list(outputs),
              "gates": list(gates), "deps": list(deps), "spec": list(spec), "inputs": list(inputs),
              "params": params or {}, "priority": priority, "budget_usd": budget, "timeout_min": timeout,
              "max_attempts": attempts, "needs_human": needs_human})
    return id


H = "handoff"
G = f"{H}/specs/00-global.md"

# ------------------------------------------------------------------ phase 0: foundations (short critical path)
F00 = task("F00", "Monorepo scaffold, conventions and gate scripts", "architect", 0, "core", ["**"],
           ["files:scripts/gates/static.sh,scripts/gates/unit.sh,scripts/gates/py.sh,scripts/gates/contract.sh,scripts/gates/visual.sh,scripts/gates/e2e.sh,scripts/gates/data.sh,scripts/gates/migrate.sh,scripts/gates/perf.sh,scripts/gates/security.sh,scripts/gates/main_check.sh,docs/conventions.md"],
           spec=[f"{H}/README.md", G, f"{H}/specs/00a-product-spec.md", f"{H}/specs/21-tech-stack.md", f"{H}/specs/22-data-agents-models.md"], priority=0, budget=12, timeout=60,
           params={"brief": "pnpm workspaces (apps/web Next.js App Router, apps/api Fastify, packages/contracts, packages/ui), Python 3.12 services/ with uv or pip-tools, db/migrations (dbmate), scripts/gates/*.sh that run real lint/typecheck/tests and print readable failures, tests/ for Playwright visual+e2e, docs/conventions.md. main_check.sh = full suite. Keep every gate fast. Calling convention (the factory runs these from the worktree root; exit 0 = pass, print readable failures): static.sh (lint + typecheck + ruff/mypy on changed packages); unit.sh <area> (JS/TS tests related to files changed vs main, e.g. vitest related; <area> like web:01-home or api:sites is for logs); py.sh <path under services/> (pytest for that service, e.g. py.sh collectors/eia_860m); contract.sh <endpoint-slug>; visual.sh <screen-name>; e2e.sh <group>; data.sh <key|all>; migrate.sh (apply all migrations to a fresh throwaway database, then roll back the last); perf.sh all; security.sh all; auth.sh unit (spec 23 tests 1-3, no network) and auth.sh live (tests 4-5 against the real Clerk dev instance; fails if Clerk keys are missing). A gate whose target does not exist yet must fail with a clear message, never pass silently. static.sh must enforce, as errors not warnings: ESLint max-lines 300 per file, max-lines-per-function 80, complexity 15, max-depth 4, max-params 4, import/no-cycle, no-explicit-any, and layer boundaries (eslint-plugin-boundaries or dependency-cruiser): apps/web may import packages/* but never apps/api; apps/api routes -> services in apps/api/src/lib -> db access only in apps/api/src/db; packages/contracts imports nothing internal. Python: ruff with C901 max 12, PLR0913/PLR0915, isort, pep8-naming; mypy strict on services/common and services/models; import-linter contracts so collectors never import models and models never import collectors. TypeScript strict mode everywhere. docs/conventions.md must state the folder map, naming rules and these limits exactly, so every agent follows the same rules."})
F01 = task("F01", "Contracts package: types, JSON Schemas, zod validators, fixtures", "architect", 0, "contracts",
           ["packages/contracts/**"], ["static", "unit"], [F00], [f"{H}/api/types.ts", f"{H}/api/endpoints.md", f"{H}/fixtures", f"{H}/specs/13-account.md"], priority=1,
           params={"brief": "Also write packages/contracts/permissions.ts: the single access matrix. Roles: owner, admin, member, viewer (customer org, mapped from Clerk org:owner/org:admin/org:member/org:viewer per spec 23) and staff (Marden internal, from staff_users). Plans: trial, design_partner, paid. Permissions like sites:read, instruments:read, stress:run, watchlist:write, alerts:write, export:xlsx, account:manage, billing:manage, api_keys:manage, review:approve (staff only), ops:read (staff only). Map EVERY endpoint in endpoints.md to exactly one permission, a scope (global market data vs org-owned data) and the plans allowed. Export a test that fails if any endpoint in endpoints.md is missing from the matrix."})
F02 = task("F02", "Database schema and migrations (fact store, entities, all product tables, read-model tables)", "architect", 0, "db",
           ["db/**"], ["migrate"], [F00], [G, f"{H}/specs/17-dc-power-pipeline.md", f"{H}/specs/18-obligation-map.md", f"{H}/specs/19-gpu-collateral.md", f"{H}/specs/20-power-plant-debt.md"],
           params={"brief": "Tenancy: organizations, memberships (user, org, role), invitations, api_keys (hashed), audit_log (append-only). Every customer-owned table (watchlists, alerts, alert settings, saved stress scenarios, exports, API usage) has org_id NOT NULL, an index on it, and a Postgres row-level-security policy on current_setting('app.org_id'); market data tables are global and read-only to the API role. Separate DB roles for api (read market data, read/write org data under RLS), workers (write market data, no org data) and migrations. Also: facts (value, tag, source_doc, quote, valid_from, learned_at, method_version), documents (R2 key, hash, url, fetched_at), entities+aliases, review_queue, snapshots, sites/chain_links/milestones, instruments/holders/marks, campuses/buildings/site_link, plants/units/liens/plant_serves, obligation nodes/edges, gpu prices/facilities, users/watchlists/alerts/alert_settings, heartbeats, and one read-model table per screen. PostGIS for geometry. Point-in-time queries via valid_from/learned_at."},
           priority=1, budget=12, timeout=60)
F03 = task("F03", "Infrastructure as code: Neon, R2 buckets, Railway services, env, Sentry", "devops", 0, "infra",
           ["scripts/infra/**", "infra/**", "railway.json", "railway.toml", "**/Dockerfile", ".env.example"], ["static"], [F00], [f"{H}/README.md", f"{H}/specs/21-tech-stack.md"], priority=2)
F04 = task("F04", "Python service framework: fact store client, R2 raw store, queue, heartbeats, rate limits, review queue", "architect", 0, "services-common",
           ["services/common/**", "services/pyproject.toml"], ["py:common"], [F02], [f"{H}/specs/17-dc-power-pipeline.md", f"{H}/specs/22-data-agents-models.md"], priority=2,
           params={"brief": "No fact is written without source document, quote and tag; enforce in code. Procrastinate (Postgres) queue with per-source concurrency and rate limits; SEC <=10 req/s with SEC_USER_AGENT. Raw document saved to R2 before parse. Heartbeat table with expected cadence. Review queue API: material facts wait for approval."})
F05 = task("F05", "Design system package: tokens, Tailwind config, shared components", "frontend", 0, "ui",
           ["packages/ui/**"], ["static", "unit"], [F01], [f"{H}/design-system/tokens.css", f"{H}/design-system/tokens.json", f"{H}/design-system/components.md", f"{H}/design-system/tailwind.config.snippet.js"], priority=2)
F06 = task("F06", "Web app shell: sidebar, search, header, routing, fixture/live data layer, SSE client, states", "frontend", 0, "web-shell",
           ["apps/web/**/layout.tsx", "apps/web/lib/**", "apps/web/components/shell/**", "apps/web/package.json", "apps/web/next.config.*", "apps/web/tailwind.config.*", "apps/web/app/globals.css", "apps/web/middleware.ts"],
           ["static", "unit"], [F05], [G, f"{H}/screens/index.html", f"{H}/specs/23-auth-clerk.md"], priority=3,
           params={"brief": "Use @clerk/nextjs (clerkMiddleware on every app route, sign-in page, OrganizationSwitcher in the header; spec 23). Include a permissions layer from packages/contracts/permissions.ts: useCan(permission) and <Can permission=...> so screens hide or disable actions the user's role or plan does not allow (export, stress run, watchlist edits, account and billing, staff-only ops pages), and route guards that send users to a 403 page. The API is still the enforcement point; the UI only reflects it."})
F07 = task("F07", "API skeleton: Fastify, Clerk auth (spec 23), default-deny authorization, tenancy, contract validation, asOf, SSE stream, Excel export framework", "backend", 0, "api-core",
           ["apps/api/src/server.ts", "apps/api/src/lib/**", "apps/api/src/plugins/**", "apps/api/package.json", "apps/api/tsconfig.json"],
           ["static", "unit"], [F01, F02], [f"{H}/api/endpoints.md", G, f"{H}/specs/23-auth-clerk.md"], priority=3,
           params={"brief": "Default deny: every route must declare its permission from packages/contracts/permissions.ts in its route config; the server refuses to start if any route lacks one, and a test enumerates all registered routes to prove it. Request pipeline: authenticate (Clerk session token via @clerk/fastify, or a Marden API key) -> load membership -> check permission and plan -> SET LOCAL app.org_id for the transaction so row-level security applies -> handler. 401 when not signed in, 403 when the role or plan lacks the permission, 404 (not 403) for another org's objects so ids don't leak. SSE stream only delivers events for objects the subscriber may read. Staff-only routes are also IP/SSO restricted. Every write is recorded in audit_log. Provide test helpers: asUser(role, plan, org) for endpoint tests."})
F08 = task("F08", "Test harness: visual diff vs screenshots, contract runner, e2e with seeded DB, data validators", "integrator", 0, "qa",
           ["tests/**", "services/qa/**", "playwright.config.ts", "scripts/gates/**"], ["static"], [F05, F06, F07], [f"{H}/screenshots", G],
           params={"brief": "visual.sh <screen>: run the route in fixture mode at 1440px and compare with handoff/screenshots/<screen>.png (tolerance documented). contract.sh <endpoint>: call endpoint against seeded DB and validate JSON Schema. data.sh <area>: services/qa validators (sources present, tags, sums tie, no orphans). e2e.sh <screen>: live mode against API."},
           priority=4, budget=12, timeout=60)
LEG = task("F09", "Inventory the legacy code and data; map each module to its new home", "planner", 0, "legacy",
           ["docs/legacy-map.md"], [], [F00], ["legacy/README.txt", "legacy/COORDINATION.md"], priority=5,
           params={"brief": "Read legacy/ (and MARDEN_SEED_DIR data). Write docs/legacy-map.md: what each folder does, what is reusable, what data it produces. Return new_tasks only for modules not already covered by port-* tasks in the graph."})
FOUND = [F01, F02, F04, F05, F06, F07, F08]

# ------------------------------------------------------------------ screens (designed boards)
SCREENS = {
    "01-home": "/", "02-customize-home": "/home/customize", "03-completion": "/completion",
    "04-site-detail": "/sites/[slug] (schedule tab + shared site header)", "04b-site-evidence": "/sites/[slug]?tab=evidence",
    "04c-site-power": "/sites/[slug]?tab=power", "04d-site-opposition": "/sites/[slug]?tab=opposition",
    "04e-site-events": "/sites/[slug]?tab=events", "04f-site-debt": "/sites/[slug]?tab=debt", "04g-site-lease": "/sites/[slug]?tab=lease",
    "04h-site-buildings": "/sites/[slug]?tab=buildings", "05-sites": "/sites", "06-instruments": "/instruments",
    "07-instrument-detail": "/instruments/[id]", "08-payer": "/payers/[slug]", "09-stress": "/stress", "10-risk-list": "/risk-list",
    "11-watchlist": "/watchlist", "12-track-record": "/track-record", "13-account": "/account",
    "14-data-centers": "/data-centers (table)", "14b-data-centers-map": "/data-centers?view=map",
    "14c-campus-overview": "/data-centers/[id] (overview + shared campus header)", "14d-campus-buildings": "/data-centers/[id]?tab=buildings",
    "14e-campus-power": "/data-centers/[id]?tab=power", "14f-campus-records": "/data-centers/[id]?tab=records",
    "14g-campus-history": "/data-centers/[id]?tab=history", "15-onsite-power": "/power (table)", "15b-onsite-power-map": "/power?view=map",
    "15c-plant-detail": "/power/[id]",
}
SPEC_OF = {"01": "01-home", "02": "02-customize-home", "03": "03-completion", "04": "04-site-detail", "05": "05-sites",
           "06": "06-instruments", "07": "07-instrument-detail", "08": "08-payer", "09": "09-stress", "10": "10-risk-list",
           "11": "11-watchlist", "12": "12-track-record", "13": "13-account", "14": "14-data-centers", "15": "15-onsite-power"}
PARENT = {"04": "04-site-detail", "14": "14c-campus-overview", "15": "15-onsite-power"}
present = {p.stem for p in (handoff / "screens").glob("*.html")} if (handoff / "screens").exists() else set(SCREENS)
for name, route in SCREENS.items():
    if name not in present:
        continue
    num = name[:2]
    deps = [F05, F06, F08, F01]
    par = PARENT.get(num)
    if par and name != par and not (num == "14" and name in ("14-data-centers", "14b-data-centers-map")):
        deps.append(f"UI-{par}")
    if name == "14b-data-centers-map":
        deps.append("UI-14-data-centers")
    if name == "15b-onsite-power-map" or name == "15c-plant-detail":
        deps.append("UI-15-onsite-power")
    spec = [G, f"{H}/specs/{SPEC_OF[num]}.md", f"{H}/screens/{name}.html", f"{H}/screenshots/{name}.png"]
    if name == "04h-site-buildings":
        spec.append(f"{H}/specs/16-site-links.md")
    task(f"UI-{name}", f"Screen {name}: {route}", "frontend", 1, f"web:{SPEC_OF[num]}",
         [f"apps/web/app/**/{name}/**", f"apps/web/components/{name}/**", f"apps/web/app/(app)/**/{name.split('-',1)[1]}*/**"],
         ["static", "unit", f"visual:{name}"], deps, spec, params={"route": route, "screen": name}, priority=20)

# screens specified but not yet designed
for sid, title, spec, outs in [
    ("UI-18-obligations", "Obligation map: network graph + table view", "18-obligation-map", "obligations"),
    ("UI-18b-obligations-company", "Obligation company page", "18-obligation-map", "obligations-company"),
    ("UI-26-project-file", "Site detail: Parties, Structure and Construction tabs plus the 360 completeness score in the site header and new Completion monitor columns", "26-project-file", "project-file"),
    ("UI-25-leases", "Lease map: KPI strip, per-lessee reconciliation, ranked lease table with trigger leases, Excel export", "25-lease-map", "leases"),
    ("UI-24-payers", "Payers index: capacity to pay and downgrade risk table", "24-payer-capacity", "payers"),
    ("UI-24b-payer-capacity", "Payer page: Capacity to pay section (outflows vs cash flow, leverage vs trigger, rating history, exposed instruments, downgrade stress button)", "24-payer-capacity", "payer-capacity"),
    ("UI-19-gpus", "GPU index page", "19-gpu-collateral", "gpus"),
    ("UI-19b-gpu-model", "GPU model page", "19-gpu-collateral", "gpu-model"),
    ("UI-19c-gpu-deployment", "GPU index: Delivered vs energized section (facility table, CIP vs in-service chart) and the deployment block in the instrument Collateral card", "19-gpu-collateral", "gpu-deployment"),
    ("UI-20-plant-debt", "Plant detail: Debt and contracts section", "20-power-plant-debt", "plant-debt"),
    ("UI-ops-review", "Internal: review queue (approve material facts and links)", "17-dc-power-pipeline", "ops-review"),
    ("UI-ops-pipeline", "Internal: pipeline health (heartbeats, last runs, failures)", "17-dc-power-pipeline", "ops-pipeline"),
]:
    d = [F05, F06, F08, F01] + (["UI-15c-plant-detail"] if sid == "UI-20-plant-debt" else [])
    task(sid, title + " (no board yet: follow the design system and the nearest designed screen)", "frontend", 1, f"web:{outs}",
         [f"apps/web/app/**/{outs}/**", f"apps/web/components/{outs}/**"], ["static", "unit"], d, [G, f"{H}/specs/{spec}.md"], priority=30)

# ------------------------------------------------------------------ endpoints (one task per row in endpoints.md)
ep_rows = []
ep_file = handoff / "api" / "endpoints.md"
if ep_file.exists():
    for line in ep_file.read_text().splitlines():
        m = re.match(r"\|\s*([A-Z /]+?)\s*\|\s*`([^`]+)`(?:,\s*`([^`]+)`)?\s*\|\s*([^|]+)\|\s*([^|]+)\|", line)
        if m and m.group(1).strip() not in ("Method",):
            ep_rows.append((m.group(1).strip(), m.group(2), m.group(4).strip(), m.group(5).strip()))
seen = set()
for method, path, screen, returns in ep_rows:
    slug = re.sub(r"[^a-z0-9]+", "-", path.lower().replace("/api/", "").split("?")[0]).strip("-") or "root"
    if slug in seen:
        slug += "-" + method.split()[0].lower()
    seen.add(slug)
    deps = [F07, F02, F01, F08]
    task(f"API-{slug}", f"Endpoint {method} {path} ({screen}) → {returns}", "backend", 1, f"api:{slug}",
         [f"apps/api/src/routes/{slug}/**", f"db/readmodels/{slug}.sql"], ["static", "unit", f"contract:{slug}"], deps,
         [f"{H}/api/endpoints.md", f"{H}/api/types.ts", G], params={"method": method, "path": path, "returns": returns, "brief": "Declare this route's permission from packages/contracts/permissions.ts. Tests must cover: signed out -> 401; each role and plan that lacks the permission -> 403; a user from another org reading or writing org-owned objects -> 404; allowed role -> 200 with a contract-valid body. Never read org data without the tenancy context."}, priority=25)

# ------------------------------------------------------------------ ports of existing working code (fast path to real data)
PORTS = [
    ("holdings", "N-PORT and BDC schedules: holdings, marks, non-accruals, unmarked fixes, facilities", "services/holdings/**",
     ["legacy/pipeline_repo_backup", "legacy/marks", "legacy/watch", "legacy/na", "legacy/unm", "legacy/fpos"], "data_eng"),
    ("banks", "Bank-to-BDC facility links", "services/banks/**", ["legacy/banks"], "data_eng"),
    ("entities", "Entity aliases and the linker (SPV → parent → site → tenant → lenders)", "services/entities/**", ["legacy/aliases", "legacy/marks/app/_aka.py", "legacy/aiwx"], "data_eng"),
    ("edgar-watch", "EDGAR credit-event watcher (8-K items, covenant/default language, BDC gates)", "services/edgar_watch/**", ["legacy/alerts"], "data_eng"),
    ("dockets", "Docket monitor: 73 proceedings across FERC, state commissions, NMED and counties", "services/collectors/dockets/**", ["legacy/dockets"], "data_eng"),
    ("power-projects", "Power and permit projects (182) and scanner feed", "services/collectors/power/**", ["legacy/aiw/power"], "data_eng"),
    ("ai-debt", "AI debt instrument list (707 instruments) and break watchlist", "services/instruments/**", ["legacy/aiw/debt", "legacy/aiinfra"], "data_eng"),
    ("cases", "Credit cases and memos → facts (sites, leases, credit support, risks, watch items)", "services/cases/**", ["legacy/aiwc/research", "legacy/aiwc/memo", "legacy/aiwc/SCHEMA.md", "legacy/secfill"], "data_eng"),
    ("credit-sim", "Credit Monte Carlo (sim.py, mkparams.py) as a versioned model", "services/models/credit_sim/**", ["legacy/aiwc/sim.py", "legacy/aiwc/mkparams.py", "legacy/aiwc/REVIEW.md"], "quant"),
    ("pricing", "Pricing inputs: price sources, TRACE refresh, spreads", "services/models/price/**", ["legacy/px", "legacy/aiwc/pxsrc", "legacy/trace", "legacy/ytm.py"], "quant"),
    ("q3-nowcast", "Q3 mark nowcast, mark lag and Q3 calendar", "services/models/q3/**", ["legacy/aiwc/q3x", "legacy/watch/marklag.py", "legacy/watch/nowcast_bt.py", "legacy/fwd"], "quant"),
    ("backtest", "Backtests (signals, loans, funds, prices) and the track-record cases", "services/models/backtest/**", ["legacy/bt3", "legacy/pipeline_repo_backup/backtest", "legacy/bt", "legacy/bt4"], "quant"),
    ("gpu-loans", "GPU-backed loan terms and collateral", "services/gpu/**", ["legacy/gpuloans", "legacy/aiwc/gpu"], "data_eng"),
    ("events", "Filing events, SEC full-text hits, WARN notices, rating actions", "services/collectors/events/**", ["legacy/events", "legacy/secft", "legacy/warn", "legacy/ratings"], "data_eng"),
    ("redflags", "Accounting red flags and hardware supplier exposure", "services/collectors/redflags/**", ["legacy/redflags", "legacy/hwexp"], "data_eng"),
    ("verify", "Fact verification tools (independent re-checks)", "services/qa/verify/**", ["legacy/verify", "legacy/verify2", "legacy/verify3", "legacy/vet"], "data_eng"),
    ("exits", "Continuation vehicles, tenders and exits", "services/collectors/exits/**", ["legacy/tender", "legacy/capwork"], "data_eng"),
]
for key, title, out, ins, role in PORTS:
    task(f"PORT-{key}", f"Port: {title}", role, 1, f"port:{key}", [out, f"tests/golden/{py(key)}/**"],
         ["static", f"py:{out.split('/**')[0].replace('services/', '')}"], [F04, F02], [f"{H}/specs/17-dc-power-pipeline.md", "docs/conventions.md"], ins,
         params={"brief": "Keep the working logic and its outputs; wrap it in services/common (raw store, fact store with sources and tags, queue, heartbeat). Add a backfill and an incremental command. Prove parity: the ported code reproduces the legacy output files within tolerance."},
         priority=15, budget=10, timeout=60)

# seed loads from the legacy data export (real data on screens by morning)
SEEDS = [("cases-memos", ["cases", "memo"], "PORT-cases"), ("ai-debt", ["aidebt.json"], "PORT-ai-debt"),
         ("holdings", ["fundpos.json", "q3snap.json"], "PORT-holdings"), ("power", ["power.json", "collmeta.json"], "PORT-power-projects"),
         ("events", ["bevents.json", "niche.json"], "PORT-events"), ("q3", ["q3x"], "PORT-q3-nowcast"), ("prices", ["pxsrc"], "PORT-pricing"),
         ("gpu", ["gpu"], "PORT-gpu-loans"), ("credit", ["aiwcredit.json", "params"], "PORT-credit-sim")]
for key, files, port in SEEDS:
    task(f"SEED-{key}", f"Load legacy data into R2 and the fact store: {', '.join(files)}", "data_eng", 1, f"seed:{key}",
         [f"services/seed/{py(key)}/**"], ["static", f"data:{key}"], [port, F03],
         params={"seed_dir": "$MARDEN_SEED_DIR", "files": files, "brief": "MARDEN_SEED_DIR is the legacy code and data unzipped into one folder (see legacy/README.txt); it is read-only. File names are where to start; search the seed dir and the legacy code that reads/writes them. Idempotent loader; raw files copied to R2 first; every fact keeps its original source URL and quote; anything without a source is loaded as a review-queue item, not a published fact."}, priority=10)

task("SEED-reported-obligations", "Reported obligation edges for V1 companies (Anthropic first: Google Cloud, AWS, Microsoft, Broadcom TPU leases, Nscale, Lambda, SPV chip-lease facilities) from press coverage of the draft S-1, each tagged R with its article URL and quote, held in the review queue; superseded automatically by F facts when the public S-1 is extracted",
     "data_eng", 1, "seed:reported-obligations", ["services/seed/reported_obligations/**"], ["static", "data:reported-obligations"], [F04, F02],
     [f"{H}/specs/18-obligation-map.md"], params={"brief": "Use only articles you can fetch and quote; never copy figures without a source passage. Amounts reported as ranges or 'up to' stay ranges. Mark contracted vs under negotiation separately."}, priority=12)

# ------------------------------------------------------------------ new collectors (specs 17–20)
COLLECTORS = {
    "edgar-index": "EDGAR daily index and full-text search (8-K, 10-K/10-Q, exhibits, S-1, 13F/13D, Form D); priority watch for any S-1/S-1A or F-1 by Anthropic, OpenAI, Crusoe, Lambda, Nscale, Vantage, Fluidstack and other V1 counterparties, routed straight to EXT-s1 with a material-event alert",
    "fred": "FRED: Treasuries, SOFR, spreads",
    "mechanics-liens": "Mechanic's liens and lien releases against V1 sites and their owners/contractors from county recorders (spec 26); amounts, claimants, dates",
    "contractor-records": "General contractor and subcontractors of record from building permits and state contractor license boards; contractor disputes from court dockets (spec 26)",
    "workforce": "Workforce signals per V1 site and contractor: public job postings counts, WARN notices, county-reported construction headcounts where published (spec 26)",
    "draws": "Facility draws over time: 10-Q debt notes, 8-Ks, ABS/CMBS servicer reports, BDC funded vs unfunded commitments (spec 26)",
    "sos-entities": "Secretary of State business entity records for project LLCs in V1 states (registered agent, officers, addresses, formation dates) feeding the linker that ties SPVs to tenants and parents (spec 25)",
    "xbrl-financials": "SEC XBRL company facts for every V1 payer and neocloud: operating cash flow, capex, cash, debt, lease liabilities, RPO, quarterly history (spec 24)", "market-equity": "End-of-day equity prices (ORCL, CRWV, NBIS, IREN, APLD, CORZ, WULF, CIFR, GLXY…)",
    "eia-860m": "EIA-860M generator inventory (monthly) with planned vs actual online", "hifld": "Utility service territories and balancing authority boundaries",
    "overture": "Building footprints (Overture Maps) for data center campuses", "sentinel2": "Sentinel-2 imagery per site and monthly change score",
    "tceq": "Texas TCEQ air permits (standard, NSR, Title V) incl. equipment lists", "nmed": "New Mexico NMED air permits and hearing dockets",
    "ohio-epa": "Ohio EPA air permits", "va-deq": "Virginia DEQ air permits (backup generators)", "ga-epd": "Georgia EPD air permits",
    "az-adeq": "Arizona ADEQ air permits", "in-idem": "Indiana IDEM air permits", "pa-dep": "Pennsylvania DEP air permits",
    "ercot": "ERCOT large-load interconnection reports", "pjm": "PJM queue and large-load studies", "miso": "MISO queue", "spp": "SPP queue",
    "ferc-pipelines": "FERC pipeline certificates and in-service dates", "tx-rrc": "Texas Railroad Commission pipeline permits",
    "county-agendas": "County agendas and minutes framework + V1 counties (Doña Ana NM, Shackelford TX, Taylor TX, Ozaukee WI, Washtenaw MI, Milam TX, Loudoun VA, Prince William VA, Maricopa AZ)",
    "building-permits": "Building permits and certificates of occupancy framework + V1 jurisdictions",
    "county-recorder": "County deed of trust / mortgage records framework + V1 counties",
    "ucc": "UCC-1 searches by debtor name: Secretary of State portals for V1 debtor states (start TX, NM; document which states need paid access)",
    "courts": "Court dockets: federal (CourtListener/PACER where free) and V1 state courts", "ratings": "Rating agency press releases (public pages)",
    "gpu-prices-hyperscalers": "GPU price pages: AWS, Azure, Google Cloud, Oracle", "gpu-prices-neoclouds": "GPU price pages: CoreWeave, Lambda, Nebius, Crusoe, Together, others",
    "gpu-prices-marketplaces": "Public GPU marketplaces (on-demand spot)", "gpu-resale": "Used GPU and server resale listings",
    "press": "Press and news search per site and company (tagged R)",
    "local-opposition": "Local opposition per V1 site: local news outlets, opposition group sites and petitions, public comment filings, zoning appeals and recall or referendum efforts (tagged R unless a filed record)",
    "state-legislation": "State bills and enacted laws on data centers: moratoria, tax incentive changes, large-load tariffs, water and noise rules, for every V1 state (legislature sites; LegiScan if a key is provided)",
    "water-permits": "Water rights, withdrawal and discharge permits for V1 sites (TCEQ, AZ ADWR, NM OSE, other V1 states)",
}
for key, title in COLLECTORS.items():
    task(f"COL-{key}", f"Collector: {title}", "data_eng", 1, f"col:{key}", [f"services/collectors/{py(key)}/**", f"tests/golden/{py(key)}/**"],
         ["static", f"py:collectors/{py(key)}", f"data:{key}"], [F04, F02], [f"{H}/specs/17-dc-power-pipeline.md", f"{H}/specs/18-obligation-map.md", f"{H}/specs/19-gpu-collateral.md", f"{H}/specs/20-power-plant-debt.md"],
         params={"brief": "backfill + incremental commands, raw to R2 first, heartbeat with expected cadence, polite rate limits, respect site terms; if a source needs a paid account or blocks automation, return blocked with exactly what is needed."},
         priority=22)

# ------------------------------------------------------------------ extraction schemas with golden sets
for key, title in {"8k": "8-K items and press exhibits", "credit-agreement": "Credit agreement exhibits (EX-10): parties, amounts, collateral, advance rates, covenants",
                   "lease-commitments": "10-K/10-Q lease and commitment notes incl. leases not yet commenced: totals, term ranges, commencement years, operating vs finance, for every lessee in spec 25 (exact disclosed totals, used for reconciliation)",
                   "lessor-backlog": "Landlord and developer side of leases (spec 25): REIT/owner filings, ABS/CMBS offering docs and servicer reports, JV disclosures: signed-not-commenced leases by tenant, site, amount, start, term", "backlog": "10-K/10-Q backlog and customer concentration",
                   "capex-deployment": "10-K/10-Q PP&E notes and balance sheets (construction in progress vs placed in service, supplier prepayments) and GPU delivery / acceptance / placed-in-service statements in filings, earnings calls and 8-Ks, with dates and unit counts",
                   "offering-docs": "Offering memoranda and ABS filings",
                   "s1": "S-1/F-1 registration statements and amendments (Anthropic first): contractual obligations and compute commitments by counterparty and year, cancelable vs noncancelable, SPV/chip-lease financing, convertibles, customer concentration, related-party deals, risk factors", "air-permit": "Air permits and technical reviews: units, OEM, model, rating, status; redundancy language (maximum units operating simultaneously, hour limits, block/distributed redundant), spare vs primary units, chiller and cooling-tower capacity (spec 27)",
                   "utility-load": "Utility and commission filings: large-load requests and agreements (MW, dates), substation and transformer MVA serving each campus (spec 27)",
                   "county-agenda": "County agenda items: zoning, abatements, moratoria, votes", "ferc": "FERC filings: in-service dates, deficiencies, orders",
                   "ucc": "UCC-1 records: secured party, debtor, collateral text", "rating-release": "Rating agency releases: rating, outlook, action and the downgrade triggers the agency states, quoted with date (spec 24)",
                   "project-structure": "Project structure from credit agreements and offering docs (spec 26): tranches, security package, completion guarantees, sponsor support, reserves, DSCR/LTC/LTV covenants, cash waterfall, hedges, intercreditor terms",
                   "permit-valuation": "Declared construction value from building permits and capital investment commitments from tax abatement agreements (spec 26)",
                   "rating-triggers": "Rating triggers inside the debt (EX-10 and offering docs): minimum counterparty rating for eligible contracts, advance-rate step-downs, cash sweeps, payer-linked events of default (spec 24)", "press": "Press articles (tag R only)"}.items():
    task(f"EXT-{key}", f"Extraction schema + golden set: {title}", "extraction", 1, f"ext:{key}", [f"services/extract/{py(key)}/**", f"tests/golden/ext_{py(key)}/**"],
         ["static", f"py:extract/{py(key)}"], [F04], [f"{H}/specs/17-dc-power-pipeline.md", f"{H}/specs/22-data-agents-models.md"],
         params={"min_precision": 0.95, "min_recall": 0.85}, priority=22)

# ------------------------------------------------------------------ models
MODELS = [("status-rules", "Chain status rules engine (E/T/D/L codes) and materiality rules", ["PORT-cases"]),
          ("cushion", "Cushion model: milestone slips, P10/P50/P90, outside-date assumption", ["PORT-cases", "COL-eia-860m"]),
          ("price", "Marden price engine (port + extend), daily and on events", ["PORT-pricing", "PORT-credit-sim"]),
          ("mark-gap", "Mark gap and NAV hit per holder", ["PORT-holdings", "MOD-price"]),
          ("recovery", "Recovery values (shell, equipment, GPU collateral)", ["MOD-gpu-value", "PORT-credit-sim"]),
          ("gpu-index", "Marden GPU rental index", ["COL-gpu-prices-hyperscalers", "COL-gpu-prices-neoclouds", "COL-gpu-prices-marketplaces"]),
          ("gpu-value", "GPU value by age, LTV and recovery per facility", ["MOD-gpu-index", "PORT-gpu-loans", "COL-gpu-resale"]),
          ("capacity-estimate", "Capacity and GPU estimate (spec 27): facility MW triangulated from filed figures, utility load, prime generation, backup generators with redundancy ranges, cooling and footprint; IT MW via PUE; GPUs via kW per GPU by generation from OEM specs; ranges, confidence, disagreement flags, replaced-by-filing; backtest on disclosed sites", ["EXT-air-permit", "EXT-utility-load", "COL-overture", "COL-building-permits", "PORT-power-projects", "EXT-8k", "EXT-press", "PORT-gpu-loans"]),
          ("project-file", "Project file (spec 26): 360-degree checklist per V1 site with sourced / n/d / n/p states and completeness score; cost-to-complete gap, funding gap, draw pace vs build pace, contractor stress from liens and disputes, long-lead equipment risk; signals feed cushion and status rules", ["MOD-capacity-estimate", "EXT-project-structure", "EXT-permit-valuation", "COL-mechanics-liens", "COL-contractor-records", "COL-workforce", "COL-draws", "COL-sos-entities", "MOD-cushion", "MOD-lease-map", "EXT-air-permit"]),
          ("lease-map", "Lease map (spec 25): attribute every disclosed dollar of not-yet-commenced leases to named leases and sites; per-lessee reconciliation that ties to the filing exactly (attributed + unattributed = disclosed); researcher loop that backs into leases from the trail (LLC to tenant via Secretary of State records, permits, tax abatements, utility agreements, deeds, ABS loan-level data; size from filed rent, ABS rent or MW x market rent per kW-month; date from energization evidence; quarter-over-quarter change matching against the lessee total; inferred sums capped at the disclosed total), tagged M Inferred with confidence; turns added at commencement vs agency headroom, trigger-lease flag, priority score with weights shown; agency criteria quoted from current published methodology documents", ["EXT-lease-commitments", "EXT-lessor-backlog", "EXT-offering-docs", "EXT-8k", "EXT-press", "EXT-county-agenda", "EXT-air-permit", "COL-sos-entities", "COL-county-recorder", "COL-building-permits", "MOD-capacity-estimate", "MOD-payer-capacity", "PORT-entities", "PORT-cases"]),
          ("payer-capacity", "Payer capacity and downgrade risk (spec 24): committed outflows vs cash flow, lease-adjusted leverage path vs agency triggers, headroom and breach year, AI revenue coverage, concentration, risk band, exposed instruments; per-agency trigger tracking, rebuilt agency-adjusted metrics, same-day event recompute with likely-to-act alerts, outlook clock, mitigants, spread gap, fallen-angel flag; backtest on past rating actions (Oracle Jul 2026 first)", ["COL-xbrl-financials", "EXT-lease-commitments", "EXT-backlog", "EXT-s1", "EXT-rating-release", "EXT-rating-triggers", "MOD-obligations"]),
          ("stress", "Stress scenarios: funding miss, payer downgrade by N notches or below investment grade (trips rating triggers, reprices, recovery, holders), power delay", ["MOD-price", "MOD-cushion", "MOD-payer-capacity"]),
          ("contagion", "Contagion over the obligation graph (3 hops)", ["MOD-obligations", "MOD-price"]),
          ("obligations", "Obligation graph builder from extracted edges", ["EXT-lease-commitments", "EXT-8k", "EXT-backlog", "EXT-credit-agreement", "EXT-s1", "SEED-reported-obligations"]),
          ("gpu-deployment", "Delivered vs energized per facility and site (spec 19): units financed/delivered/energized, idle share, months idle, interest on idle share, value lost while idle, LTV today vs on schedule; company construction-in-progress series", ["MOD-gpu-value", "MOD-cushion", "MOD-capacity-estimate", "EXT-capex-deployment", "EXT-credit-agreement", "EXT-8k", "PORT-gpu-loans", "COL-ercot", "COL-pjm", "COL-eia-860m"]),
          ("risk-rank", "Weekly risk ranking (rules until calibrated)", ["MOD-cushion", "MOD-mark-gap", "MOD-status-rules", "MOD-gpu-deployment", "MOD-payer-capacity"]),
          ("coverage", "Coverage rates per field and source health", [F04])]
for key, title, deps in MODELS:
    task(f"MOD-{key}", f"Model: {title}", "quant", 1, f"model:{key}", [f"services/models/{key.replace('-', '_')}/**"],
         ["static", f"py:models/{key.replace('-', '_')}"] + (["data:lease-reconciliation"] if key == "lease-map" else []), [F04, F02] + deps, [G, f"{H}/specs/22-data-agents-models.md", f"{H}/specs/09-stress.md", f"{H}/specs/12-track-record.md", f"{H}/specs/19-gpu-collateral.md", f"{H}/specs/18-obligation-map.md"], priority=24, budget=12, timeout=60)

# ------------------------------------------------------------------ read models + platform features
for key in ["completion", "sites", "site-detail", "instruments", "instrument-detail", "payer", "risk-list", "watchlist", "home",
            "track-record", "data-centers", "campus", "power-plants", "plant", "obligations", "gpus", "search", "leases", "payers", "project-file"]:
    task(f"RM-{key}", f"Read model for {key}: precomputed rows, rebuilt on fact publish, NOTIFY on change", "data_eng", 1, f"rm:{key}",
         [f"services/readmodels/{py(key)}/**", f"db/readmodels/rm_{key.replace('-', '_')}.sql"], ["static", f"py:readmodels/{py(key)}", "migrate"], [F02, F04],
         [G], priority=26)
for key, title, role, outs in [
    ("alerts", "Alert engine + Postmark email (instant red, 7:00 ET digest), alert settings", "backend", ["services/alerts/**", "apps/api/src/routes/alerts*/**"]),
    ("weekly-note", "Weekly note agent: drafts from computed numbers only, approval queue, Monday send", "backend", ["services/notes/**"]),
    ("review-queue", "Review queue service + API (approve facts, links, events)", "backend", ["services/review/**", "apps/api/src/routes/review*/**"]),
    ("snapshots", "Daily point-in-time snapshot archive + asOf reads", "data_eng", ["services/snapshots/**"]),
    ("excel", "Excel export for every table (data sheet + sources sheet)", "backend", ["apps/api/src/routes/export*/**", "services/export/**"]),
    ("auth-account", "Clerk integration end to end per spec 23: webhook sync + nightly reconcile, /api/me, invites/role changes/removal via the Clerk Backend API, owner rules, staff_users, MFA enforcement, alert settings, plan (Stripe invoice stub)", "backend", ["apps/api/src/routes/account*/**", "apps/api/src/routes/auth*/**", "apps/api/src/routes/me/**", "apps/api/src/routes/webhooks/**", "apps/api/src/lib/clerk/**", "services/auth_sync/**"]),
    ("access-matrix-tests", "Role-based access test suite: for every endpoint x every role (owner, admin, member, viewer, staff) x every plan, assert the outcome in packages/contracts/permissions.ts; plus cross-org reads/writes (404), API keys limited to their scopes, and RLS checked directly in SQL with the api DB role", "red_team", ["tests/security/access/**"]),
    ("team-roles", "Members and roles management: invite, change role, remove, transfer ownership; only owner/admin may; last owner cannot be removed; every change audit-logged", "backend", ["apps/api/src/routes/members*/**", "apps/api/src/lib/members/**"]),
    ("audit-log", "Audit log of user and admin actions (who, what, when, from where), queryable by org owners, append-only", "backend", ["apps/api/src/routes/audit*/**", "apps/api/src/lib/audit/**", "services/audit/**"]),
    ("api-keys", "Customer API keys (hashed, scoped, revocable), per-key and per-org rate limits, usage metering", "backend", ["apps/api/src/routes/api-keys*/**", "apps/api/src/plugins/rate-limit*/**", "apps/api/src/lib/keys/**"]),
    ("observability", "Structured JSON logs with request IDs across web, API and workers; OpenTelemetry traces; Sentry; health and readiness endpoints", "devops", ["apps/api/src/plugins/telemetry*/**", "apps/web/lib/telemetry/**", "services/common/telemetry/**", "apps/api/src/routes/health*/**"]),
    ("backups", "Nightly Neon dump to R2, retention policy, scripted monthly restore test into a scratch branch", "devops", ["scripts/ops/backup/**", "services/ops/backup/**"]),
    ("ci", "GitHub Actions running the same gates on every push and PR, preview deploys for PRs, required checks on main", "devops", [".github/**"]),
]:
    task(f"PLAT-{key}", title, role, 1, f"plat:{key}", outs, ["static", "unit"], [F07, F04, F02], [G], priority=26)

task("AUTH-e2e", "Auth proven against the real Clerk dev instance: spec 23 tests 4 and 5 (test orgs and users per role via Backend API with +clerk_test emails, every endpoint x role with real tokens, Playwright flows: sign-in, org switch, invite/accept, role change, owner rules, sign-out closes stream, revoked session); cleans up test users", "integrator", 2, "auth",
     ["tests/auth/**", "tests/e2e/auth/**", "scripts/test/clerk/**"], ["static", "auth:live"],
     ["PLAT-auth-account", "PLAT-team-roles", F06, F07] + [x["id"] for x in T if x["id"].startswith("API-")] + ["UI-13-account"],
     [f"{H}/specs/23-auth-clerk.md", f"{H}/specs/13-account.md"], priority=38, budget=12, timeout=60)
for t in T:
    if t["id"] in ("PLAT-auth-account", "PLAT-team-roles", "UI-13-account"):
        t["spec"].append(f"{H}/specs/23-auth-clerk.md")
    if t["id"] in ("PLAT-auth-account", F07):
        t["gates"].append("auth:unit")

# Construction progress is the #1 product: power bottlenecks, permitting delays and local opposition.
# Everything on that path is scheduled ahead of the rest of its phase.
COMPLETION = {
    "UI-01-home", "UI-03-completion", "UI-04-site-detail", "UI-04b-site-evidence", "UI-04c-site-power", "UI-04d-site-opposition",
    "UI-04e-site-events", "UI-05-sites", "UI-10-risk-list", "UI-ops-review",
    "RM-completion", "RM-sites", "RM-site-detail", "RM-home", "RM-risk-list",
    "MOD-status-rules", "MOD-cushion", "MOD-coverage", "MOD-risk-rank",
    "PORT-dockets", "PORT-power-projects", "PORT-cases", "PORT-entities", "PORT-edgar-watch",
    "SEED-cases-memos", "SEED-power", "PLAT-review-queue", "PLAT-alerts",
    "EXT-air-permit", "EXT-county-agenda", "EXT-ferc", "EXT-8k", "EXT-press",
    "MOD-capacity-estimate", "EXT-utility-load", "API-data-centers-id-capacity",
    "MOD-project-file", "UI-26-project-file", "RM-project-file", "EXT-project-structure", "EXT-permit-valuation", "API-sites-slug-project-file",
} | {f"COL-{k}" for k in ["eia-860m", "tceq", "nmed", "ohio-epa", "va-deq", "ga-epd", "az-adeq", "in-idem", "pa-dep", "ercot", "pjm", "miso",
                         "spp", "ferc-pipelines", "tx-rrc", "county-agendas", "building-permits", "sentinel2", "courts", "press", "hifld", "overture",
                         "local-opposition", "state-legislation", "water-permits", "mechanics-liens", "contractor-records", "workforce", "draws"]}
for t in T:
    if t["id"] in COMPLETION or (t["id"].startswith("API-") and any(k in t["id"] for k in ("completion", "sites", "home", "risk-list"))):
        t["priority"] = min(t["priority"], 5 if t["phase"] == 1 else 25)
        t["params"]["focus"] = "Construction progress is Marden's #1 product: power bottlenecks, permitting delays and local opposition, with project-finance-level 360 information per site (spec 26). Prefer depth on the V1 sites over breadth."
LEASES = {"COL-sos-entities", "EXT-lease-commitments", "EXT-lessor-backlog", "COL-xbrl-financials", "EXT-rating-release", "EXT-rating-triggers",
          "MOD-lease-map", "MOD-payer-capacity", "RM-leases", "RM-payers", "UI-25-leases", "UI-24-payers", "API-leases", "API-payers"}
for t in T:
    if t["id"] in LEASES:
        t["priority"] = min(t["priority"], 8)
        t["params"]["focus"] = "Second priority after construction progress: map every dollar of off-balance-sheet leases and rank the ones that tip a lessee into a downgrade."
missing = sorted((COMPLETION | LEASES) - {t["id"] for t in T})
assert not missing, missing

for t in T:  # capacity section plugs into the designed Payer page
    if t["id"] == "UI-26-project-file":
        t["deps"] += ["UI-04-site-detail", "UI-03-completion"]
        t["outputs"] += ["apps/web/app/**/sites/*/page.tsx", "apps/web/components/04-site-detail/site-header*", "apps/web/components/03-completion/columns*"]
    if t["id"] == "UI-24b-payer-capacity":
        t["deps"] += ["UI-08-payer"]
        t["outputs"] += ["apps/web/app/**/payers/*/page.tsx"]

for t in T:  # deployment UI plugs into the GPU index and the instrument Collateral card, so it goes after them
    if t["id"] == "UI-19c-gpu-deployment":
        t["deps"] += ["UI-19-gpus", "UI-07-instrument-detail"]
        t["outputs"] += ["apps/web/app/**/gpus/page.tsx", "apps/web/components/07-instrument-detail/collateral*"]

for t in T:  # the role matrix suite runs once every endpoint exists
    if t["id"] == "PLAT-access-matrix-tests":
        t["deps"] += [x["id"] for x in T if x["id"].startswith("API-")] + ["PLAT-team-roles", "PLAT-api-keys"]
        t["gates"] = ["static", "security:access"]
        t["phase"] = 2

# ------------------------------------------------------------------ phase 2: integration, backfill, hardening
ui_ids = [t["id"] for t in T if t["id"].startswith("UI-")]
api_ids = [t["id"] for t in T if t["id"].startswith("API-")]
for group, prefixes in {"site": ["04"], "completion-sites": ["03", "05"], "instruments": ["06", "07"], "payer-stress": ["08", "09", "24", "25"],
                        "risk-watch-home": ["01", "02", "10", "11"], "track-account": ["12", "13"], "datacenters": ["14"], "power": ["15", "20"],
                        "obligations-gpus": ["18", "19"]}.items():
    uis = [u for u in ui_ids if any(u.startswith(f"UI-{p}") for p in prefixes)]
    task(f"INT-{group}", f"Wire {group} screens to live API + read models; e2e in live mode", "integrator", 2, f"int:{group}",
         ["**"], ["static", f"e2e:{group}"], uis + api_ids + [f"RM-{k}" for k in ["completion", "sites", "site-detail", "instruments", "instrument-detail"]],
         [G], priority=40, budget=12, timeout=60)
int_ids = [t["id"] for t in T if t["id"].startswith("INT-")]
task("OPS-backfill", "Run every collector and seed backfill for the V1 universe; record counts and gaps", "integrator", 2, "ops",
     ["reports/backfill/**", "services/**/backfill_state/**"], ["data:all"], [t["id"] for t in T if t["id"].startswith(("COL-", "SEED-", "PORT-"))], priority=35, budget=10, timeout=120)
task("OPS-live", "Turn on incremental schedules (Railway cron + workers); verify heartbeats and SSE updates end to end", "devops", 2, "ops",
     ["infra/**", "scripts/infra/**", "railway.json", "railway.toml"], ["static"], ["OPS-backfill", F03] + int_ids, priority=36)
task("QA-perf", "Performance: p95 API < 100 ms on every endpoint, first paint < 1 s on every screen", "integrator", 2, "perf",
     ["**"], ["static", "perf:all"], int_ids, priority=45)
task("QA-security", "Security review: authz, injection, SSRF, secrets, rate limits; fix or file", "red_team", 2, "security",
     ["reports/findings/**", "**/tests/**"], ["static", "security:all"], int_ids, priority=45)
task("QA-data", "Data acceptance: every displayed value sourced and tagged; sums tie; spot-check 50 facts against sources", "integrator", 2, "data-qa",
     ["reports/**", "services/qa/**"], ["data:all"], ["OPS-backfill"] + [t["id"] for t in T if t["id"].startswith("MOD-")], priority=40)
task("DEPLOY-staging", "Deploy to Railway staging; smoke every route and endpoint; publish URL in reports/deploy.md", "devops", 3, "deploy",
     ["infra/**", "scripts/infra/**", "reports/deploy.md"], ["static"], ["OPS-live", "QA-perf", "QA-security", "QA-data", "AUTH-e2e"] + [t["id"] for t in T if t["id"].startswith("PLAT-")], priority=50)
task("ACCEPT", "Acceptance run against the Definition of done; write reports/acceptance.json", "integrator", 3, "acceptance",
     ["reports/acceptance.json", "reports/acceptance.md", "tests/acceptance/**"], ["static"], ["DEPLOY-staging"], [f"{H}/README.md", f"{H}/specs/00a-product-spec.md"], priority=60,
     params={"brief": "For every screen: renders in live mode, matches its design in fixture mode, no untagged numbers. For every endpoint: contract passes. Auth: spec 23 tests 1-5 pass against the Clerk dev instance on the deployed staging URL. For the 50 V1 instruments: sourced chain, Marden price, recovery, stress result. COMPLETION FIRST: for every V1 site (~25), the Completion monitor and site detail show every critical-path milestone (power: interconnection or on-site generation; permits: air, building, water, zoning; construction progress: satellite change and certificates; opposition: agendas, lawsuits, moratoria, legislation) with its evidence, a cushion band and status; list every site with any unexplained n/d as a failure. Every V1 site has a spec 26 completeness score and every n/d has an open research item. Alerts fire on a test watchlist. List every failure with its cause."})

for t in T:  # completion screens are wired to live data first in phase 2
    if t["id"] in ("INT-site", "INT-completion-sites"):
        t["priority"] = 30
        t["params"]["focus"] = "Construction progress is Marden's #1 product: wire and prove these screens first."
json.dump(T, sys.stdout, indent=1)
roles = {}
for t in T:
    roles[t["role"]] = roles.get(t["role"], 0) + 1
print(f"{len(T)} tasks · " + ", ".join(f"{k} {v}" for k, v in sorted(roles.items())), file=sys.stderr)
