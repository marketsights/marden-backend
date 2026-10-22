# Source licences and the unlicensed-price switch

Written 2026-10-21. Code: `services/marden/core/licensing.py`. Setting: `MARDEN_SHOW_UNLICENSED_PRICES`.

## 1. The decision

[price-sources.md](price-sources.md) found that three market-data sources Marden can reach do not allow commercial use without a licence. It recommended against using them.

The business owner was told those terms and decided on 2026-10-21 that Marden collects them daily and uses them in the product. The owner accepts the risk.

So Marden does three things:

1. **Collects the data.** No source is marked `blocked`.
2. **Records the terms.** Each source's `terms_note` quotes the clause and gives its URL.
3. **Flags every row.** Each row from these sources carries an exact `source_name`. One switch can remove all of them, and should when a licence is bought or the decision changes.

## 2. Sources and their status

`SOURCE_LICENCE` maps each exact `source_name` to `{status, note}`. Every `price_obs.source_name` and `fact.source_name` the collectors and loaders write uses one of these strings.

| `source_name` | Status | Written by | Terms clause (checked 2026-10-21) | Licensed route |
|---|---|---|---|---|
| `FINRA TRACE (finra.org)` | unlicensed-commercial | `collect finra_trace` (finra.org bond pages and the backend they call), `seed legacy_trace` (the archive's finra.org pull) | https://www.finra.org/terms-of-use (last modified Nov 9, 2023): content "shall be used ONLY for your own non-commercial personal or professional use". It forbids anyone to "develop or create a database of data using the FINRA Website", to "use any data mining, scraping or harvesting tools (including robots)", or to use it "in conjunction with any machine learning ... predictive analytics". | FINRA Rule 7730 TRACE subscription (Corporate Bond, Rule 144A, Securitized Products Data Sets) |
| `Börse Frankfurt` | unlicensed-commercial | `collect boerse_frankfurt`, `seed legacy_frankfurt` | https://live.deutsche-boerse.com/disclaimer, "Download Price History": "Any access to and use of the FWB-Data is subject to a licence which is only granted free of charge if the FWB-Data User does not commercialize the FWB-Data ... FWB-Data User creates and offers value-added services using FWB-Data which are then provided to third parties ... a separate fee liable licence with DBAG is required and any usage without a licence in place is strictly prohibited." | Deutsche Börse MDDA: Börse Frankfurt Pre-Trade distribution €1,115/month real-time; non-display from €360/month |
| `Yahoo Finance` | unlicensed-commercial | `collect yahoo_equity`, `seed legacy_yahoo` | https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html (last updated 4 Aug 2026): you may not "access or collect data ... from our Services using any automated means ... including but not limited to robots, spiders, scrapers ... for any purpose without our express, prior permission". Also: "Unless otherwise expressly stated, you may not access or reuse the Services, or any portion thereof, for any commercial purpose." https://query1.finance.yahoo.com/robots.txt: `User-agent: *` / `Disallow: /` | An exchange or vendor equity feed with redistribution rights (Nasdaq Basic, Cboe, a data vendor) |
| `ICE Data Indices, LLC via FRED` | unlicensed-commercial | `collect fred` (series `ice_bofa_us_{hy,bb,b,ccc,bbb,ig}_oas`) | https://fred.stlouisfed.org/legal/: series labelled "Copyrighted: Pre-Approval Required" "may only be used for non-commercial educational or personal use. For any other use, you must obtain permission from the copyright holder". ICE's series note: "Reproduction of this data in any form is prohibited except with the prior written permission of ICE Data Indices." Collected by the owner's decision (2026-10-21) as model inputs only: each series also carries `display_allowed` = false and the ICE notice, so it is never shown, whatever the switch says. | ICE Data Indices written permission, or a direct ICE index licence |
| `iShares USHY fund OAS (BlackRock)`, `iShares HYBB fund OAS (BlackRock)`, `iShares USIG fund OAS (BlackRock)` | unlicensed-commercial | `collect etf_oas` (series `etf_oas_{ushy,hybb,usig}`, proxies for `ice_bofa_us_{hy,bb,ig}_oas` while FRED is down) | The iShares pages' Terms & Conditions, https://www.blackrock.com/corporate/compliance/terms-and-conditions (checked 2026-10-21): content "is solely for your personal, non-commercial use". You may not "use the content of this Website for public or commercial purposes ... without BlackRock's written permission", or "Use any robot, spider, intelligent agent, other automatic device, or manual process to search, monitor or copy this Website or the reports, data, information ... without BlackRock's permission". robots.txt does not disallow product pages. Collected at the owner's request of 2026-10-21 ("find them elsewhere") as model inputs only, with `display_allowed` = false. | BlackRock written permission, or the ICE indices under an ICE licence |
| `SPP` | unlicensed-commercial | `collect spp_queue` (interconnection queue, `queue_project` facts) | https://www.spp.org/terms-conditions/: copying is allowed "EXCEPT when such materials will be used, in whole or in part, within a commercial publication ... Any commercial use of these materials requires prior, express written authorization". | Written authorization from SPP |
| `El Paso Electric` | unlicensed-commercial | `collect epe_queue` (interconnection queue PDF, `queue_project` facts) | https://www.epelectric.com/company/terms-of-use: "For commercial use of any portion of the contents you must secure the written consent from the appropriate content provider." | Written consent from EPE |
| `SEC EDGAR` | public | SEC collectors | "Information presented on sec.gov is considered public information and may be copied or further distributed ... without the SEC's permission." | n/a |
| `N-PORT: <fund> (<period>)`, `N-PORT median of N funds (<period>)`, `BDC 10-Q: <bdc> (<period>)` | public (matched by prefix) | N-PORT and BDC loaders and collectors | Same SEC clause. Cite marks "as filed by <fund>". | n/a |
| `U.S. Treasury` | public | `collect ust_par_curve` | U.S. Government work (17 U.S.C. §105) | n/a |
| `Federal Reserve Bank of New York` | public | `collect nyfed_sofr` | Licence "to use, copy, and distribute Content for your personal or business purposes"; the SOFR notice is required | n/a |

Statuses:
- **public:** a public record; commercial reuse is allowed, though attribution or a notice may be required.
- **licensed:** Marden holds a licence.
- **unlicensed-commercial:** the publisher's terms require a paid licence, or forbid commercial or automated use, and Marden has no licence.

A name not in the table, such as a press site, has no entry. `licence_for` returns `None` for it, and the switch leaves it alone.

## 3. The switch

`MARDEN_SHOW_UNLICENSED_PRICES` is read into `Settings.show_unlicensed_prices`.

- **`true` (the default):** values from unlicensed sources are served and used like any other value.
- **`false`:** every row whose `source_name` is `unlicensed-commercial` is left out.
- **Anything else:** a `ConfigError`.

Helpers in `marden.core.licensing`:

- `licence_for(source_name)` returns the entry. It tries the exact name first, then the per-filing prefixes.
- `is_unlicensed(source_name)` and `visible(source_name)` check one row against the current switch.
- `filter_rows(rows)` drops hidden rows from a list of `price_obs` or `fact` dicts.
- `unlicensed_source_names()` returns the exact names for SQL, e.g. `WHERE source_name <> ALL(%s)`.
- `hidden_source_names()` returns the names a reader leaves out under the current switch: `[]` while it is true, `unlicensed_source_names()` when false. `source_name <> ALL(%s)` with an empty list keeps every row.

**Wiring status (2026-10-22): complete.** Every place a value leaves the database reads the switch. With `false`, no value, source name or source link from an `unlicensed-commercial` source reaches a read model or an API response. With `true` (the default), nothing changes.

| Where | How the switch is applied | Test (both settings, real rows copied from `marden`) |
|---|---|---|
| Read-model fact reads: `marden.readmodels.context.known_facts` and `known_fact_rows` | `known_facts` passes `hidden=` to `marden.core.facts.current_facts` / `facts_asof`. Hidden rows are dropped before the `fact_current` / `fact_asof` precedence picks (`core.facts.PRECEDENCE`), so the field falls back to its next licensed value, or to n/d. `known_fact_rows` adds `source_name <> ALL(...)`. | `tests/readmodels/test_licence_switch.py` |
| Read-model builders' own SQL: `instrument_inputs.price_rows` (every price shown: Marden price history, last trade, exchange quote, fund marks), `sites_common.dealer_quote`, `sites_common.subjects_listing`, `facilities_common.serving_plants` / `served_campuses` (plant_serves evidence facts), `payers_common.edges_changed_since`, `sites_names` (instrument fact source links) | `source_name <> ALL(hidden_source_names())` | The same file rebuilds `instruments` and `instrument_detail` for Oracle 5.2% 2035 (`EX-ORCL-239`: TRACE prints, Frankfurt fixings, TRACE security-master facts), CoreWeave 9% 2031 and `EX-CRWV-103`. With `true`, the TRACE last trade and Frankfurt quote are served. With `false`, no document holds an unlicensed name or link, and the SEC EDGAR terms and N-PORT fund marks are unchanged. A code scan fails on any new builder read of `fact` / `price_obs` that does not apply the switch. `marden.readmodels.deps` is exempt: it only marks dirty keys. |
| API (`apps/api`): permit numbers in `GET /api/search` and the data-center / power-plant search box (`ctx.facts()` in `lib/row-query.ts`), and the staff review queue (`GET /api/review`, `/api/review/:id` and its decisions) | The API reads the same `MARDEN_SHOW_UNLICENSED_PRICES`, parsed the same way (`config.ts`: true/1/yes, false/0/no, blank = true, anything else fails at start-up), into `app.hiddenSources`. `lib/licensing.ts` `factSource()` applies the same precedence over the remaining rows. A review item about a hidden fact is not listed and answers 404. `UNLICENSED_SOURCE_NAMES` there must equal the Python list; `test_licence_switch.py` compares them. Everything else the API serves comes from `read_model` (built by the builders above) or from tables with no third-party values. | `apps/api/test/licensing.test.ts` |
| Models | `marden.models.pit` (facts, events), `marden.models.price_marks` and `track_record_cases.market_series` (price observations) drop the hidden names. Every other direct read in `marden/models` is listed in `tests/models/test_model_input_paths.py` with its reason; none reads a flagged source. Verified 2026-10-22. | `tests/models/test_model_pit.py` (price run with the switch off: no input from an unlicensed source), `tests/models/test_price.py` |

Events carry no unlicensed `source_name` (checked on `marden` 2026-10-22), so event reads are not filtered by the switch. `pit.event_rows` filters events anyway.

**Flipping the switch.** Read models and model outputs are precomputed, so a flip shows only after they are rebuilt. In order:

1. Set `MARDEN_SHOW_UNLICENSED_PRICES=false` on the worker and the API, and restart both.
2. Re-run the models for today. Run `python -m marden model price` first. Then run the models that read its output: `mark_gap`, `recovery`, `stress`, `risk_list` and `track_record`; `python -m marden model --help` lists them all. Stored outputs were computed with the switch as it was.
3. Rebuild the read models: `python -m marden rebuild all`.

Snapshots of past days (`read_model.as_of` before the flip) keep what was served then. A backfill made after the flip (`rebuild_all(view, as_of=day)`) uses the current switch.

## 4. How collection stays polite

The owner set these limits. `services/marden/collectors/market_http.py` enforces them for Yahoo Finance and Börse Frankfurt; `services/marden/collectors/finra_trace_client.py` enforces them for TRACE (see §7).

- **Honest identity.** The User-Agent is `Marden research cody@marden.ai`, the declared `SEC_USER_AGENT`, sent with an ordinary `Accept` header. There is no browser User-Agent, no fingerprint headers, no proxies and no rotating identities.
- **Low rate.** Each host is capped at 1 request/second, and each request waits a further random 0.2–0.8 s.
- **Once a day, after the close:**
  - Yahoo: weekdays 18:20 ET. The window opens at 18:00 ET.
  - Börse Frankfurt: weekdays 18:05 Frankfurt time; trading ends 17:30. The window opens at 17:45.
  - TRACE: weekdays 18:45 ET, after TRACE reporting closes at 18:30 ET. The window opens at 18:30 ET.
  - The collectors enforce this themselves, so a manual `collect` obeys it too. A run is skipped, with no request made, before its window opens or after a full ok run that evening. `--force` overrides; use it only when the owner asks. A run limited with `--ticker`, `--isin` or `--cusip` does not count as the evening's full run.
  - Frankfurt ISINs that are not listed are re-checked only weekly.
- **Never evade bot protection.** Each of these raises `Challenged`:
  - a Cloudflare `cf-mitigated` header;
  - a CAPTCHA or "Just a moment" page;
  - a redirect to a login or consent page;
  - HTTP 401/403;
  - a 429 that persists after one retry.

  The run is recorded as failed on the `source` row (`last_error`, `source_run.ok = false`) and stops. Nothing is retried with other headers. The next scheduled run tries once more.
- **Only what the page loads itself.** Collectors fetch only the page or the JSON that page requests, the same calls a browser makes.

## 5. Tags

- **Yahoo Finance equity series** (`series` / `equity:<TICKER>`: open, high, low, close, adj_close, volume, dividend, split) are tagged **F**: the exchange's official daily prices, as instructed for this build.
  - `collect yahoo_equity` and `seed legacy_yahoo` hold each series, field and date once between them. The collector does not repeat a value the archive already holds, and the archive does not add a date the collector holds. A value Yahoo later revised, such as a late volume print, is written by the collector as a new value.
- **Börse Frankfurt** specialist quotes and fixings are **R** (`price_obs.kind = 'exchange'`, plus fact `exchange_quote` with bid/ask on the instrument). They are usually zero-turnover quotes, not trades.
  - `seed legacy_frankfurt` writes an archive fixing on the instruments whose ISIN, CUSIP or `identifiers` carry its ISIN, the same universe the collector reads. The archive's own instrument id is not enough. Five archive ISINs are reported as `unmatched` for that reason: EX-CRWV-102 `USU2069EAB66`, EX-CRWV-103 `USU2069EAC40` and EX-CRWV-104 `USU2069EAE06` (the RegS lines), plus EX-NBIS-198 `US63954QAE61` and EX-NBIS-199 `US63954QAF37`. `trace/match.json` lists the CoreWeave lines and NBIS-198 as twins, but not NBIS-199's. They load once the `seed instruments` loader adds them to `identifiers`.
  - After the 17:30 close Frankfurt shows no bid or ask (both 0; seen 2026-10-21). The scheduled 18:05 run therefore records the last fixing, day range, previous close, fixings count and nominal turnover, but no bid/ask. Capturing bid/ask needs a run before 17:30, which is outside the "after 17:45 CET" limit, so that is the owner's call.
- **TRACE prints** are F.

## 6. Changing the decision

- **To stop showing:** set `MARDEN_SHOW_UNLICENSED_PRICES=false`, re-run the models, then rebuild (§3, "Flipping the switch").
- **To stop collecting:** set the source `blocked` with a `blocked_reason` in its `declare_source`. Heartbeats then record every scheduled run as blocked.
- **To delete:** remove rows by `source_name`. Append-only history means this needs an explicit, audited deletion; the writers never do it.
- **After buying a licence:**
  - Change the entry's status to `licensed` and its note to the agreement.
  - Update the source's `terms_note`.
  - If the licensed feed has a different name, give it its own `source_name`, so pre-licence rows stay identifiable.

## 7. TRACE (`FINRA TRACE (finra.org)`)

**What the collector asks for.** `collect finra_trace` makes the requests the finra.org bond page makes, read from the page's app bundle (`ddwa-cdn-us-east-1.ddwa.finra.org/app-dynamic-reporting/app-dynamic-reporting.js`):
1. the page itself;
2. the bond template, then the security master and last sale for the CUSIP;
3. the price/yield template, then the end-of-day history. The page asks for five years. Marden asks for five years the first time the collector reads a CUSIP, then the last 14 days. Rows from `seed legacy_trace` do not count as that first read, because the archive only starts in 2026.

The watched universe is every CUSIP an instrument carries: `instrument.cusip`, the CUSIP inside a US/CA ISIN, and the `identifiers` facts.

**What it writes.**
- Each row is `price_obs` kind `trade`, tag F, with `source_url` = `https://www.finra.org/finra-data/fixed-income/bond?cusip=<CUSIP>&bondType=CORP`.
- One end-of-day row per instrument, CUSIP and trade date (last sale price and yield, `obs_at` NULL). Today's row is written only after 18:30 ET.
- The last print, with its time as `obs_at` in UTC.
- Security-master facts the instrument lacks: `coupon_rate_pct`, `maturity`, `is_144a`, `issuer_name`.
- The bond page shows no high/low, volume or trade count, so none is stored.

**Status on 2026-10-21: refused.** The bond page answers 200. That page carries Cloudflare's bot script (`/cdn-cgi/challenge-platform/...`). Every backend request the page makes answers Marden's client with an HTML 404 "Not Found" page, not JSON. The backend is `services-dynarep.ddwa.finra.org` and the requests include the bond template. The collector treats this as a refusal:
- it stores the answer as a `refusal` document;
- it records `AccessRefused` on the source (`last_error`, `source_run.ok = false`);
- it stops after 2 requests and writes nothing.

Nothing is retried with other headers, a browser User-Agent or a headless browser. The scheduled run tries once a day. Until finra.org serves the backend to an honestly identified client, live TRACE needs the licensed route in §2.

**The archive (`seed legacy_trace`).** The loader reads:
- `trace/eod.json`, `trace_rows.json` and `spreads.json`;
- the security-master searches (`secs2.json`, `secs.json`, `sec_search.json`);
- the "TRACE last sale" rows in `aiwc/pxsrc/case_px.json`.

Each print becomes one observation per CUSIP and trade date. The unrounded `eod.json` price wins; rounded copies match it within half their last digit. Loading rules:
- Only CUSIPs the identifiers name are loaded. The run report lists the others, with the archive's own `match.json` guess.
- `aidebt.json` px values that copy a TRACE row resolve to that same observation and add no rows.
- `spread_bp` on these rows is the legacy G-spread from `spreads.json`: the TRACE yield minus the interpolated UST par yield. It is a legacy Marden derivation carried on the F price row.
- The archive's last refresh ran at 10:55 ET on 2026-10-20 (`refresh_state.json`). Its 107 prints dated that day are intraday last sales, not end-of-day prices. They are written with `obs_at` = 2026-10-20 14:55:29 UTC, the refresh time; the print itself is at or before that. End-of-day rows keep `obs_at` NULL, so the collector's end-of-day row for 2026-10-20 never collides with them.

**Known inconsistencies left for the owners of other files:**
- `docs/methods/price-sources.md` is the research record from before the decision. It says these sources are blocked; a note at its top points here.
- The `instruments` loader's `legacy_price_status` facts still say `trace_copy_blocked`.

## 8. Loading the main database

Run these after `seed all` has finished on the main database. The legacy loaders need the instruments and their `identifiers` facts that `seed instruments` writes; without them nothing maps.

```
cd services
export MARDEN_SEED_DIR=<extracted marden_data_2026-10-20>
uv run python -m marden seed legacy_trace        # ~5 min on a fresh database; FINRA TRACE (finra.org) rows
uv run python -m marden seed legacy_frankfurt    # Börse Frankfurt fixings from the bondq snapshots
uv run python -m marden seed legacy_yahoo        # Yahoo Finance bars from aiwc/research/dl/y_*.json
uv run python -m marden collect finra_trace      # after 18:30 ET; skipped earlier unless --force
uv run python -m marden collect boerse_frankfurt # after 17:45 Frankfurt time; skipped earlier unless --force
uv run python -m marden collect yahoo_equity     # after 18:00 ET; skipped earlier unless --force
```

In short: after `seed all`, run `seed legacy_trace`, `legacy_frankfurt`, `legacy_yahoo`; then `collect finra_trace`, `boerse_frankfurt`, `yahoo_equity`.

- **Order.** Load the archive first, then collect. Either order leaves one row per value, but loading the archive first leaves the collector's newer values (Yahoo's revised volumes, today's quotes) as the latest.
- **Once.** Run each collector once, in its window after the close, as the daily schedule does. Do not run them several times to test. A second run the same evening writes nothing new, but it is still more requests to the publisher.
- **Repeat runs.** Every step is idempotent. A second run of any loader or collector writes 0 new rows.
- **One writer at a time.** Run the loaders while no other loader or collector is writing. Concurrent writers can deadlock on `read_model_dirty`; this happened on 2026-10-21 with `collect sec_nport`. `legacy_trace` now rolls back and retries a deadlocked bond up to 3 times; the other loaders fail and can simply be rerun.
- **Expected on the scratch database `marden_prices` (2026-10-21, 707 instruments):**
  - `legacy_trace`: about 24,709 TRACE rows on 203 instruments (203 of 294 watched CUSIPs), 404 security-master facts, 0 rows from `aidebt.json` copies. The 36 `eod.json` CUSIPs that no `identifiers` fact names (mostly RegS twins) are listed under `unmapped` with the archive's `match.json` guess. The other 233 `unmapped` CUSIPs appear only in the security-master searches.
  - `legacy_frankfurt`: 474 fixings on 158 instruments, plus the 5 unmatched ISINs in §5.
  - `legacy_yahoo`: 792 facts for CRWV, NBIS and ORCL, 2026-08-17 to 10-16.
  - `collect finra_trace`: refused today (§7). It records the refusal and writes nothing.
  - `collect boerse_frankfurt`: about 162 of 306 watched ISINs are listed, one fixing each per day.
  - `collect yahoo_equity`: 26 tickers, a 2-year backfill on the first run.
- **Main database check (2026-10-22).** `marden` already holds exactly what the fixed loaders write, so nothing was cleaned.
  - TRACE: 24,709 rows. Replaying the archive through `legacy_trace`'s reader and the watched universe gives the same set: 0 missing, 0 extra, 0 duplicate. For 2026-10-20 there are 108 rows (107 CUSIPs; `629377CP5` maps to two instruments), all with `obs_at` = 14:55:29 UTC, and no end-of-day rows.
  - Yahoo: 82,788 facts (792 from `seed legacy_yahoo`, 81,996 from the collector). No series, field, date and value is held by both writers. The 3 dates both hold are the 2026-10-16 volume revisions for CRWV, NBIS and ORCL, which §5 keeps.
- **Scratch database.** `marden_prices` held these sources while another workflow loaded `marden`. The scratch rows are not copied over: the main database is loaded by these same commands.
