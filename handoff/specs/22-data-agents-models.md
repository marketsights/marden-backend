# Data, agents and models

Every fact enters Marden from a document or a data feed, is extracted and checked by agents, approved where it matters, stored once with its source, and only then used by the models and screens. Agents read and check; code computes every number; nothing an LLM writes reaches a PM without a source quote behind it.

## Where every data point comes from

Two kinds of input: structured feeds pulled by API (numbers, ready to use) and documents read by extraction agents (facts with a quoted source). Tags: F filed, R reported, M Marden model.

| Data point | Source | How it's pulled | Refresh | Tag |
| --- | --- | --- | --- | --- |
| Fund holdings and marks | SEC N-PORT, BDC 10-Q/10-K schedules | EDGAR full-text search API finds filings; parse N-PORT XML and BDC tables in code | Check every 10 min; data quarterly | F |
| Debt terms, lenders, recourse | 8-K exhibits, credit agreements, offering memos, ABS filings | EDGAR API, then extraction agent | On each new filing | F |
| Lease terms (rent start, outside date, force majeure) | 8-K exhibits, filings; press when not filed | Extraction agent | On each new document | F or R |
| Power and pipeline milestones | FERC eLibrary, state utility dockets | Collector by docket number, then extraction agent | Hourly / daily | F |
| Air permits, on-site generation | TCEQ, NMED and other state portals | Collector by site, then agent | Daily | F |
| Grid energization | ERCOT large-load reports, PJM/MISO/SPP queues | Download reports, parse in code | Weekly / monthly | F |
| Local opposition, abatements | County agendas, court dockets | Collector, then classification agent | Daily | F |
| Construction progress | Sentinel-2 via Planetary Computer API | Image pull, change score in code | Every pass, \~5 days | M |
| Rates and curves | FRED API (Treasuries, SOFR) | API | Daily | F |
| Bond trades | FINRA TRACE (licensed feed when added) | API | Daily, later intraday | F |
| Equity prices (ORCL, CRWV, NBIS, IREN) | Market data API (e.g. Polygon or Tiingo) | API | End of day; 15 min later | F |
| Ratings, CDS levels, dealer quotes | Agency releases, press | Agent with source quote | On publication | R |
| GPU rental prices | Licensed index (Silicon Data or Ornn) or public price pages | API or collector | Weekly | F or R |
| Prices, cushions, recovery, stress | Marden models | Computed in code | Daily and on events | M |

## How data moves

Five services, one database, one job queue. Services talk through the queue and Postgres notifications, not service-to-service HTTP calls, so a slow source never blocks a page.

1. **Collectors** (Python, one adapter per source) fetch new items, save the raw file to R2 with its hash and fetch time, and enqueue an `extract` job.
2. **Extraction workers** (Python plus Claude API) turn each document into candidate facts: field, value, unit, entity, date and the exact quote with its location.
3. **Fact store** (Postgres) keeps every fact with its source, when it was true and when Marden learned it. Approved facts are published; nothing is overwritten.
4. **Model workers** (Python) run when a published fact touches their inputs, and on schedule. They write versioned outputs: price, cushion, recovery, stress.
5. **API** (Node and TypeScript) reads published facts and model outputs, serves the app, and streams update events to open screens.

The queue (Postgres-backed) handles retries, rate limits per source and priorities: material events jump the line ahead of routine filings.

## The agents

Six agent roles, each a stateless job on the queue. Hundreds run at once during a backfill: every document is its own job, sent through the Claude Batch API at half the cost, with concurrency capped per source so SEC and state portals aren't overloaded.

| Agent | Job | Output |
| --- | --- | --- |
| Extractor | Reads one document with a schema for its type (8-K, credit agreement, FERC filing, permit, agenda) | Candidate facts with exact quotes |
| Verifier | A second, independent pass re-reads the source and checks each candidate field against its quote | Agree, disagree or unsupported |
| Linker | Matches entities (Red Chiles A–D LLC → Jupiter → STACK, BorderPlex, Oracle) | Proposed links with evidence |
| Researcher | Each week, searches for fields still marked not disclosed | New documents for the Extractor |
| Watchdog | Each day, checks consistency: sums, contradictions between screens, stale sources, numbers that moved without a cause | Issues for the review queue |
| Note writer | Drafts the weekly note from computed numbers only; every figure is filled in by code, not written by the model | Draft for your approval |

Agents never compute a price, cushion or total. They read, check and write; the models do the math.

## How every number stays accurate

Six layers, each catching what the one before misses.

1. **No quote, no fact.** An extracted value without an exact source passage is rejected automatically.
2. **Two independent reads.** Extractor and Verifier must agree; any disagreement goes to review.
3. **Code validators.** Units, date order, value ranges, and sums (tranches add to the facility, sites add to the payer total) checked on every write.
4. **Source ranking.** Filed beats reported; a conflict between sources is flagged, never silently resolved.
5. **Your approval only where it matters.** Facts that change a chain status, cushion band or alert wait for a click; routine filed facts that pass layers 1–4 publish automatically.
6. **Measured, not assumed.** A test set of verified facts is re-run on every prompt or model change, and extraction accuracy is tracked by document type. The models are held to the backtest.

Every screen value links to its fact, its source document and the model version that produced it.

## The models

All models are Python, run in the model worker on Railway, and write versioned outputs to Postgres. Each re-runs when a published fact touches its inputs, so prices move with events, not just on a clock.

| Model | What it answers | How it works | Inputs | Runs |
| --- | --- | --- | --- | --- |
| Marden price (the engine already built) | What an untraded loan or bond is worth | Reduced-form default model: tier comparable curves plus a default-risk build-up for tenant credit, construction stage and chain status; a Kalman filter blends each new observation | FRED curves, fund marks, TRACE and dealer quotes, bond-implied CDS (ISDA standard model in QuantLib), chain status | End of day, plus on each material event; every 15 min for instruments with traded inputs once a feed is licensed |
| Cushion | Months between likely completion and the lease outside date | Critical-path milestones per site; each milestone's slip drawn from history (EIA-860M planned vs actual, plus Marden's archive); 10,000 simulated schedules | Milestones, evidence dates, outside date | On each new milestone fact; weekly |
| Mark gap and NAV hit | Who carries it above market, and the cost | Arithmetic: (fund mark − Marden price) × par, divided by the fund's net assets | N-PORT, BDC schedules, Marden price | On each price or filing |
| Recovery | What lenders get if the tenant or customer walks | Collateral waterfall: GPU units × residual value from rental prices and depreciation, or powered-shell value per MW, minus senior claims | Debt stack, GPU prices, comparables | Weekly |
| Stress | What a shock does across every chain | Shock applied link by link (tenant rating, end-user funding, power delay), then cushion and price re-run | All of the above | Saved scenarios nightly; custom runs in seconds |
| Risk ranking | Which instruments to look at this week | Rule-based score (status, cushion, mark gap, recent slips) until the backtest supports a fitted model | Model outputs | Monday morning |

Odds and ranges show a "model v0" label until the backtest calibrates them.

## Real-time updates and fast pages

Target: every screen renders in under a second, with the server answering in under 100 ms.

- **Precomputed screens.** Each screen has its own read table (completion monitor rows, a site-detail document, an instrument-detail document), rebuilt only when its inputs change. A page load is one indexed query, never a join across the fact store.
- **Server-rendered first paint.** Next.js renders the page with data on the server, so it arrives filled in, not as a spinner.
- **Live updates without polling.** When a fact publishes or a model finishes, Postgres sends a notification; the API pushes a small update event over Server-Sent Events to any open screen showing that site or instrument; the screen refetches just that piece. Simpler and sturdier than WebSockets for updates that come minutes apart.
- **Caching.** Responses carry short cache lifetimes and are cleared by the same update events; Cloudflare sits in front for static assets.
- **Big tables stay fast.** Server-side sorting and filtering, virtualized rows, and chart series precomputed by the model worker.
- **Add Redis only if measured latency says so.** Postgres with the right indexes handles this volume.

## API endpoints

One versioned REST API serves the app now and customers later. Every value returns with its source, date and tag.

| Endpoint | Returns |
| --- | --- |
| `GET /v1/completion` | Completion monitor rows, filtered and sorted |
| `GET /v1/sites`, `/v1/sites/{id}` | Site list; full site detail: chain, schedule, evidence, debt |
| `GET /v1/instruments`, `/v1/instruments/{id}` | Instrument list and curve; full detail with holders and stress |
| `GET /v1/instruments/{id}/holders` | Holders, marks, gaps, NAV hits, filings |
| `GET /v1/payers/{id}` | A tenant's or end user's sites, debt and rent schedule |
| `GET /v1/risk-list?week=` | The week's ranked list and note |
| `GET/POST /v1/watchlist`, `GET /v1/alerts` | The user's names and alerts |
| `GET /v1/facts/{id}` | One fact with its source quote and document link |
| `GET /v1/stream` | Server-Sent Events: updates for open screens |

## What runs when

Times are Eastern. Events trigger work immediately; the clock is the backstop.

| When | What runs |
| --- | --- |
| Every 10 min | EDGAR new filings (8-K, N-PORT, 10-Q) → extraction |
| Hourly | FERC eLibrary dockets for watched sites |
| Daily, overnight | State permit portals, county agendas, court dockets, FRED curves; Watchdog consistency checks; snapshot archive; database backup |
| Daily, 5–6 am | End-of-day prices, cushions where inputs changed, mark gaps; screens rebuilt; daily email digest at 7 am |
| On any material fact | Approval request to you, then reprice, cushion and status update, alerts, live screen update |
| Weekly | Grid queue reports, satellite change scores, recovery, Researcher search for missing fields |
| Sunday night | Risk ranking and note draft |
| Monday 6 am | Note goes out after your approval |
