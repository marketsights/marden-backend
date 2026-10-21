# Tech stack

Marden runs on Railway as a handful of services around one Postgres database: Python for collectors and models (your existing modeling code), TypeScript for the API and app (your existing Node skills), and cheap object storage for raw documents. Expect roughly $200–1,300 a month at launch; AI extraction and record fees are the swing costs.

## Architecture

&#91;embedded content: Marden architecture · sources to customers\]

Records flow one way: fetched, stored raw, extracted, checked by a human, then written to Postgres. Models read and write Postgres daily, and everything customers see comes through the API.

## Stack by layer

Boring, proven tools you already know, with one database as the single source of truth.

| Layer | Choice | Why |
| --- | --- | --- |
| Hosting | Railway: one project, separate staging and production environments | Everything in one place, as you wanted; cron, workers and Postgres built in |
| Database | Postgres with PostGIS | Handles tens of millions of rows; maps and site geometry; dated history tables |
| Migrations | Plain SQL migrations (dbmate) | Both Python and TypeScript share one schema, no ORM lock-in |
| Raw documents and images | Cloudflare R2 | Cheap, no download fees; every fetched file kept unchanged |
| Collectors | Python: httpx, Playwright for JavaScript-heavy county portals, PyMuPDF and Tesseract for PDFs and scans | One script per source, scheduled by Railway cron |
| Job queue | Postgres-backed queue (Procrastinate) | Retries and no duplicate runs without adding Redis or Kafka |
| Extraction | Claude API with structured JSON output; Haiku for triage, Sonnet for hard documents; Batch API and prompt caching | Turns filings and permits into fields cheaply; every field keeps its source |
| Entity matching | Postgres tables plus fuzzy matching (rapidfuzz), human-confirmed | SPV names to parents, sites, tenants, lenders |
| Models | Python: polars, QuantLib (already used for CDS), scipy | Pricing, cushion, recovery and stress run daily |
| Imagery | Sentinel-2 via Microsoft Planetary Computer, rasterio | Free; tested from this workspace on Abilene |
| API | Node and TypeScript (Fastify) with an OpenAPI spec, API keys, rate limits | The app, Excel export, MCP and customers all use the same API |
| Web app | Next.js, Tailwind, TanStack Table, ECharts, MapLibre | Matches the mocked screens; maps without licence fees |
| Login | Clerk (organizations, roles, MFA, enterprise SSO); see spec 23 | Org-based B2B auth with invitations and SSO funds will ask for; tested with real sign-ins |
| Alerts | Postmark for email | Reliable delivery, simple |
| Billing | Stripe invoices | Annual contracts, no checkout flow needed |
| Monitoring | Sentry for errors; heartbeat checks (Healthchecks.io) on every collector and job | You hear when a source goes stale before a customer does |

## Reliability rules

These rules matter more than the tool choices; they are what make the numbers defensible to a fund.

1. **Raw first.** Save every fetched file to R2 with its URL, fetch time and hash before parsing, so anything can be re-parsed and audited.
2. **Dated history.** Store when a fact was true and when Marden learned it. That answers "what did we know on March 20?" for backtests and the track record.
3. **Safe reruns.** Every job can run twice without creating duplicates, keyed on source and document hash.
4. **Nothing goes live unchecked.** Extracted events pass the review queue, or a rule plus a high confidence score, before they change a status or fire an alert.
5. **Every number carries its source,** as-of date and Filed / Reported / Modeled tag, end to end.
6. **Heartbeats.** Each source has an expected cadence; if it goes quiet, you get an alert.
7. **Polite collection.** Rate limits, caching, and respect for each site's terms; SEC requests stay under its 10-per-second limit with the declared User-Agent.
8. **One door to the data.** The app, Excel, MCP and customers all read through the API, never the database directly.
9. **Backups you've tested.** Nightly database dumps to R2, with a monthly restore test into staging.
10. **Staging before production.** Every change runs in staging first; migrations run in a single, reversible step.

## Monthly running cost

Estimates, not quotes: launch is 25 sites and 50 instruments; scale is about 300 sites and 500 instruments.

| Item | Launch ($/mo) | Scale ($/mo) |
| --- | --- | --- |
| Railway (app, API, workers, cron, Postgres) | 40–120 | 200–500 |
| Cloudflare R2 storage | 0–5 | 15–50 |
| Claude API extraction and weekly notes | 50–300 | 300–1,500 |
| County and UCC record fees | 100–800 | 300–1,500 |
| Email, error tracking, heartbeats | 0–45 | 45–130 |
| Login and billing | 0 | Single sign-on per customer, passed through in pricing |
| **Total** | **\~200–1,300** | **\~900–3,700** |

Keep extraction cheap with the Batch API, prompt caching, Haiku for triage, and parsing only new or changed documents.

## What not to build with

- **Kubernetes or microservices:** a solo founder can't run them; a few Railway services are enough.
- **Kafka, Redis queues, Airflow:** the Postgres queue and Railway cron cover it.
- **A separate warehouse (Snowflake, BigQuery):** Postgres handles this data volume for years.
- **A vector database:** search over filings fits in Postgres; add pgvector only if needed.
- **No-code scrapers:** they break silently and can't be versioned or tested.
- **Customers reading the database:** everything goes through the API.
