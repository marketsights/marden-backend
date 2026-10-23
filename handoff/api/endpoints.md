# Internal app endpoints

Served by the Node/Fastify backend to our own web app. Session auth: Clerk session token verified by the API; every route has a permission (spec 23, `packages/contracts/permissions.ts`). Not a customer API. All responses use the types in `types.ts`; all GETs accept `?asOf=YYYY-MM-DD` for snapshot reads.

| Method | Path | Screen | Returns |
| --- | --- | --- | --- |
| GET | `/api/home` | Home | headline numbers, watchlist changes, coming up |
| GET / PUT | `/api/home/layout` | Home, Customize | `HomeLayout` |
| GET | `/api/completion` | Completion | `CompletionResponse` (filters: endUser, tenant, holder, state, driver, cushion, watchlist) |
| GET | `/api/sites` | Sites | `SiteSummary[]` |
| GET | `/api/sites/:slug` | Site detail | `SiteDetail` |
| GET | `/api/sites/:slug/schedule` | Site detail (Schedule) | `SiteSchedule` |
| GET | `/api/sites/:slug/{evidence,power,opposition,events,debt,lease}` | Site detail tabs | tab data |
| GET | `/api/instruments` | Instruments | `InstrumentRow[]` (filter: type) |
| GET | `/api/curve` | Instruments, widget | `CurveTier[]` |
| GET | `/api/instruments/:id` | Instrument detail | `InstrumentDetail` |
| GET | `/api/instruments/:id/price-history?range=` | Instrument detail | price series |
| GET | `/api/instruments/:id/holders` | Instrument detail, widget | holders |
| GET | `/api/instruments/:id/moves` | Instrument detail | `InstrumentMoves` (contracts/moves.ts): 1D/1W/1M/QTD/YTD price and yield changes, what moved it |
| GET | `/api/moves` | Moves, Home card | `MovesPage` (contracts/moves.ts; filters: type, site, tenant, endUser) |
| GET | `/api/delivery` | Delivery record, payer pages | `DeliveryPage` (contracts/delivery.ts): who keeps having construction delays, per developer, issuer and tenant |
| GET | `/api/mispriced` | Mispriced, Home card | `MispricedPage` (contracts/delivery.ts): Marden price against the latest market evidence and the events since |
| GET | `/api/sites/:slug/debt-effects` | Site detail (Material events, Evidence) | `SiteDebtEffects` (contracts/moves.ts): each record's effect on the debt's value |
| GET | `/api/payers/:slug` | Payer | `Payer` |
| POST | `/api/stress/run` | Stress | `StressResult`, or `ContagionResult` for scenario `contagion` (spec 18) |
| POST / GET | `/api/stress/saved`, `/api/stress/saved/:id` | Stress, widget | saved scenario |
| GET | `/api/risk-list?week=` | Risk list, widget | `RiskList` |
| GET | `/api/watchlist` | Watchlist | `WatchItem[]` |
| POST / DELETE | `/api/watchlist/items`, `/api/watchlist/items/:id` | Watchlist, Watch buttons | — |
| GET | `/api/alerts` | Watchlist, Home | `Alert[]` |
| POST | `/api/alerts/read` | Watchlist | — |
| GET / PUT | `/api/alert-settings` | Watchlist, Account | `AlertSettings` |
| GET | `/api/alert-delivery` | Account | `AlertDelivery` (app contract): whether alert email is configured, the caller's recent alert emails and their status |
| GET / POST | `/api/email/unsubscribe?token=` | Alert emails | No session; HMAC-signed link for one member. GET shows a confirm page (changes nothing); POST (the button, or the RFC 8058 one-click POST) switches that email off |
| GET | `/api/track-record` | Track record | cases, rules, models |
| GET | `/api/sources/health` | Track record, sidebar footer | source heartbeats |
| GET | `/api/account` | Account | team, plan, security |
| POST / PATCH | `/api/account/invites`, `/api/account/members/:id` | Account | — |
| GET | `/api/export/:table.xlsx?…filters` | Every table | Excel file (data sheet + sources sheet) |
| GET | `/api/search?q=` | Sidebar search | sites, issuers, instruments, CUSIPs, data centers, buildings (label, parcel ID), plants, permit numbers |
| GET | `/api/data-centers` | Data centers | `DataCenterPage` (filters in spec 14; `?format=geojson` for the map) |
| GET | `/api/data-centers/summary` | Data centers | `DataCenterSummary` |
| GET | `/api/data-centers/:id` | Campus detail | `DataCenterDetail` |
| GET | `/api/data-centers/:id/buildings` | Campus detail (Buildings) | `Building[]` |
| GET | `/api/data-centers/:id/records` | Campus detail (Records) | `DcRecord[]` |
| GET | `/api/data-centers/:id/history` | Campus detail (History) | `DcHistoryEvent[]` |
| GET | `/api/power-plants` | On-site power | `PowerPlantPage` (filters in spec 15; `?format=geojson`) |
| GET | `/api/power-plants/summary` | On-site power | `PowerPlantSummary` |
| GET | `/api/power-plants/:id` | Plant detail | `PowerPlantDetail` |
| GET | `/api/sites/:slug/buildings` | Site detail (Buildings) | `SiteBuildings` |
| GET | `/api/obligations` | Obligation map | `ObligationGraph` (filters in spec 18) |
| GET | `/api/obligations/:company` | Obligation company page | `CompanyObligations` |
| GET | `/api/gpus` | GPU index | `GpuPricePoint[]` + today's `GpuProviderPrice[]` |
| GET | `/api/gpus/:model` | GPU model page | `GpuModelDetail` |
| GET | `/api/gpus/deployment` | GPU index: Delivered vs energized | `GpuDeploymentPage` |
| GET | `/api/instruments/:id/collateral` | Instrument detail (GPU facilities) | `GpuCollateral` |
| GET | `/api/power-plants/:id/debt` | Plant detail (Debt and contracts) | `PlantDebt` |
| GET | `/api/stream` | All screens | Server-Sent Events (`StreamEvent`): alerts, status, cushion, price and mark changes, pipeline health. Scoped to the user's session; heartbeat every 25 s; client reconnects with `Last-Event-ID`. |


## Auth (spec 23)

| Method | Path | Screen | Returns |
| --- | --- | --- | --- |
| GET | `/api/me` | all | user, active org, Marden role, plan, permission list |
| POST | `/api/webhooks/clerk` | — | 204; Clerk webhook (signature verified, idempotent) |
| PATCH | `/api/account/members/:id` | 13 Account | member with new role |
| DELETE | `/api/account/members/:id` | 13 Account | 204 |


## Payer capacity (spec 24)

| Method | Path | Screen | Returns |
| --- | --- | --- | --- |
| GET | `/api/payers` | Payers index | `PayersPage` |
| GET | `/api/payers/:slug/capacity` | Payer page: Capacity to pay | `PayerCapacity` |


## Lease map (spec 25)

| Method | Path | Screen | Returns |
| --- | --- | --- | --- |
| GET | `/api/leases` | Lease map | `LeaseMapPage` |
| GET | `/api/leases/:id` | Lease side panel | `LeaseRecord` |


## Project file (spec 26)

| Method | Path | Screen | Returns |
| --- | --- | --- | --- |
| GET | `/api/sites/:slug/project-file` | Site detail: Parties, Structure, Construction tabs; completeness score | `ProjectFile` |


## Capacity and GPU estimate (spec 27)

| Method | Path | Screen | Returns |
| --- | --- | --- | --- |
| GET | `/api/data-centers/:id/capacity` | Campus overview and Buildings tab | `CapacityEstimate[]` (campus first, then buildings) |
