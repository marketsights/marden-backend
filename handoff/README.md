# Marden V1: design and build handoff

Marden prices and stress-tests AI data center and GPU debt by linking each loan to the payment chain behind it: end user → tenant → developer/SPV → lenders. This package holds the final V1 screens, the reference HTML/CSS, a design system, and a build spec for every screen and feature.

Product spec (source of truth for scope and data): Marden V1 Product Spec (Claude Docs), tabs "Marden V1 Product Spec", "Screens and features", "Tech stack".

## What is in this folder

| Folder | What it is | How to use it |
| --- | --- | --- |
| `screens/` | 30 standalone HTML pages, linked to each other: 13 core screens, all 8 Site detail tabs, Data centers (table, map, 5 campus tabs) and On-site power (table, map, plant detail). Open `screens/index.html`. | Pixel and markup reference. The CSS in each page's `<style>` block is the real styling; lift it into Tailwind classes. |
| `screenshots/` | Full-page PNG of every screen at 1440 px wide (2x). | Visual reference and QA baseline. |
| `review/` | Landscape PDF of all screens with a cover page. | Share for review and sign-off. |
| `design-system/` | `tokens.css`, `tokens.json`, `tailwind.config.snippet.js`, `components.md`. | Set these up first. Every screen uses only these tokens and components. |
| `specs/` | `00-global.md` (shell, rules, formats, states) plus one file per screen; 14–17 cover Data centers, On-site power, linking financed sites to buildings and plants, and the public-records pipeline behind them. | Build each screen from its spec. Each spec lists route, layout, fields, data source, interactions, states and acceptance criteria. |
| `api/` | `types.ts` (response shapes) and `endpoints.md` (internal app endpoints). | Backend contract for the web app. These are internal endpoints for our own app. Marden has no customer API in V1. |
| `fixtures/` | JSON matching `types.ts`, filled with the values shown in the designs. | Build the front end against these before the backend is live. |
| `design-source/` | The design canvas source (`*.dc.html`, `canvas.json`). | Only needed to edit the designs in the canvas tool. |
| `PLACEHOLDERS.md` | Every value in the designs that is a placeholder, and where the real value comes from. | Nothing on this list ships as typed text. |

## Stack (from the Tech stack tab)

- Web app: Next.js (App Router) + TypeScript, Tailwind, TanStack Table for every table, ECharts for charts, MapLibre (free vector tiles) for the Data centers and On-site power maps and the Buildings site plans.
- Backend: Node + TypeScript (Fastify) serving the endpoints in `api/endpoints.md`; Postgres (+ PostGIS) is the single source of truth; Python for collectors and models.
- Hosting: Railway (staging and production). Email: Postmark. Login: Clerk (spec 23). Errors: Sentry.
- Not in V1: customer API, MCP, Slack, holdings upload, thermal or drone data, licensed third-party data center datasets (Marden builds its own from public records; spec 17).

## Build order

Goal: a working demo by day 4.

1. Day 1: design system (tokens, fonts, shared components) and the app shell; fixtures wired.
2. Days 1–2: Completion monitor.
3. Days 2–3: Site detail, Schedule tab first, then the other six tabs (the Buildings tab comes with step 7).
4. Day 3: Instrument detail.
5. Day 4: Watchlist and alerts, including email and the `/api/stream` live updates. **Demo-ready here.**
6. Then: Home with the six default widgets; Instruments, Sites, Payer; Risk list and Track record; Stress; Account; Customize Home last (it can slip without hurting the demo).
7. Data centers and On-site power (specs 14–17). The pipeline (spec 17) starts in week 1 alongside Completion; the screens, the Site detail Buildings tab and the Bldgs columns follow once financed sites are linked to their campuses and plants.
8. Excel export on every table as each screen lands.

## Rules that apply everywhere

- Every number shown carries a source tag: **F** filed record, **R** reported in press, **M** Marden model. The tag links to the source document or method. No untagged figures.
- Marden never asks for holdings. Exposure is computed from the watchlist only.
- Alerts go in-app and by email. No Slack.
- Status colors mean status only: red = broken, amber = strained, green = holding, grey = no data. Indigo (brand) means interactive.
- Every value shown in the designs that is tagged M, or written in [brackets], is a placeholder. See `PLACEHOLDERS.md`.

## Definition of done for V1

A fund can open any of the 50 V1 instruments and see a sourced chain, a Marden price, a recovery value and a stress result; alerts fire on watchlist names; every field has a source, date and F/R/M tag.

## Added after the design pass (specs only, no boards yet)

- Obligation map (spec 18) for OpenAI, Anthropic, Oracle, Microsoft, CoreWeave and Nvidia, with the contagion stress scenario.
- GPU price index and collateral values (spec 19), feeding recovery on GPU facilities; labeled model v0 until checked against resale prices.
- Power plant debt (spec 20): owners, UCC liens, financing and power buyers on every on-site plant.

## Specs exported from the product doc (for agents)

- `specs/00a-product-spec.md`: positioning, V1 coverage (50 instruments, ~25 sites), data model, computed outputs, definition of done.
- `specs/21-tech-stack.md`: stack by layer, reliability rules, running cost, what not to build with.
- `specs/27-capacity-gpu-estimate.md`: facility MW, IT MW and GPU count per building from generators, utility load, prime generation, cooling and footprint, with ranges and calibration.
- `specs/26-project-file.md`: project-finance-level 360° view per site (parties, structure, budget, draws, contractor health incl. mechanic's liens, equipment) with a completeness score.
- `specs/25-lease-map.md`: every dollar of off-balance-sheet data center leases, reconciled to filings, ranked by which leases tip a lessee into a downgrade.
- `specs/24-payer-capacity.md`: can the payers behind the leases afford them; downgrade headroom and which instruments reprice if they're cut.
- `specs/23-auth-clerk.md`: Clerk setup, roles, how the API enforces access, and the definition of tested-and-working.
- `specs/22-data-agents-models.md`: where every data point comes from, how data moves, the six agents, accuracy layers, the models, real-time updates, schedule.

**Precedence when documents disagree:** auth is **Clerk**, per `specs/23-auth-clerk.md`; ignore any WorkOS mention. numbered specs 14–20 are newer than 21–22 and win. Postgres is **Neon** (with PostGIS), not Railway Postgres; everything else runs on Railway. GPU prices come from public price pages per spec 19, not a licensed index. The API is served under `/api/...` per `api/endpoints.md`; `/v1/...` is the later public alias.
