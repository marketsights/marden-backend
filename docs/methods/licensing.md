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

**Wiring status (2026-10-21):** the flag and the switch exist, and every row from these sources carries the exact name. The API, read-model builders and models do not yet read the switch, so today it changes nothing they serve.

To make `false` take effect:
- Filter `price_obs` and `fact` reads in `marden.readmodels.known_facts`, the price-history and curve builders, and the pricing model with `unlicensed_source_names()`.
- Rebuild read models after flipping it: `python -m marden rebuild all`. Read models are precomputed, so a flip shows only after a rebuild.

## 4. How collection stays polite

The owner set these limits. `services/marden/collectors/market_http.py` enforces them for Yahoo Finance and Börse Frankfurt; `services/marden/collectors/finra_trace_client.py` enforces them for TRACE (see §7).

- **Honest identity.** The User-Agent is `Marden research cody@marden.ai`, the declared `SEC_USER_AGENT`, sent with an ordinary `Accept` header. There is no browser User-Agent, no fingerprint headers, no proxies and no rotating identities.
- **Low rate.** Each host is capped at 1 request/second, and each request waits a further random 0.2–0.8 s.
- **Once a day, after the close:**
  - Yahoo: weekdays 18:20 ET.
  - Börse Frankfurt: weekdays 18:05 Frankfurt time; trading ends 17:30.
  - TRACE: weekdays 18:45 ET, after TRACE reporting closes at 18:30 ET. A second run after an ok run that evening is skipped unless forced.
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
- **Börse Frankfurt** specialist quotes and fixings are **R** (`price_obs.kind = 'exchange'`, plus fact `exchange_quote` with bid/ask on the instrument). They are usually zero-turnover quotes, not trades.
  - After the 17:30 close Frankfurt shows no bid or ask (both 0; seen 2026-10-21). The scheduled 18:05 run therefore records the last fixing, day range, previous close, fixings count and nominal turnover, but no bid/ask. Capturing bid/ask needs a run before 17:30, which is outside the "after 17:45 CET" limit, so that is the owner's call.
- **TRACE prints** are F.

## 6. Changing the decision

- **To stop showing:** set `MARDEN_SHOW_UNLICENSED_PRICES=false` and rebuild, once the wiring in §3 is done.
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
3. the price/yield template, then the end-of-day history. The page asks for five years; Marden asks for five years once per CUSIP, then the last 14 days.

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

**Known inconsistencies left for the owners of other files:**
- `docs/methods/price-sources.md` still says these sources are blocked.
- The `instruments` loader's `legacy_price_status` facts still say `trace_copy_blocked`.
