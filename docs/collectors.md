# Structured collectors

The collectors for rates, SEC filings, EIA-860M, ERCOT large load, FERC eLibrary and Sentinel-2, as reviewed on 2026-10-21. Each one is `services/marden/collectors/<name>.py`. Each runs as `python -m marden collect <name>` and as a periodic task on the `collect` queue. The task registers in the collector's own module, and `marden.plugins.load_plugins()` imports it before the worker starts.

## Rules every collector follows

- **One run, one heartbeat.** Each run is one `heartbeat.run(source_id)`. A blocked source, a failed fetch or an unreadable file is recorded on `source` / `source_run`, and nothing is written in its place.
- **Raw first.** Every file that is parsed goes through `http.fetch_document`, or `documents.record_document` for POSTs, before the parse. The exceptions are listed in the table. They are files that are not kept, and each is counted in the run meta.
- **Reruns write nothing new.** Writes go through `FactWriter`, `PriceWriter`, `EventWriter` and `HoldingWriter`, which dedupe and never overwrite. Every collector was run twice against the live DB on 2026-10-21, and the second run wrote nothing (see the results below).
- **SEC limits.** All SEC hosts (www.sec.gov, data.sec.gov, efts.sec.gov) share one token bucket of at most 5 requests a second: `http.SEC_MAX_PER_SECOND`, from price-sources.md §5. Every request carries `SEC_USER_AGENT` (`Marden research cody@marden.ai`). No collector calls `/cgi-bin` or `/search/`.
  - The bucket is per process. The worker is one process, so its SEC collectors share 5 req/s. A manual CLI run alongside the worker adds up to another 5, which is still within the SEC's 10 a second "regardless of the number of machines".
- **Other hosts** get the `MardenBot/0.1 (...; cody@marden.ai)` User-Agent and the per-host limit named in each row.

## Collectors

| Collector (source id) | Source and endpoint | Cadence (task, cron UTC) | Terms clause relied on | Outputs | Tag |
|---|---|---|---|---|---|
| `ust_par_curve` | U.S. Treasury daily par yield curve. One CSV per year: `https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{YYYY}/all?type=daily_treasury_yield_curve&field_tdr_date_value={YYYY}&page&_format=csv`. Each run fetches the current year, plus backfill years from 2025 not yet loaded. 1 req/s. | Daily 18:30 ET. `collect.ust_par_curve`, `30 22,23 * * *` with an ET guard. | robots.txt: "User-agent: * Allow: /". The site policies have no copyright or reuse clause. "Copyright protection under this title is not available for any work of the United States Government" (17 U.S.C. §105). | Facts on series `ust_par_{1m,6w,2m,3m,4m,6m,1y,2y,3y,5y,7y,10y,20y,30y}`, field `value` (pct). `valid_from` is the curve date. Blank cells are skipped. | F, "U.S. Treasury" |
| `nyfed_sofr` | NY Fed Markets API, one calendar year per request: `https://markets.newyorkfed.org/api/rates/secured/sofr/search.json?startDate={YYYY}-01-01&endDate={YYYY}-12-31`. The year URLs are stable, unlike `last/1.json`. Each run fetches the current year plus a 2-year backfill. 1 req/s. | Daily 08:30 ET. `collect.nyfed_sofr`, `30 12,13 * * *` with an ET guard. | NY Fed Terms of Use (Last Updated 6/9/2023): "The New York Fed grants you a non-exclusive license, subject to the Terms, to use, copy, and distribute Content for your personal or business purposes. You may: Access the Content, manually or through an automated process or device…". The required reference-rate notice is stored. | Series `sofr`, field `value` (pct) per effective date. Plus fact `notice`: the NY Fed and DTCC Solutions text, which must go with any display. | F, "Federal Reserve Bank of New York" |
| `fred` (`fred_ice_bofa`) | ICE BofA OAS series on FRED: BAMLH0A0HYM2, BAMLH0A1HYBB, BAMLH0A2HYB, BAMLH0A3HYC, BAMLC0A4CBBB and BAMLC0A0CM. `https://fred.stlouisfed.org/graph/fredgraph.csv?id=<ID>`, or the API `https://api.stlouisfed.org/fred/series/observations` when `FRED_API_KEY` is set (the key is sent, never stored). One stable URL per series, 1 req/s. | Daily 18:00 ET. `collect.fred`, `0 22,23 * * *` with an ET guard. | FRED legal: "Copyrighted: Pre-approval required … these series may only be used for non-commercial educational or personal use. For any other use, you must obtain permission from the copyright holder". ICE: "Reproduction of this data in any form is prohibited except with the prior written permission of ICE Data Indices." **Not blocked: collected by the business owner's decision of 2026-10-21** as model inputs only, licence-flagged `unlicensed-commercial` (`marden.core.licensing`). | Series `ice_bofa_us_{hy,bb,b,ccc,bbb,ig}_oas`, field `value` (pct) per observation date, plus `display_allowed` = false and `copyright_notice` (the ICE notice) on each series, so the values are never shown. | R, "ICE Data Indices, LLC via FRED" |
| `sec_index` | Daily form index `https://www.sec.gov/Archives/edgar/daily-index/{YYYY}/QTR{q}/form.{YYYYMMDD}.idx`. Submissions `https://data.sec.gov/submissions/CIK##########.json`, plus older pages. Filing index `…/Archives/edgar/data/{cik}/{acc}/{acc}-index.htm`, the primary document and EX-4, EX-10 and EX-99 exhibits. Watched CIKs are every entity with a CIK. Fund registrants (kind `fund`) are matched in the daily index only; their submissions are not polled. | Every 10 min, weekdays 06:00–24:00 ET. `collect.sec_index`, `*/10 10-23 * * 1-5` and `*/10 0-4 * * 2-6` with a guard. `collect.sec_index_backfill` runs on demand. | SEC: "Information presented on sec.gov is considered public information and may be copied or further distributed by users of the web site without the SEC's permission." Fair access: "Current max request rate: 10 requests/second." and "Please declare your user agent in request headers". robots.txt disallows `/cgi-bin` and `/search/`. | Documents: 8-K, 424B\*, S-1 and F-1 documents and every EX-10 get `extract_state='queued'`. Events of kind `filing` (published) go on the filer entity, the instruments it or its direct subsidiaries issued, their sites, and the sites it develops. XBRL, graphics and other exhibits are skipped and counted. | F, "SEC EDGAR" |
| `sec_nport` | Daily index (NPORT-P, NPORT-P/A), plus the DERA quarterly zip `https://www.sec.gov/files/dera/data/form-n-port-data-sets/{YYYY}q{n}_nport.zip`, then `…/Archives/edgar/data/{cik}/{acc}/primary_doc.xml`. Only filings that hold a watched CUSIP, ISIN or LoanX are stored. The rest are counted (`filings_skipped_no_match`, `bytes_skipped`) and dropped. | Daily 07:00 ET. `collect.sec_nport`, `0 11,12 * * *` with an ET guard. `collect.sec_nport_backfill` runs on demand. | The same SEC clauses, plus the N-PORT data sets page: "The data sets will be updated quarterly." Marks are cited "as filed on Form N-PORT by <fund>". | Holdings (N-PORT) with holding facts: `public_date`, `units`, `cur_cd`, `exchange_rt`, `fx_convention`, `fair_value_level`, `asset_cat`, `unfunded_commitment`, and `mark_price` for non-USD lines. A `fund_mark` price per priced position. A cross-fund median "N-PORT median of N funds (<period>)". Instrument fact `fund_mark_dispersion(_cusip)`. Fund fact `net_assets_usd`. `valid_from` / `obs_date` is the report period. | F. M (`nport-fx-direction@0.1`) for `fx_convention` |
| `sec_bdc` | BDC Data Sets page `https://www.sec.gov/data-research/sec-markets-data/bdc-data-sets`, plus zips `https://www.sec.gov/files/datastandardsinnovation/data/business-development-company-bdc-data-sets/{YYYY}_{MM}_bdc.zip` (`soi.tsv`, `datasets/tag.tsv`). By default only months from 2025-10 are fetched. | Monthly on the 9th, and daily Nov 1–20, Feb 15–Mar 10, May 1–20 and Aug 1–20. `collect.sec_bdc`, `17 11 * * *` with `due_on`. | BDC page: "The data has been automatically and directly taken from submissions created by the registrants and provided as filed with the Commission. The data sets will be updated monthly." Plus the SEC clauses. | Holdings (10-Q/10-K) for the followed AI-infrastructure borrowers; collisions such as Vantage Specialty Chemicals are excluded. Holding facts `cost_usd`, `spread_bp`, `interest_rate_pct`, `maturity`, `unfunded_commitment_usd`. On tranche-linked lines, a raw `fund_mark` of 100×FV/par, refused when cost/par is outside 0.5–1.5. | F (raw). M (`bdc-unfunded-adj@0.1`) for the unfunded-adjusted mark |
| `sec_xbrl` | `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json` for operating-company targets (payers, neoclouds, developers, chipmakers, power). The filer name must match the entity, or nothing is written. | Weekly, Sunday 09:23 UTC (`collect.sec_xbrl`, `23 9 * * 0`). Also `collect.sec_xbrl_on_filing` (`2-59/15 * * * *`) for CIKs with a new 10-Q or 10-K `filing` event. | SEC developer APIs page: "These APIs do not require any authentication or API keys to access." Plus the SEC clauses. data.sec.gov has no robots.txt (404). | Entity facts. Durations `<base>_{3,6,9,12}m_usd` (capex, operating cash flow, revenue …). Instants `<base>_usd` (cash, debt, leases, CIP, PP&E, purchase obligations). Share counts and `public_date`. `valid_from` is the period end, `quote_loc` is the concept and filing, and the quote is the companyfacts record. | F |
| `eia_860m` | robots.txt and the release page `https://www.eia.gov/electricity/data/eia860m/`, then the newest workbook `/electricity/data/eia860m/xls/<month>_generator<year>.xlsx`. Earlier months sit under `/archive/` and are robots-disallowed, so they are recorded in run meta; a person can load them with `--import-dir`. 2 req/s. | Weekly, Fridays 13:37 UTC (`collect.eia_860m`, `37 13 * * 5`). | EIA Copyrights and Reuse: "U.S. government publications are in the public domain and are not subject to copyright protection. You may use and/or distribute any of our data…". robots.txt has "Disallow: /*archive/" and is re-read every run. | `eia_generator` facts, change-only: `generator`, `status`, `planned_online`, `operating_month`, `retired_month`. `valid_from` is the report month's end. Matched plants get `planned_online` and `eia_status`. Slips are counted in run meta. | F. M (`eia860m-plant-match@0.1`) on name+location plant matches |
| `ercot` | LLWG page `https://www.ercot.com/committees/tac/llwg`, then meeting pages `/calendar/MMDDYYYY-LLWG-Meeting`, then status-update decks under `/files/docs/…` (pptx or pdf). robots.txt is checked for every URL. 0.2 req/s. An Imperva challenge stops the run. | Weekly, Mondays 13:41 UTC (`collect.ercot`, `41 13 * * 1`). | ERCOT Terms of Use §5: "The publicly available contents of this website may be used, reproduced, and redistributed… raw data provided in public portions of this website may be used, reproduced, and redistributed in compilations, charts, and analyses without maintaining such notices." §6: "Use of this website in a manner that negatively affects the performance of this website… is prohibited." | Series `ercot_large_load_approved_to_energize_mw`, `_peak_nonsimultaneous_mw` and `_peak_simultaneous_mw`, field `value` (MW), with ERCOT's sentence as the quote and the slide or page as the location. Decks are queued for extraction (the charts are pictures). No per-project rows are written: ERCOT publishes none. Unchanged HTML pages are not re-stored, because they carry per-request tokens. | F, "ERCOT" |
| `ferc` | eLibrary JSON API used by the web app: `POST https://elibrary.ferc.gov/eLibraryWebAPI/api/Search/AdvancedSearch` per watched docket, over a trailing 30-day window, and `POST …/api/File/DownloadP8File` for each new filing's main public file. 0.5 req/s. Watched dockets come only from seed facts and events. | Hourly at :23 (`collect.ferc`, `23 * * * *`). | 18 CFR 388.106(a): "Publicly available documents may be obtained electronically from the Commission's website". eLibrary PIA: "All end users have read access to the public data in the eLibrary repository." elibrary.ferc.gov has no robots.txt (404). FERC's website-policy pages answer with a Cloudflare challenge, so they could not be read; the challenge was not bypassed. | Documents: search pages, and main files. Orders, deficiency letters, in-service notices and certificate filings are queued. Events of kind `docket` go on the docket's sites, plants and campuses. A material title becomes a candidate event plus a `review_item`. | F, "FERC eLibrary" |
| `sentinel2` (`sentinel2_pc`) | Planetary Computer STAC `GET https://planetarycomputer.microsoft.com/api/stac/v1/search` (collection sentinel-2-l2a). The search pages are not stored, because their key order changes on every call; they are counted as `search_pages`. The chosen scene's item JSON (`…/collections/sentinel-2-l2a/items/<id>`) is stored. COG windows are read through SAS-signed URLs held in memory only; they are not stored, and are counted as `scl_reads` and `band_window_reads`. 2 req/s API, 4 req/s blobs. | About every 5 days, 07:17 UTC (`collect.sentinel2`, `17 7 */5 * *`). | PC Supplemental Terms (Jan 2023): "Previews are not meant for production use (though such use is not prohibited)". Tokens "may not be sublicensed to, distributed to, or shared with third parties". Sentinel legal notice: "users shall have a free, full and open access to Copernicus Sentinel Data". The caption carries "Contains modified Copernicus Sentinel data [Year]". | Site and campus facts. `imagery` holds {month, url, caption, scene_id}, `valid_from` = acquisition date. `change_score` and `last_visible_change` are computed against the pick about 3 months earlier. | R (imagery). M (`change-score@0.1`) for the score and the last change |

## What still needs a key, a licence or a decision

| Item | Needed for | Status |
|---|---|---|
| ICE Data Indices written permission, or a direct ICE licence | `fred` (ICE BofA OAS tier-curve inputs) | Collected without it by the owner's decision (2026-10-21), never displayed, licence-flagged. Legal should also read FRED's clause on "development or training of any software program or system or machine learning". |
| `FRED_API_KEY` | `fred`, optional: the public `fredgraph.csv` path works without it | Not set. Optional: the API path avoids the public CSV download, nothing else changes. |
| Legal sign-off on republishing N-PORT and BDC marks "as filed by <fund>" | `sec_nport`, `sec_bdc` display | Open (price-sources.md §6.5a). |
| A monitored contact in `SEC_USER_AGENT` (`cody@marden.ai`) | Every SEC collector | Confirm it is monitored (price-sources.md §6.7). |
| ERCOT MIS paths `/misapp` and `/misdownload` (robots.txt disallows both for every agent) | `ercot_gis` (GIS Report, EMIL PG7-200-ER, the only place ERCOT posts it) | Collected by the owner's decision (2026-10-21) at low volume: User-Agent "Marden research cody@marden.ai", one request per 5 s, weekly, only reports not already stored, and the run stops on any challenge, 403 or CAPTCHA. Recorded in the source's terms_note. |
| EIA-860M archive months (robots.txt disallows `/*archive/`) | `eia_860m` backfill, which slip history needs | Only the newest month (August 2026) is loaded. A person can download earlier months and run `collect eia_860m --import-dir DIR`. |
| FERC website policies, which sit behind a Cloudflare challenge | `ferc` terms | The policies could not be read, and the challenge was not bypassed. The public-records basis (18 CFR 388.106) is quoted instead. |
| Microsoft Planetary Computer is a Preview service | `sentinel2` | Allowed ("not prohibited"). It may be rate-limited or withdrawn. |

None of these collectors needs a credential (`FRED_API_KEY` is optional). The `ERCOT_API_*` keys are for `ercot_prices` only; `ercot` and `ercot_gis` (MIS, no credential) need none. None of them reads or writes FINRA TRACE, Börse Frankfurt or Yahoo data.

## Known limits

- **Dirty-mark deadlocks.** Long writer transactions mark `read_model_dirty` and `model_dirty` rows in different orders, so two loaders writing at once can deadlock (seen between `sec_nport`, `sec_bdc` and the seed loader). Postgres aborts one transaction; nothing is half-written.
  - `sec_nport` and `sec_bdc` retry a deadlocked filing. Other collectors record a failed run and catch up on the next one.
  - The lasting fix is to buffer the marks and write them sorted at commit, in `marden.readmodels.deps`.
- **Write speed.** Every published value runs the dependency lookups in `marden.readmodels.deps` (and the model triggers). Lookups that filter `status IN ('published', 'superseded')` could not use the published-only index, and holding lookups compared `id::text`, so each was a full scan, run per written value; the N-PORT pass wrote about 1 holding a second. Fixed on 2026-10-21 by the index `fact_subject_known` (migration 20261021200000) and integer holding ids (`via(..., int_id=True)`); the same pass then wrote about 8 a second.
- **N-PORT exchange rates.** Filers disagree on which way the exchange rate points, so `sec_nport` prices a non-USD line only when its rate matches the period's majority rate across filings, or that rate's inverse. Some of those votes come from filings that hold no watched instrument and are not stored; the run meta (`fx`) records the votes and the reference rate per currency and period. `fx_convention` is Marden's reading of the direction, not a filed value, so it is written with tag M and method `nport-fx-direction@0.1`. The 51 rows written before that fix (2026-10-21) keep tag F as history.
- **XBRL coverage.** `sec_xbrl` reads companyfacts only, which leaves out facts on an axis and company-specific concepts. So there are no leases-not-yet-commenced figures, and no CIP for CoreWeave, Nebius or IREN. `fact_current` can hold an old value for a field a filer stopped reporting, so check `valid_from`.
- **BDC gaps.**
  - The in-season 10-Q/10-K iXBRL path (price-sources.md §5) is not built.
  - Palmer Square's unfunded amount is given only in a footnote, so there is no adjusted mark for it.
  - The Franklin BSP par is kept as filed (479,468,000 against cost 466,000), and its mark is refused.
- **Entity data.**
  - `blackrock-funds` (kind `developer`) carries CIK 844779, which is the BlackRock Funds mutual-fund trust: its companyfacts file is empty, and `sec_index` polls it.
  - `valor-compute` no longer carries the Apollo trust's CIK 2052153.
- **ERCOT** publishes only aggregate large-load figures, and none since June 2026, so nothing is matched to a site.

## Live verification (local DB, 2026-10-21)

Each collector was run twice in a row with `python -m marden collect <name>`, one process at a time for the SEC collectors, between 17:16 and 18:45 UTC. The counts are rows the run wrote, by producer, source and `learned_at`. The builders' first loads earlier in the day are in their reports; an earlier review pass at 16:02–16:08 UTC also found every collector idempotent (`sec_index` then stored 258 documents, 43 queued, and 30 `filing` events for new filings and newly added BDC CIKs on its first run, and nothing on its second).

| Collector | Run 1 | Run 2 |
|---|---|---|
| `ust_par_curve` | 0 documents, 0 facts. The 2026 file is unchanged (2,814 values read); 2025 is skipped as loaded. | 0 documents, 0 facts |
| `nyfed_sofr` | 0 documents, 0 facts. The 2026 file is unchanged (201 values read); 2024 and 2025 are skipped as loaded. | 0 documents, 0 facts |
| `fred` | **Failed, recorded on the source.** fred.stlouisfed.org answered every URL, robots.txt included, with a "Site Maintenance" HTML page. The page was stored (1 document) and refused by the parser (`ParseError: unexpected header ['<!doctype html>']`); 0 facts. | Failed the same way: 0 documents (the same page), 0 facts |
| `sec_index` | 0 documents, 0 events. 38 relevant filings in the 7-day window, all already recorded; 216 submissions files polled; the 10/21 daily index is not yet published. | 0 documents, 0 events |
| `sec_xbrl` | 0 documents, 0 facts (34 companyfacts files unchanged). | 0 documents, 2,525 facts: the payer-capacity work added fields to the map at 18:15 (`operating_income`, `depreciation_amortization`, `depreciation`, `amortization_intangibles`, `remaining_performance_obligation`), read from the stored files. A third run wrote 0 facts. |
| `sec_bdc` | 0 documents, 0 holdings, 0 facts, 0 prices (all ten zips 304). | Same |
| `sec_nport` | The full 2026q3 pass. The previous review's run stored its 1,465 filings and was then stopped by the spend limit before writing; this run wrote them from the stored files: 15,873 holdings, 93,604 facts, 15,667 position marks and 602 medians, 0 new documents. (An earlier attempt, run 211, wrote 1,677 holdings at the old write speed and was interrupted to pick up the write-speed fix under Known limits.) | 0 documents, 0 holdings, 0 facts, 0 prices: the data set and every index day are done |
| `eia_860m` | 0 documents, 0 facts. The August 2026 workbook is unchanged (3,756 generator rows read). | 0 documents, 0 facts |
| `ercot` | 0 documents, 0 facts. 14 meetings listed, 3 meeting pages read, 18 figures already stored. | 0 documents, 0 facts |
| `ferc` | 0 documents, 0 events (48 filings in the watched dockets, all recorded). | 0 documents, 0 events |
| `sentinel2` | 0 documents, 0 facts (80 picks; 269 scores reused). | 0 documents, 0 facts |

Ten random rows per collector were checked against the raw stored document by an independent parser: the value, date, tag, source and URL. All matched. The checker raised two false alarms of its own, both confirmed correct by hand: a BDC line whose prior-year row has the same par and fair value, and a EUR N-PORT line whose mark is in euros (100 × valUSD × exchangeRt / balance with exchangeRt 0.8752 EUR per USD). `fred` has no rows to check yet.

Totals from these sources on 2026-10-21 18:45 UTC (documents / queued for extraction / events / holdings / prices): `sec_index` 338 / 74 / 195 / 0 / 0, `sec_nport` 1,993 / 0 / 0 / 22,720 / 22,728 (plus 1,035 "N-PORT median" prices, a name the holdings seed loader shares), `sec_bdc` 11 / 0 / 0 / 317 / 109, `sec_xbrl` 35 documents and 15,456 facts, `ust_par_curve` 2 documents and 6,269 facts, `nyfed_sofr` 3 and 700, `eia_860m` 4 and 3,756, `ercot` 20 (6 queued) and 18, `ferc` 57 (8 queued) and 15 events, `sentinel2` 287 documents and 1,281 facts, `fred` 1 document (the maintenance page) and no facts.

## Air permits (`air_permits`)

State air permits for our power plants (spec 15 "Air permit card", spec 20). One collector, `services/marden/collectors/air_permits.py`, with one adapter per state permit system Marden may read: `air_permits_tx.py` (+ `air_permits_tx_parse.py`), `air_permits_nm.py`, `air_permits_ia.py` and `air_permits_la.py`. The shared parts are `air_permits_match.py` (plant profiles and the match rule), `air_permits_text.py` (PDF text, dates, units and emission statements), `air_permits_store.py` (facts and events) and `air_permits_systems.py` (every state with a plant, its permit system, and why it is or is not collected).

- **Run:** `python -m marden collect air_permits [--state TX --state LA]`. Task `collect.air_permits` runs weekly, Wednesdays 12:29 UTC (`29 12 * * 3`).
- **Rules:** each adapter is one heartbeat on its discovery source; Records Online gets its own. The User-Agent is `Marden research cody@marden.ai`, set per host through the project's `Fetcher` with no retries. A 401, 403 or 429 or a challenge page stops that adapter, is recorded on its `source_run`, and nothing is written in place of the record. Robots-disallowed hosts are never requested. Raw pages and PDFs are stored before parsing. Pages with per-request tokens, and Socrata pages whose rows come back in a different order, are stored again only when their rows change. Stored files are never fetched again.

| State | Source id | Endpoints and limits | Terms relied on (terms_note) | What is read |
|---|---|---|---|---|
| TX | `tceq_cr_open_data` | TCEQ Central Registry Files on the Texas Open Data Portal: `https://data.texas.gov/resource/{msah-s2rv,5eqq-7nad,t34q-qzi3,tzyg-j7q4,9iad-hrn8}.json`. One SoQL query per regional data set, ORing the operator, owner and project names of our Texas plants and the permit numbers they cite. Up to 4 pages of 5,000 rows. 1 req/s. | data.texas.gov robots: `Crawl-delay: 1`, with Disallow rules only on `/browse` search pages. TCEQ Website Policies: "Unless otherwise noted, content of our site is considered "public domain."" | Regulated entities (RN), county, NSR and Title V permit numbers with status, and the customers on each permit. An RN counts as a power plant only if a record says it generates power (NAICS 2211xx, "electric generation", "EGU", "power station", ...). |
| TX | `tceq_records_online` | `https://records.tceq.texas.gov/cs/idcplg`. The flow is `TCEQ_SEARCH` (session cookie), then `TCEQ_SEARCH_ADD_ACCESS` (as the page does), then `TCEQ_PERFORM_SEARCH&QueryText=...&ResultCount=200`, per RN and per permit number of a matched facility. Files come from `TCEQ_EXTERNAL_SEARCH_GET_FILE&dID=..&Rendition=Web&searchType=External`. At most 60 downloads a run, 1 request per 2 s. | No robots.txt (404). TCEQ public-domain clause; "Maintaining Public Access: ... we reserve the right to block their access". | Permit families (a numeric permit plus its PSDTX, GHGPSDTX and HAP ids), dated actions (application, notice, draft, technical review, decision, withdrawal), technical reviews (units, models, OEM, MW ratings, emission totals, received date) and final-action letters (expiry, application date). Copies that are not public come back as an HTML metadata page; those pages are stored so they are not fetched again. |
| NM | `nmed_aqb` | WordPress REST on www.env.nm.gov. Notices: `/public-notices/wp-json/wp/v2/search` and `/posts/<id>`. OPF dockets: `/opf/wp-json/wp/v2/...`. Legal-notice and application PDFs come from `service.web.env.nm.gov` and `cloud.env.nm.gov`. 1 request per 2 s. | robots: "User-agent: * Disallow: /wp-admin/". Notices: "The permit application is available for review on the AQB Public Notices of Permitting Actions website". | Applicant, facility, county and permit number from the legal notice. Its generation sentence, units (model, OEM) and the emissions the application estimates (with the basis stated). Dated actions: application received, completeness letter, draft, hearing. |
| IA | `iowa_dnr_aqrr` | `https://idnraqrr.iowadnr.gov/ConPermitSearch`: plant-name autocomplete, then the plant page `ConstructionPermit?id=<n>`, then `DownloadFile` permit-collection PDFs (programs.iowadnr.gov). 1 request per 2 s. | No robots.txt (404). Iowa DNR notices: "The public may review the following air quality construction permits available online at https://idnraqrr.iowadnr.gov/ConPermitSearch". | Plant number, county, permits by project and emission point with issue dates. From the collection PDF: company, project, process, emission units and capacities, and emission limits (Table 2). Later projects become amendments. |
| LA | `ldeq_public_notices` | `https://www.deq.louisiana.gov/public-notices?q=<operator name>`: one keyword query per operator phrase of our Louisiana plants. 1 request per 2 s. Attachments link into EDMS and are not followed. | robots: "User-Agent: * Disallow: /admin". "Permit records are available for review at the DEQ Public Records Center". The privacy page states no reuse restriction. | Air Quality rows: company and facility, AI number, permit numbers (Part 70, PSD-LA, acid rain), subject, publication, hearing and issue dates. The parish is taken from the facility name only when it says "<Name> Parish". |

**Match rule** (`air-permit-match@0.1`). A facility is tied to a plant only when all three of these hold:

1. **Name.** An operator, owner, party, served-campus or project name of the plant appears in the facility's names or permit customers. This is a whole-phrase token match. Legal suffixes, notes and representatives are dropped, and 3-letter acronyms such as CPV are kept.
2. **Generation.** The record says the facility generates power.
3. **Location.** One of: the county or parish agrees (from the plant row, its served campuses, or a "<X> County" in its name or notes, checked against the Census list); or a permit number the plant's own records cite; or the project name (for example "Project Llano").

A county that conflicts rejects the facility. When two plants have equal evidence, the facility goes to neither. A plant takes a second facility only if each facility it takes brings a cited permit number or the project name. Profiles never read this collector's own facts.

Everything that is not matched, but bears a plant's name with generation, a cited number or a project name, goes to the run report (`source_run.meta.coverage.unmatched` and the CLI) with the reason it failed. It never becomes a fact.

**Outputs** (tag F, `source_name` = the agency, `document_id` = the stored record, verbatim `quote`, `method_version` = `air-permit-match@0.1`, `valid_from` = the run day; a field is written only when its value changed):

- `air_permit`: the primary permit. It is ranked PSD/NSR, then Title V, then standard/PBR; live before withdrawn; then the latest activity. It carries the number, ids, type, status and agency status, applicant, filed, issued and expires dates, and units (count, model, OEM, MW each and total, each with its quote and URL). It also carries emission limits or estimates with their basis, amendments, the facility, and the match evidence.
- `air_permits`: every matched permit.
- `air_permit_number`: all ids joined.
- Events of kind `permit` on the plant: one per dated action, titled `"<agency> air permit <number>: <action> (project <n>)"`.

The plant card reads them through `facilities_plants.air_permit` (`filed`, `decided`, `amendments`; types PSD becomes Major, NSR/PBR become Minor).

**States with plants and no adapter** (each recorded in the report; the blocked ones are declared as sources with `blocked_reason`):

| State | System | Status |
|---|---|---|
| TX | TCEQ APAD, NSR project reports, CID, Central Registry query (www2/www14/www15/www17) | Blocked: robots.txt `User-agent: * Disallow: /` (`tceq_air_permits_web`). The same data come from data.texas.gov and Records Online. |
| LA | LDEQ EDMS | Blocked: robots.txt `User-agent: * Disallow: /` (`ldeq_edms`). |
| KS | KDHE Bureau of Air | Unreachable: every page, robots.txt included, answers Cloudflare 403 "Sorry, you have been blocked" (`kdhe_air`). Kansas Register notices are a v2 candidate. |
| TN | Shelby County Health Department (xAI Memphis) | Unreachable: Cloudflare 403 (`shelby_county_air`). TDEC: n/a (the other Tennessee plant is a reactor). |
| MN | MPCA air permits | Unreachable: Radware "Verifying your browser" challenge (`mpca_air`). |
| IN | IDEM permits online | Unreachable: 403 "Access is denied" (`idem_air`). |
| VA | Virginia DEQ | Unreachable: Akamai 403 on every page, robots.txt included (`va_deq`). |
| PA | DEP eFACTS | Unreachable: incomplete TLS chain. Verification is never disabled (`pa_efacts`). Regional files and PA Bulletin notices are v2. |
| OH, ND, NE, OK, MS, WY, WI, SD | Ohio EPA eDocs, NDDEQ notices, Nebraska DWEE notices (the old deq-iis.ne.gov host reset the connection), Oklahoma DEQ, MDEQ enSearch, Wyoming DEQ, Wisconsin DNR, SD DANR | Reachable; no adapter yet (v2). |
| IL, WA | Illinois EPA, Washington Ecology/EFSEC | n/a: nuclear PPAs and uprates, and an advanced reactor. There is no combustion permit to look for. |

### Live results (local DB, 2026-10-21)

`python -m marden collect air_permits` (every state) ran three times in a row, at 20:30, 20:35 and 20:42 UTC. Each followed the earlier TX/NM/IA runs that wrote this collector's rows at 20:14–20:17.

- **Run 1:** 0 documents, 0 facts, 0 events.
- **Run 2:** 1 document: a Central Registry page whose rows came back from Socrata in a different order. The page was then made change-only on its row set, with a test.
- **Run 3:** 0 documents, 0 facts, 0 events.

All 15 adapter runs were ok. Stored in total: `tceq_cr_open_data` 27 documents, `tceq_records_online` 68 (32 queued for extraction), `nmed_aqb` 23 (2 queued), `iowa_dnr_aqrr` 3 (2 queued), `ldeq_public_notices` 1. Facts: 24 (`air_permit`, `air_permits` and `air_permit_number` on 8 plants). Permit events: 63 (TCEQ 52, NMED 9, Iowa DNR 2).

| State | Plants | Facilities with power generation found | Permits found | Plants matched (permits) | Unmatched candidates |
|---|---|---|---|---|---|
| TX | 19 | 28 (from 2,365 search rows without generation or name evidence) | 76 | 6 (15): n-abilene-onsite, n-frontier-shack, p-089, p-090, p-098, p-110 | 22 |
| NM | 3 | 1 | 1 | 1 (1): n-jupiter-microgrid | 0 |
| IA | 7 | 1 | 1 | 1 (1): p-050 | 0 |
| LA | 2 | 9 | 9 | 0 | 9 |
| 16 other states | 43 | – | – | – | Not collected (table above) |

**Spot checks against the stored records:**

- **n-abilene-onsite** (RN112029061, Longhorn Data Center, Taylor County). Primary permit 182126 (+ PSDTX1688, GHGPSDTX263, HAP89): PSD, pending. Also on the plant:
  - 177263, Standard, issued, expires 2034-08-21. Technical review 387309: "Project Received Date January 11, 2025"; 5 Solar Titan 350 at 38 MW and 5 GE Vernova LM2500 at 34.1 MW. The final action is dated 2025-01-22.
  - 177262 (PBR).
  - 182267, withdrawn 2026-04-20.
  - O4721 (Title V).
- **p-089** (Fermi, RN112258678, Carson County). PSD 181009, issued 2026-02-27, technical review received 2025-08-01. 90 Siemens Energy SGT-800 among the units; NOx 1,741.75 tpy and CO2e 23,964,782.74 tpy as the review states them. 183462 is pending.
- **p-098** (GW Ranch, RN112259775): PSD 181033 issued 2026-01-21.
- **p-110** (CPV Basin Ranch, RN111876330): PSD 175063 issued 2024-11-25, plus O4946.
- **p-090** (Project Llano, RN112229901): PSD 182880 pending, plus 182124 (PBR) and O4965. The generation evidence came from Records Online, because the Central Registry rows had none.
- **n-frontier-shack** (RN112247101): PSD 182467 pending, plus Standard 180842.
- **n-jupiter-microgrid**: NMED NSR 10883 (YGI Microgrid, Doña Ana County). Pending, application received 2026-04-27. Bloom Energy Server; NOx 37.2 tpy, as "estimated in the application (legal notice)".
- **p-050** (Orient Energy Center, Adair County): Iowa DNR 26-A-117 to 26-A-121, issued 2026-04-30. Project 26-153 (CT-3, 26-A-246) is an amendment; NOx 235.42 tons/yr combined.

**Unmatched, in the report:**

- **Louisiana.** Richland Parish 1 and 2 (AI 249132, PSD-LA-869) and 3 and 4 (AI 249365, PSD-LA-871) tie between n-hyp-ccgt1 and n-hyp-exp7: same operator, same parish. Franklin Farms (PSD-LA-862) and Waterford 5 and 6 (PSD-LA-865) have no parish in their names.
- **Texas.**
  - El Paso Electric: five facilities in El Paso County, so which is the Meta unit's is not guessed.
  - Pacifico: B & W Ranch (Bastrop) and Spunky (Hood).
  - VoltaGrid, Vistra and Crusoe: sites in counties other than our plants'. Klondike Pad 1 is in Dawson.

**Known limits:**

- NMED's legal notice says "Permit No. 10883", while its OPF docket index file says "NSR 10833". 10883 is written.
- Technical reviews that Records Online does not make public cannot be read. Up to 3 copies are tried per project.
- Louisiana needs a permit number or a project name on the Hyperion plants before its notices can be tied to them.
