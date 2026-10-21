# Data map: aiwc cases / research area

Root: `data/aiwc/` (from marden_data_2026-10-20.zip). All files are JSON. Profiled 2026-10-21 with read-only scripts in `datamap/scripts/` (`shape.py`, `union.py`). Not covered here: `aiwc/pxsrc/` and `aiwc/q3x/`, which belong to other areas. The root files `data/aidebt.json`, `data/power.json` and `data/collmeta.json` are also another area's. They appear below only where files in this area join to them.

## 0. The spine: the 29 "cases"

Every per-case directory is keyed by the same uppercase **case id**, also called the deal or site id:

`ABILENE, APLD_PF1, APLD_PF1B4, APLD_PF2, BEIGNET, CIFR_BARBER, CIFR_BLACKPEARL, CIFR_STINGRAY, CLOUDHQ, CORZ_CW, CRWV_DDTL3, CRWV_DDTL4, DATABANK, EDGE_CEDAR, FLASH_ABERNATHY, GLXY_H1, GLXY_H2, HUT_BP, HUT_RB, IREN_CHILDRESS, JUPITER, QTS, SALINE, SOPAIPILLA, SOUTHGATE, VANTAGE_TX, VANTAGE_WI, WULF_LM, XAI_VCI`

A case is one financed project: a site or campus, or a GPU fleet, plus the debt behind it. It is the natural `site.slug` (lowercased) and the parent of its instruments.

| dir | files | coverage of the 29 ids |
|---|---|---|
| cases/ | 29 | all |
| memo/ | 29 + 6 aux | all |
| params/ | 28 | missing EDGE_CEDAR |
| out/ | 28 + all.json + live.json | missing EDGE_CEDAR; all.json has 26 (no DATABANK, EDGE_CEDAR, SOUTHGATE) |
| gpu/site_power.json, gpu/gpu_out.json:pwp | 26 | no DATABANK, EDGE_CEDAR, SOUTHGATE |
| case_seeds.json | 29 | all |

Scope flags: SOUTHGATE is in Australia (state "VIC/TAS"), which is outside the US-only V1. CLOUDHQ, DATABANK and QTS are data-center ABS or master-trust deals, which are close to the "stabilized colocation / CMBS" exclusion.

### Join keys, verified

| key | where | joins to | check |
|---|---|---|---|
| case id | file names; `id` fields; `case` field in events; `"<CASE>\|..."` prefix on keys in state/*.json and live.pages; `cases[]` in feedx | all per-case files | consistent |
| aidebt row id: `EX-<GRP>-NNN`, `NP-<ISSUER>-<CUSIP/LoanX>`, `FWP-…`, `MAN-…`, `BDC-…`, `XB-…`, `8K-…` | memo `instruments[].debt_row` (51 unique), case_seeds `debt_rows[].id` (53), memo/watch_plain keys (25), out-of-area aidebt.json | `data/aidebt.json` `rows[][0]` (707 rows) | 100% resolve |
| grp code (e.g. `CRWV`, `ORCL`, `APLD`, `VANTAGE_ORCL`) | memo `chain[].grp` (108 of 288 non-null) | aidebt `grp` column (79 groups), aidebt `gn` map (grp → [display name, ticker]) | 100% resolve |
| power project id `N-…` (53) / `P-…` (129) | case_seeds `power_ids`; feedx/proj_x `id` | `data/power.json` `projects[].id` (182) | 100% resolve; proj_x has the same 182 ids in the same order |
| collmeta key `"<GRP>\|<sub-site label>"` | case_seeds `collmeta_keys` | `data/collmeta.json` top-level keys | 100% resolve |
| aiw_proj index | case_seeds `aiw_idx` (ints 0–78) | position in aiwc/aiw_proj.json (80 entries, no id field) | GLXY_H1 and GLXY_H2 both carry index 0 |
| aiw_proj `"<issuer>\|<project>"` | aidebt `aiwx` keys (80) | aiw_proj `issuer`+`project` | 80 = 80 |
| SEC CIK | research/dl, dl_b, gpu/operators `cik` | EDGAR URLs in every file | CoreWeave 1769628, IREN 1878848, Hut 8 1964789, APLD 1144879, CIFR 1819989, WULF 1083301, GLXY 1859392, CORZ 1839341, NBIS 1513845 |
| ticker | memo `chain[].ticker` (54 of 288 non-null), operators `ticker`, aidebt `gn` | party table | partial |
| FERC accession `YYYYMMDD-NNNN` | state/ferc.json keys, pfind keys, feed_x/docket_x links, events `u` | elibrary URLs | – |

---

## 1. aiwc/cases/*.json — research dossiers, 29 files, 9–23 KB each

**Entity:** one financed project (site, campus or GPU fleet) with its debt, lease, collateral, parties, risks and a list of pages to monitor. `asof` = "2026-10-17" on every file.

Top level (union): `id, name, asof, debt[], collateral{}, site{}, capacity{}, lease[], credit_support[], parties{}, cost{}, risks{}, watch[], note`, plus optional `covenants{}` (3 files), `facility{}` (1), `metrics{}` (1, QTS KBRA DSCR).

| block | count | fields (n non-null / n) and samples |
|---|---|---|
| `debt[]` | 46 rows | `inst` ("9.250% Senior Secured Notes due 2030"), `issuer`, `amt_m` (USD mm; 37/46, e.g. 18000), `coupon_pct` (26/46 float; 7 rows use a string `coupon` instead, e.g. "SOFR + 4.00% …"), `maturity` (38/46, ISO date "2030-12-15"), `amort` (text), `rank` (text), `price` (33/46, e.g. 106.29), `price_date` (26/46), `price_src` (URL or `local:` ref), `src[]` URLs, `quote` (verbatim excerpt), `note`; optional `issue_price`, `rating` (16), `drawn_m`, `amt_asof`, `commitment_m`, `holders` (text), `covenants` (text) |
| `collateral` | 29 | `desc`, `excludes_gpus` (bool 24/29), `reserves` (text 18/29, e.g. "Debt Service Reserve Required Amount $177.0m"), `src[]`, `quote`; optional `inferred_gpus` |
| `site` | 29 | `location` (text), `county` (27/29, "Dickey"), `state` (28/29, "ND"; multi-state values such as "TX, GA, NC, OK" and "VA/MN" occur), `land` (text), `src[]` |
| `capacity` | 29 | `critical_it_mw` (19/29), `gross_mw` (16/29), `delivered_mw` (23/29), `pct_complete` (9/29), `scheduled_completion` (free text: "2026-Q4 (…)", "2026-10", "2028 (…)"), `original_completion` (free text), `src[]`, `quote`, `note`; optional `per_building{}`, `per_site_mw{}` |
| `lease[]` | 40 | `tenant` (text, "Oracle (end user OpenAI)"), `mw` (30/40), `term_yrs` (26/40; int/float/str), `start` (26/40; "2025-10", "2025 (phased…)"), `tcv_m` (14/40), `annual_rent_m` (**3/40**), `rent_basis`, `termination_rights`, `outside_date` (**3/40**, text only, e.g. "Delayed Data Center Date = [*] …(redacted)"), `renewals`, `src[]`, `quote`; optional `inferred` |
| `credit_support[]` | 42 | `type` (completion guarantee 14, parent guarantee 5+, backstop 3, RVG 2, LC …), `provider`, `amount_m` (12/42), `covers`, `src[]`, `quote` |
| `parties` | 29 | `tenant_rating` (text, "Baa2 (neg) / BBB- (S&P, cut 2026-07-09)…"), `tenant_bond` (text with price and yield, 19/29), `sponsor`, `sponsor_rating`, `guarantor`, `src[]`; optional `lenders`, `fund_holders`, `sponsor_liquidity`, `sponsor_bonds`, `deal_rating` |
| `cost` | 29 | `total_project_cost_m` (10/29), `cost_to_complete_m` (6/29), `overrun` (text), `src[]`, `note`; optional `inferred` |
| `risks` | 29 × 7 | keys `power, permits, water, construction, tenant, sponsor, other`; each holds `status` (short text, "on-site gen under construction; fuel supply unresolved"), `detail`, `evidence[]` = `{d, w, q, u}` (date, headline, verbatim quote, URL). 235 evidence items in total, dated 2023-12 to 2026-10-17. Some dates are month-only and 1 is null |
| `watch[]` | 186 | `kind` (news_query 49, state_puc 33, county_board 33, state_env_permit 21, city_council 14, iso_queue 10, ferc_docket 9, court 7, sec_filings 7, zoning 3), `agency`, `id` (docket or permit no., 71/186, e.g. "PU-26-293", "182126 / PSDTX1688"), `url` (146/186), `query`, `why` |

**Sources and tags:** there is no F/R/M tag field. Provenance comes from URLs (434 sec.gov, 39 DCD, 34 kbra, 15 interchange.puc.texas.gov, 12 elibrary.ferc.gov, 12 tdlr, 9 tceq, …) and from `local:` pointers (`local:aidebt.json row EX-…` 47, `local:aiwc/case_seeds.json` 13, `local:watch/out/db/pulse.json` 9, `local:iren/txt/*.txt`, `local:glxy/holders.md`, `local:collmeta.json`). Those local files are not in this area, and the `iren/`, `glxy/` and `crwv/txt` paths may not exist in the zip. Words such as "Reported:", "inferred", "unverified" and "per local seed" inside strings mark R or M status. An `inferred: true` flag appears on cost, lease, site, capacity and credit_support in a few files. CUSIPs appear only inside text (07367AAA7, 36322DAA4, 44813DAA4).

**Quality:** values are rich but mostly free text. Units are USD mm (`*_m`) and MW. Date formats are mixed (ISO, "2026-Q4", "2026-mid", "2025-11"). `term_yrs` has mixed types. The coupon is either `coupon_pct` or a string `coupon`. Site location is text with no lat/lon. Multi-site cases (CORZ_CW, QTS, DATABANK, CLOUDHQ, CRWV_DDTL4 with site null) do not map 1:1 to a single site row.

**Seeds:** site (location, county, state, MW, stage), contract/lease, instrument (terms, coupon, maturity, amort, rank, collateral, reserves), party (sponsor, tenant, guarantor, ratings as text), obligation edges (lease and credit_support), event and evidence (`risks.*.evidence`), document (all URLs and quotes), fact (each field with its quote). It also seeds a watch-source registry (`watch[]`), which has no table in the spec. types.ts targets: `SiteDetail.facts`, `debtExposed`, `ProjectFile.checklist / structure / construction`, `Evidence`, `LeaseRecord`, `ObligationEdge.conditions`, `InstrumentDetail.terms`.

---

## 2. aiwc/memo/ — publication-ready, plain-English layer

### memo/<CASE>.json (29 files, 12–25 KB)
**Entity:** an editorial rewrite of a case for display. It covers the chain of parties, the instruments, security terms, unknowns and scored issues. File mtimes run up to 2026-10-19.

| key | n | fields and samples |
|---|---|---|
| `id`, `headline`, `status` | 29 | headline: "Two buildings with 250 MW of critical IT load at Applied Digital's Polaris Forge 1…"; status: prose |
| `chain[]` | 288 | `role` (Borrower 35, Utility 32, Tenant 30, Sponsor 29, Lender 29, Site owner 22, Project company 17, Grid operator 14, Guarantor 12, Equity investor 10, Customer 10, Arranger 10, End user 9, Power plant 8, Contractor 7, Developer 6, Backstop 4, Fuel supply 3, Operator 1), `name` ("CoreWeave, Inc."), `what`, `ticker` (54), `grp` (108, joins aidebt grp), `basis` (disclosed 197 / reported 76 / presumed 15) |
| `instruments[]` | 78 | `name`, `borrower`, `amt_m` (55/78), `terms` (prose: coupon, maturity, amortization), `ranking`, `price` (44), `price_date` (33), `price_kind` (fund mark 39, exchange last 3, dealer quote 2, issue price 1, null 33), `price_note`, `price_src` (N-PORT `primary_doc.xml` URLs for fund marks), `holders[]` (strings such as "iShares Broad USD High Yield Corporate Bond ETF ($50.4m principal, fund filing 31 Jul 2026)"), `third_party` (bool), `modeled` (bool; the instrument run through params/out; 1 per case in 22 cases, 2 in 4, 0 in 3), `note`, `debt_row` (aidebt id, 52/78) |
| `cap[]` | 78 | short-form capital stack parallel to instruments: `tranche, issuer, rank, coupon` ("9.25" or "n/d"), `maturity` ("Dec 2030"), `amt_m, price, px_date, px_src` ("Fund mark"/"Dealer quote"), `holders` ("iShares Broad USD HY ETF +3 funds"), `modeled, note` |
| `security[]` | 217 | `k` (Collateral 29, Title to the site 29, Lease term 29, Termination rights 27, Guarantee 24, Equity cushion 22, Reserves 21, Completion support 16, Backstop 7, …), `v` (prose), `s` (short form), `basis` (disclosed 150 / reported 29 / not disclosed 24 / presumed 14), `src` (URL, 200/217), `q` (verbatim quote, 32) |
| `unknowns[]` / `unknowns_s[]` | 186 each | long and short phrasing of missing facts ("Rent, and whether it covers debt service on all eight buildings") |
| `issues[]` | 160 | `driver` (power 29, tenant 26, other 25, construction 25, water 20, sponsor 18, permits 17), `cat` (display category), `level` (0:7, 1:112, 2:37, 3:4), `title`, `what`, `why`, `impact`, `next{date (23/160), event}`, `next_short`, `clears_if`, `clears`, `src[]` (346 URLs); optional `level_suggest`, `level_note` |

**Tags:** `basis` is the closest thing to a tag. Suggested mapping: disclosed → F, reported → R, presumed/inferred → M. "not disclosed" → value null (n/d).
**Quality:** `issues[].level` agrees with `params.drivers.*.level` (max per driver) in 107 of 112 case-driver pairs. The 5 mismatches are in DATABANK and SOUTHGATE. Holder data is unstructured text (par in $m inside the string) with no CUSIP or numeric mark per holder. Memo prices are older than params prices; see §3.
**Seeds:** party + role edges (`chain`), the instrument list (`instruments`/`cap`), facts with basis (`security`), events (`issues` with `next.date`), and the checklist n/d items (`unknowns`). types.ts: `ChainLink` (role/party/reason/fact), `SiteSummary.endUser/tenant/developer/leadLenders`, `ProjectParty`, `Tranche`, `ProjectStructure.*` (security keys map onto security / completionGuarantee / reserves …), `ChecklistItem` (state 'n/d' from unknowns), `RiskRow.why`, `SiteDetail.materialEvents` (severity from `level`), `CompletionRow.driver` (from the top issue driver).

### memo auxiliary files
| file | shape | entity / use |
|---|---|---|
| `titles.json` (2 KB) | `{CASE: [display name, place]}` 29 | site name and place label, e.g. `"APLD_PF1": ["Polaris Forge 1, Buildings 2 and 3", "Ellendale, North Dakota"]` → `SiteSummary.name` |
| `shortnames.json` (1.5 KB) | `{CASE: [sponsor short, payer short]}` 29 | e.g. `["Applied Digital","CoreWeave"]` → `developer` / `tenant` labels |
| `check_1/2/3.json` (24–32 KB) | list of `{id, field, problem, fix, severity, evidence}`: 40 / 48 / 34 rows covering 10 / 10 / 8 case ids | QA findings on memo fields (`field` is a JSON path such as "issues[1].what"; severity error / unsupported / clarity). Audit trail only; do not seed as facts |
| `watch_plain.json` (28 KB) | `{aidebt_id: {project, borrower, parties, debt, security, lenders, why, confirm, next, headline}}` 25 keys | plain-English rewrite of `aidebt.json.watch` (the same 25 instrument ids, ranked). Feeds `RiskList.rows[].why`, `note`, `WatchItem.nextCatalyst` |

### memo.bak_secfill/ and memo_prefix/ (do not seed)
- `memo.bak_secfill/` (35 files) is the memo set **before an "SEC fill" pass**. 23 of 35 files are identical to memo/. The rest differ almost only in `security[]`: memo/ adds `security[].q` (SEC quotes, 21 entries), rewrites `v/s/src/basis` on about 28 entries, adds rows (e.g. 7 → 14 for one case), and touches 1 instrument price.
- `memo_prefix/` (34 files; no titles/shortnames; extra `CRWV_DDTL3.pre_sev.json`) is an **older generation**. It lacks the short-form fields (`cap`, `unknowns_s`, `issues[].cat/impact/clears/next_short`, `security[].s`) and has different issue and chain wording. Only 2 files are identical.

---

## 3. aiwc/params/*.json — pricing-model inputs, 28 files, 1–2 KB

**Entity:** a Monte Carlo credit-model input set for the one modeled instrument of a case. It is all Marden-modeled (M) except prices.

Top level: `id, debt{}, complete{}, lease{}, tenant{}, sponsor{}, market{}, drivers{}, notes[], assumed[]`, plus optional `rvg{}` (3), `backstop{}` (6), `gpu{}` (5), `pool{}` (3), `kv{}` (4), `support_text` (5), `collateral_type` (8: "pool" / "gpu").

- `debt`: `inst, amt_m` (e.g. 9400, which merges Abilene's two loans), `coupon_pct, maturity_m` (months), `amort` (bullet/linear), `amort_start_m, balloon_frac, call_m, call_px, rf_tenor` ("5y"), `floating`, `price` (26/28), `price_date` (2026-10-12 to 10-16), `price_kind` ("TRACE last sale", "dealer quote", "undated mark", …).
- `complete`: `done_frac, months_to_complete` (22), `outside_months` (12/22 non-null), `cost_to_complete_m, draw` ("progress"), `cash_frac_unbuilt, term_right`.
- `lease`: `rent_m` (annual USD mm, often an assumption), `term_yrs`, `months_left, renew_every_yrs, p_exit`.
- `tenant` / `sponsor` / `rvg` / `backstop`: `name, rating` ("BBB"), `spread_bp`, `type` (ig / neocloud / miner / hyperscaler / pool), `p_valid, cap_m, same_as, operator, p_disrupt`.
- `market`: `rent_m, cap_rate, replacement_m`. `drivers`: `{power, permits, water, construction}.level` 0–3. `gpu`: `value0_m, decay, vol, lien_share`.
- `assumed[]` lists which inputs are assumptions, e.g. ["rent","percent complete","cost to complete","loan coupon and term","how far Oracle's support extends"].

**Prices:** params carry the **newest prices** (mostly "TRACE last sale" on 2026-10-15/16), while cases and memo carry older marks. Examples: JUPITER 85.0 dealer quote 10-12 vs 90.0 (09-18) in cases/memo; SALINE 86.5 TRACE vs 98.75 issue price; APLD_PF1 102.875 TRACE vs 106.29 N-PORT mark (07-31).
**Seeds:** the price table (latest quote with date and kind, tag R or F), the price-input record (`Price.inputs`, method version), stress-scenario parameters, and `CushionProjection` inputs (`months_to_complete`, `outside_months`; these are model assumptions, tag M).

---

## 4. aiwc/out/ — pricing-model outputs

### out/<CASE>.json (28 files, 1.3–3 KB)
**Entity:** model result for the modeled instrument (20,000 paths; `hist` sums to 20000).
Fields: `p_full, p_loss, rec_given_loss, loss_rec_p10/p50/p90, exp_loss_pct, cause{abandon, late_termination, tenant_default, non_renewal, refinance}, cause_rec{…}, p_unfinished_at_maturity, p_bust, p_loss_if_bust, p_loss_no_bust, p_tenant_default, p_late, p_abandon, hist[10], wal_yrs, yield_pct (26), spread_bp (26), el_bp (26), spread_after_el_bp (26), bench` ("SOFR" / "5y Treasury"), `px_be, px_200, h_tenant, h_sponsor, sens[], price`.
`sens[]` (177 rows; k = tenant_x2, rent_m30, delay_p6, drivers_up, resolve_power/permits/water/construction, support_all, no_support): `label, p_loss, d_p_loss, el_bp, d_el_bp, exp_loss_pct, d_exp_loss_pct`.
JUPITER sample: p_loss 0.2176, rec_given_loss 0.6766, yield 11.54%, spread 742 bp over SOFR, EL 377 bp, px_be 96.0, px_200 89.8, price 85.0.
The data does not document `px_be` or `px_200`; the names suggest a break-even price and a price at +200 bp. No `value` field appears in the per-case files.

### out/all.json (31 KB)
`{CASE: {...}}` for 26 cases. It is an **earlier run** (mtime 2026-10-17 13:36, before the per-case files at 16:20–19:08). It adds `value` and `value_no_default` (e.g. ABILENE 101.99 / 103.89), `rec_p10/50/90`, `rf_used`, `gap`. It disagrees with the per-case files (JUPITER p_loss 0.045 vs 0.2176; price 90 vs 85). Treat it as stale.

### out/live.json (147 KB, asof 2026-10-20T17:23Z)
`{asof, bumps{} (empty), events{CASE: [event]}, pages{"CASE|url": {ok, n, err}}, errors[]}`.
- 246 events (`case, src, kind, d, t, u, seen, sev, drv, cls`), all dated 2026-09-26 to 2026-10-20. Each case is capped at 25. These are the latest pulled items; see state/events.json.
- 123 page checks (110 ok; 45 have an `err`, e.g. "Connection reset by peer").

**Seeds:** price (model price/spread/recovery with `methodVersion`, tag M; the method version is not in the data), `InstrumentDetail.recovery` (`rec_given_loss`, loss_rec quantiles), `InstrumentDetail.stress` (from `sens`, but not as priceAfter/holderNavHit), `RiskRow.odds` (`p_loss`), `CushionProjection.oddsOfMissing` (`p_late`), plus pipeline status (`live.pages` → `StreamEvent 'pipeline'`).

---

## 5. aiwc/state/ — monitor state, written 2026-10-20 17:23–17:25

| file | size | shape | entity |
|---|---|---|---|
| `events.json` | 296 KB | list[601] `{case, src, kind, d, t, u, named?, by?, seen, sev, drv, cls}` | **event log** for monitored sources. kind: news 425, county_board 98, court 42, city_council 16, ferc_filing 9, ferc 8, state_env_permit 1, zoning 1, ferc_issuance 1. sev: 0:532, 2:23, 3:28, −1:18. drv: other 480, permits 52, construction 25, power 24, sponsor 12, tenant 7, water 1. cls (rule label): null 505, "Zoning or site plan" 47, "Force majeure" 17, "Financing or rating" 12, "Pipeline or fuel supply" 11, "Court case" 3, "Tenant pull-out" 2, "Large-load tariff or utility contract" 2, "Moratorium" 1, "Air permit contested" 1. `d` 2026-09-26 to 2026-10-20; `seen` 2026-10-17 to 10-20. 23 of 29 cases have events (SALINE 100, APLD_PF1 63 …) |
| `news.json` | 287 KB | `{"CASE\|google-news-url": seen_ts}` 845 keys | dedupe store (seen URLs), no content |
| `ferc.json` | 2 KB | `{"CASE\|accession": seen_ts}` 32 | dedupe store for FERC accessions (CLOUDHQ, JUPITER, VANTAGE_WI, SOPAIPILLA, BEIGNET) |
| `pfind.json` | 2 KB | `{"CASE\|url\|title-prefix": seen_ts}` 10 | dedupe store for page-diff finds (FERC filings for JUPITER, VANTAGE_WI) |
| `pages.json` | 1.2 MB | `{"CASE\|url": {links[[href,text]], ok, checked, n, err?}}` 123 | link snapshots of the watched pages (from cases `watch[].url`), used for change detection |

**Quality, events.json:**
- 14 duplicate (case, u) pairs. 81 URLs repeat across cases, because one article is attached to several cases (e.g. GLXY_H1 and GLXY_H2 get identical items).
- All 42 "court" items (25 of them also in out/live.json) carry `src` "U.S. District Court, District of New Jersey", but every URL is a datacenterdynamics.com news article ("Amazon commits $10bn to data center campus in Montgomery City…"), so source and kind are **mislabeled**.
- Most news items (sev 0, drv other) are generic sector news. Google News redirect URLs are not canonical source URLs.
- Use `sev ≥ 2` with `cls` set as the candidate material events.

**Seeds:** event table (date, source, type, case link, severity, rule label), `Alert` (headline=t, ruleLabel=cls, refUrl=u, at=seen; tag R for news, F for ferc/county/court, which must be derived), `SiteDetail.materialEvents` (severity from sev: 3 red, 2 amber; suggested), `SiteDetail.tabCounts.events`, and `DcRecord`/`DcHistoryEvent` for county_board/zoning items. The dedupe stores and pages.json seed ingestion state only.

---

## 6. aiwc/research/dl/ — raw SEC and market downloads, captured 2026-10-17

| file | shape | entity | notes |
|---|---|---|---|
| `facts.json` (501 KB) | EDGAR companyfacts: `{cik:"0001769628", entityName:"CoreWeave, Inc.", facts:{us-gaap(305 concepts), dei(1), ecd(7), ffd(5)}}`; each concept → `units.USD[] {start?, end, val, accn, fy, fp, form, filed, frame?}` | CoreWeave XBRL financials, 2,705 datapoints, period end 2022-12-31 to 2026-09-16, filed 2025-05-15 to 2026-09-17 (10-Q 2102, 10-K 590) | e.g. DebtInstrumentCarryingAmount 35,551,000,000 @2026-06-30; PP&E net 46.736bn; RPO 103.7bn; OperatingLeaseLiability 16.319bn; YTD OCF 3.663bn; YTD capex 14.117bn. No ConstructionInProgressGross concept. `accn` joins to filing URLs (tag F) |
| `sub.json` (157 KB) | EDGAR submissions: company profile + `filings.recent{accessionNumber[], filingDate[], reportDate[], form[], items[], primaryDocument[], …}` (1001 filings, 2025-08-28 to 2026-10-14) + `files[]` (1 older page) | CoreWeave filing index (144: 484, Form 4: 440, 8-K: 26, 10-Q: 3) | **byte-identical** to dl_b/sub_0001769628.json |
| `y_CRWV.json`, `y_NBIS.json`, `y_ORCL.json` (6 KB each) | Yahoo chart API: `chart.result[0]{meta, timestamp[44], indicators{quote[{open,high,low,close,volume}], adjclose}}` | daily equity OHLCV, 2026-08-17 to 2026-10-16 (2mo, 1d), no nulls; last close CRWV 61.39, NBIS 165.47, ORCL 128.23 | unix timestamps; feed source is not a filing (tag R) |

**Seeds:** fact (XBRL facts, tag F, sourceUrl from accn), document (filing index), price (equity, not debt), and `PayerCapacityYear` (ocfB, capexB), `CompanyCip` (PP&E only; no CIP concept), `Payer.kpis` for CoreWeave. Oracle and Nebius have prices here but no XBRL.

---

## 7. aiwc/dl_b/ — more EDGAR submission indexes, captured 2026-10-17

| file | entity | filings | range |
|---|---|---|---|
| `hut_sub.json` (42 KB) | Hut 8 Corp., CIK 0001964789, HUT | 260 (Form 4: 78, 8-K: 42, 144: 24, 425: 20) | 2023-02-07 to 2026-09-30 |
| `sub_0001878848.json` (54 KB) | IREN Ltd, CIK 0001878848, FYE 0630 | 321 (6-K: 123, 8-K: 31) | 2021-08-26 to 2026-10-13 |
| `sub_0001769628.json` (157 KB) | CoreWeave (duplicate of research/dl/sub.json) | 1001 | 2025-08-28 to 2026-10-14 |

The shape is the same as sub.json; `items` holds 8-K item codes (e.g. "1.01,2.03,7.01,9.01" = material agreement / debt obligation). **Seeds:** document (filing index; 8-K 1.01/2.03 rows are candidate instrument events), party (CIK, ticker, SIC, addresses).

---

## 8. aiwc/gpu/ — GPU collateral market and operator fundamentals, asof 2026-10-17

### gpu/market.json (51 KB): research store
- `chips[12]` (A100, H100, H200, B200, GB200 NVL72, B300, GB300 NVL72, Vera Rubin NVL72, Rubin Ultra NVL576, MI300X/325X/355X): `model, vendor, gen, first_volume_ship` ("2020-05"), `tdp_w, system, capex_per_gpu_usd{lo,hi,what,src,date}, perf_fp8_dense_pflops, src[]`.
- `roadmap[3]`.
- `rental[81]`: `model` (H100 44, B200 15, H200 10, A100 7, GB200 3, B300 1, MI300X 1), `kind` (index 49, on-demand 29, 1yr 2, 3yr 1), `usd_per_gpu_hr` (3 null), `date` (mixed: "2023-03", "2024-01/2024-05", "2026-09-14 (week)", "undated (2026)"), `provider_or_index` ("Silicon Data - neocloud median"), `src, source_date`.
- `resale[9]`: `model, usd, date, condition, src`. Units **mix per-GPU and per-8-GPU node** (H100 14,250 per GPU vs 195,000 per HGX node).
- `residual_bands_pct_of_new` (H100/H200/B200/B300 × 2026/2028/2030, as text "27-39 / 39-53").
- `useful_life[17]` (company, years; CoreWeave 6, Meta 5.5, Nscale "5-6").
- `power` (PUE lo 1.1 / hi 1.54; EIA cents/kWh), `license_notes` (SemiAnalysis paywall: do not redistribute), `gaps[]`.

### gpu/gpu_out.json (59 KB): compacted, display-ready derivative
`rent{H100[14],A100[2],H200[7],B200[9]: {d,v,src,lbl}}` (time series), `lists[13]` (provider list prices `{m,v,p,src}`, e.g. CoreWeave HGX H100 $6.16/GPU-hr), `resale[2]`, `resid{model:{year:[lo,hi]}}` (numeric midpoints of the market.json bands), `chips[11]`, `life[17]`, `pue`, `cents[16]` (state industrial power prices), `ops[12]` (operator summary, short keys), `lic`, `pwp[26]` (per-case power economics: `id` = case id, `mw, pue, px, b26, b30, b30s` (annual power bill USD mm), `rent, payer, kw, liq, gen, debt_m, cpn, src`), `pwg` (PJM/MISO capacity prices, EIA STEO).

### gpu/operators.json (72 KB)
`operators[12]` (CoreWeave, Nebius, IREN, Applied Digital, Cipher, TeraWulf, Hut 8, Galaxy, Core Scientific, Lambda, Crusoe, Fluidstack). Fields: `name, ticker, cik, role, latest_filings[], q3_2026_reported, gpus[{model,count,asof,src}]` (counts mostly null; IREN 150,000 total), `mw{active_it_mw, contracted_mw}` (CoreWeave 1500 / 3700 @2026-06-30), `revenue_q/ttm, adj_ebitda_q, backlog_rpo_bn, capex_q, depreciation_q, interest_expense_q, useful_life_years, gpu_secured_debt[]` (CoreWeave 9 facilities: `name` "DDTL 1.0", `amount_m` 2300, `drawn_m` 1300, `rate, maturity, amortization, collateral, customer_contract, src`), `other_debt[], utilization, contract_terms, power_cost, notes`. Units: USD mm; `*_bn` in USD bn.

### gpu/site_power.json (117 KB)
`projects{CASE}` × 26: `it_mw{value, gross_utility_mw, delivered_mw, est_it_mw, est_basis, note, src}`, `site_power{state, iso, utility, supply, onsite_generation, contract, disclosed_price, proxy_price{…EIA}, src}`, `who_pays{payer, structure, min_demand_or_take_or_pay, if_tenant_leaves, basis}`, `density{max_kw_per_rack, liquid_cooling, cooling, chip_gen, future_density}`, `outlook_drivers[]`, `est_power_cost{gross_mw_used, est_annual_mwh, price_c_kwh, est_annual_power_cost_musd}`. It also has `general` (PJM BRA, MISO PRA, EIA prices by state, STEO, Waha gas, large-load tariffs). `iso` is free text ("ERCOT (presumed)", "Denton/Austin: ERCOT…; Muskogee: SPP"). `supply` is grid in 19 of 26.

**Seeds:**
- `GpuModelDetail` (launch, newPrice from capex lo/hi, resale, valueByAgeYears from resid, assumptions.usefulLifeYears).
- `GpuPricePoint` (rent series; **no region, p25/p75 or nProviders**).
- `GpuProviderPrice` (`lists`; no `capturedAt` per row, only the file asof).
- `GpuCollateral` / `GpuDeployment` (operators.gpu_secured_debt; unit counts mostly missing).
- `CompanyCip` (capex and PP&E context).
- Site fields `utility`, `iso`, `powerSource` (from supply), `itMw`.
- The site table's power-cost facts (M).

---

## 9. aiwc/feedx/ — power and docket feeds cross-referenced to cases, 2026-10-17

| file | shape | entity |
|---|---|---|
| `meta.json` (223 B) | `{asof:"2026-10-17T13:47:40", near_rule:"same county or <= 75 mi between US Census 2024 county internal points", load_zone:"not applied…", site_power_used:true}` | matching rules for `near` |
| `feed_x.json` (120 KB) | list[142] `{d, src, kind, iso, id, name, fuel, mw, county, state, status, title, doc_title, link, near[], near_mi{}, links[{case,basis}], why, lvl, raw}` + kind-specific `filer, docket, project, poi, dev, to, cycle, withdrawn_code, requested_isd, size_category, event_d, fields{}` | interconnection-queue and regulatory items: FERC filings 55 / issuances 5, PJM queue withdrawals 42, ERCOT GIS cancellations 26 and large-load docs 5, PUCT filings 6, SPP dropped 2. `id` = docket ("ER26-3752") or queue id ("AH1-563", INR "24INR0459"). `near` lists case ids within 75 mi; `lvl` 0:108, 1:31, 2:3. d 2026-08-26 to 2026-10-16 |
| `docket_x.json` (275 KB) | list[245] `{docket:"FERC\|EL25-49", agency, no, topic, d, by, what, status, effect, lvl, cases[], link, raw{d,by,t,u}, docket_raw{t,why,st,nx,u}}` | docket filings: 56 dockets, agencies FERC 184, PUCT 40, LPSC 5, … status comment 40, answer 39, intervention 36, procedural 32, filing 32, order 21 …; 92 rows tied to cases (CLOUDHQ 17, BEIGNET 15, CIFR_BARBER 15, IREN_CHILDRESS 15 …). d 2025-05-14 to 2026-10-16 |
| `proj_x.json` (339 KB) | list[182] `{id, plain, feeds[], related[], effect, lvl, iso, state, stage, mw, raw}` | power projects (gas plants 33, large-load interconnections 101, campus power 23, SMR 7, …). `raw` is **exactly** the matching `data/power.json.projects[]` record (182 of 182 equal). It adds the plain-English `plain`/`effect`, `feeds` (case ids, 24 non-empty) and `lvl`. stage: under construction 38, proposed 37, approved 34, permitting 27, stalled 23, … |

`raw` (from power.json) keys: `id, n` (name), `ty, dev, ut` (utility), `iso, st, mw, dc` (served data center), `debt` (text), `stage, why, cod, cod0, risk, rw, dk[{a,no,t,la,ld,u}]` (dockets), `ev[{d,w,q,u}]`, `src[], conf, x[[kind, grp, role, label]]` (cross-links to aidebt grp).

**Seeds:** power plant (proj_x, but **no lat/lon, OEM, units or air-permit numbers as fields**), event (feed_x, docket_x), document (docket links), obligation edge 'Power purchase' (raw.x links), and the site → plant link (case_seeds.power_ids and proj_x.feeds). types.ts: `PowerPlantRow` (name, state, permittedMw ← mw, status ← stage mapped to PlantStatus, serves ← dc), `PowerPlantDetail.fuel.docket`, `PowerPlantDetail.history`, `Alert` (lvl ≥ 1), `DcRecord` (type 'Utility'/'Filing').

---

## 10. aiwc/decks/ — facts extracted from investor PDFs, 2026-10-17

| file | rows | shape | sources |
|---|---|---|---|
| `crwv_facts.json` (42 KB) | 149 | `{k, v, u, asof, file, page, q, conf}` | 4 CoreWeave PDFs: March-2026 Investor Presentation 69, Q2-26 Earnings Presentation 45, 2026-03 DDTL-4 Overview 26, Q2-26 Outlook 9 |
| `iren_apld_facts.json` (67 KB) | 227 | same + `co` (IREN 108 / APLD 119), `derived`+`calc` (21), `src` (15 footnote refs), `note` | APLD June-2026 investor deck 119, IREN FY26 earnings 79, IREN Microsoft $9.7bn contract deck 29 |

Samples: `{k:"active_power", v:1.5, u:"GW (1.5GW+)", asof:"2026-06-30", file:"b9b87e5f-CoreWeave-Q2-26-Earnings-Presentation.pdf", page:5, q:"1.5GW+ Active Power"}`; `{k:"msft_tcv", v:9.7, u:"USD bn", asof:"2025-11-03", co:"IREN"}`.
**Quality:**
- `v` mixes int, float, str, dict and list types.
- `asof` mixes "2026-03", "Q2 2026", "quarterly" and "FY26 (FYE 2026-06-30)".
- The PDF `file` names have a hash prefix and **no URL**.
- crwv_facts repeats 10 keys at different as-of dates.
- `conf` is undocumented: it is true for exactly the 69 March-2026 deck facts and false for everything else.
- `derived: true` rows are Marden arithmetic (tag M).

**Seeds:** fact table (key, value, unit, asOf, document + page, quote; company-reported material, tag R or F depending on policy for investor decks), document (PDF), and `GpuDeployment`/`Payer` KPIs (contracted power, backlog, Microsoft contract terms).

---

## 11. aiwc loose files

| file | size | shape | entity / notes |
|---|---|---|---|
| `case_seeds.json` | 65 KB (2026-10-19) | list[29] `{id, name, aiw_idx[], debt_rows[{22 aidebt cols as dict}], power_ids[], collmeta_keys[]}` | **The join manifest** from case → aidebt rows, aiw_proj entries, power.json projects and collmeta keys; all resolve. The `debt_rows` are **stale snapshots**: only 13 of 54 equal the current aidebt.json row and 20 equal pretrace, so look rows up by id instead of using the copies |
| `aidebt.pretrace.json` | 609 KB (2026-10-17 23:25) | `{cols (22 names CSV), rows[707][22], watch[25], aiwx{80}, gn{29}}` | aidebt.json **before the TRACE price update**. Ids and order are identical; 381 of 707 rows differ (yld 315, pxd 208, px 173, cpn/mat 3 each). The pretrace has no `pxsrc` key. Example: EX-APLD-010 px 106.29 (07-31) → 102.375 (10-20). Gives a 2-point price history (change between pulls), not a week-on-week change. cols: `id, issuer, grp, cat (corp 310/abs 159/util 89/proj 87/bdc 62), inst, cpn, mat, amt_m, rank, rating, tenants, backstop, px, pxd, yld{value,basis}, holders[], flags[], score, why, src[], chain, status` |
| `aiw_proj.json` | 125 KB | list[80] `{issuer, project, site, mw, counterparty, term, rfs_prior{text,date,src}, rfs_latest{text,date,src}, changes[{date,what,quote,src}], financing[{instrument, amount_usd, lenders}], status, status_why}` | **Project delivery tracker** by issuer (Applied Digital 8, Oracle 7, TeraWulf 7, CoreWeave 6, Nebius 6, Core Scientific 6, …). status: on track 52, at risk 10, slipping 7, unknown 7, cut 4. `rfs_prior`/`rfs_latest` (58 and 61 non-null) give dated ready-for-service statements from 10-Q/10-K and press. There is no id field: join by array index (case_seeds.aiw_idx) or by `issuer\|project` (aidebt.aiwx). `mw` is int or text; `amount_usd` is in **USD units, not mm** |
| `global_inputs.json` | 37 KB | `{asof, purpose, conventions, items{1_treasury_curve … 10_bankruptcy_lease_rejection_cap}}`; each item has `value/values, units, asof, src, quote, reliability`, often `inferred` | sourced market inputs: UST curve 2026-10-16, S&P default rates, recovery studies, DC rents, construction cost per MW, cap rates, vacancy, GPU rental/residuals, project delays (Sightline, LBNL), 11 USC 502(b)(6) |
| `globals.json` | 5 KB | flat dict: `paths` 20000, `pd1_by_rating{AAA…CCC}`, `rf{2y…30y}`, `bust{…}`, `delay{…}`, `reject{…}`, `renew{…}`, `sofr` 4.12, `loss_threshold` 0.98, `_assumptions[15]`, `_calibration{…}` | model-wide parameters for the pricing engine, built from global_inputs. This is the method page for `methodVersion`; there is no version string in the data |

---

## 12. Mapping to types.ts and DB tables

| DB table | primary source(s) here | key | notes |
|---|---|---|---|
| **site** | cases.site/capacity, memo/titles, memo.headline/status, gpu/site_power, aiw_proj | case id → slug | no lat/lon; multi-site cases need child rows |
| **campus / building** | cases.capacity.per_building / per_site_mw (2 files), site_power.it_mw.per_site_* | – | almost nothing structured; no footprints, sqft or permits per building |
| **plant** | feedx/proj_x (= power.json) | N-/P- id; case_seeds.power_ids | no units, OEM, coordinates or air-permit fields |
| **party** | memo.chain, cases.parties, operators.json, sub.json/dl_b, aidebt.gn | name; grp; ticker; CIK | needs entity resolution (free-text names) |
| **contract / lease** | cases.lease[], aiw_proj (term, counterparty, rfs), decks (MSFT contract) | case id + tenant | rent and outside date mostly null |
| **instrument** | memo.instruments/cap, cases.debt, params.debt, aidebt rows via debt_row | aidebt id | CUSIP only inside aidebt NP- ids and 3 text mentions |
| **holder position** | memo.instruments.holders (text), cases.parties.fund_holders (1), cases.debt.holders (1) | – | unstructured; needs N-PORT data from another area |
| **event** | state/events.json, out/live.json, feedx/feed_x + docket_x, cases risks.*.evidence, memo.issues, aiw_proj.changes | case id + url/accession | dedupe needed |
| **price** | params.debt.price (latest), cases.debt.price, memo.instruments.price, aidebt vs pretrace px, out (model), research y_* (equity) | instrument + date | 1–3 points per instrument; no daily series |
| **obligation edge** | cases.lease (Lease), credit_support (Guarantee or backstop), memo.chain roles, aiw_proj.financing (Loan), proj_x.raw.x, operators.gpu_secured_debt | party ids | – |
| **document** | about 1,878 unique URLs (498 sec.gov, 233 ferc.gov), deck PDFs (file + page), EDGAR filing indexes | URL / accession | no `sourceName` field anywhere |
| **fact** | cases fields + quote, memo.security (k/v/basis/src/q), decks facts, research facts.json (XBRL), global_inputs | (entity, field, asOf) | tag must be derived from basis/URL |

types.ts coverage:
- **Good:** `SiteDetail` text fields, `ChainLink.party/role/reason`, `Evidence`, `ProjectFile` (parties, tranches, structure prose, checklist n/d), `InstrumentDetail.terms/recovery/stress` (partial), `LeaseRecord` (partial), `ObligationEdge` (partial), `GpuModelDetail` (partial), `PowerPlantRow` (partial), `Alert` / `materialEvents`, `RiskList` (memo/watch_plain + aidebt.watch).
- **Weak or none:** `CompletionResponse`/`CushionProjection`, `Milestone`, `Holder`, `priceHistory`, `change1w`, `markGap`, `CurveTier`, `DataCenterRow` geo, `Building`, `PowerUnit`/`AirPermit`, `PayerCapacity` ratings and triggers, `CapacityEstimate`, `ContagionResult`.

## 13. What the V1 screens need that is not in this area

1. **Coordinates and geometry:** no lat/lon, parcels, building footprints or imagery URLs (needed by DataCenterRow, Building, the map, `SiteSchedule.imagery`).
2. **Outside dates and rent:** `lease.outside_date` is non-null in 3 of 40 leases (all text) and `annual_rent_m` in 3 of 40. `params.outside_months` is an assumption. Cushion months and the completion monitor therefore have no filed basis.
3. **Completion projections:** no p10/p50/p90 dates. Only free-text `scheduled_completion` and `original_completion`, plus params `months_to_complete` (M) and out `p_late`.
4. **Milestones:** no structured milestone keys or status (land_zoning, air_permit, utility_agreement …). They can only be derived from risks evidence, aiw_proj rfs/changes and events.
5. **Chain link status and rule codes:** no red/amber/green and no `ruleCode` (T2 …). Must be computed (memo issue levels and event sev are inputs).
6. **Holders and marks:** no structured fund, par, mark, filing date or NAV share per holder; only prose. `Holder`, mark gap and NAV hit need N-PORT/BDC data from another area (e.g. data/fundpos.json, q3x).
7. **Price history:** at most 3 dated points per instrument across files (memo/cases mark, aidebt pretrace vs post-TRACE, params TRACE). No daily Marden price, no `change1w`, no curve points by tier and tenor.
8. **Explicit tags and source names:** there is no F/R/M field and no `sourceName`. Derive tags from `basis` and URL domain. `methodVersion` is absent (globals.json is the method).
9. **Ratings, CDS and spreads as data:** tenant/sponsor ratings and bond levels are prose (cases.parties); params.tenant.rating/spread_bp are model inputs. `RatingState` triggers, CDS and lease-adjusted leverage are absent.
10. **CUSIP/ISIN as a field:** absent here.
11. **Power plant detail:** no unit OEM/model/count, air-permit numbers, filed/decided dates or fuel pipeline as fields.
12. **GPU index detail:** no region, p25/p75 or provider count. Units financed, delivered and energized per facility are mostly missing (operator GPU counts are mostly null).
13. **User data:** watchlist, alert settings and home layout are not in the data, as expected.
14. **Stress outputs:** `sens` gives Δp_loss and ΔEL only, with no price-after, rent cover or holder NAV hit, and none of the spec's three named scenarios (OpenAI funding miss, Oracle downgrade, 6-month power delay) as such.

## 14. Data-quality summary

- **Versioning and staleness:**
  - out/all.json is older than out/<CASE>.json, and the two disagree.
  - case_seeds.debt_rows are stale copies.
  - memo.bak_secfill and memo_prefix are older memo generations.
  - aidebt.pretrace is the pre-TRACE state.
  - research/dl/sub.json duplicates dl_b/sub_0001769628.json.
- **Units:** USD mm (`*_m`) across cases, memo, params and operators; USD units in aiw_proj `amount_usd` and aidebt watch `amount_usd`; USD bn in `*_bn`. GPU resale mixes per-GPU and per-node values. MW is IT, gross or delivered depending on the field.
- **Dates:** ISO dates, months, quarters ("2026-Q4"), halves ("2026-mid"), ranges ("2024-01/2024-05") and prose. events `seen` are ISO timestamps with offsets; Yahoo timestamps are unix seconds.
- **Events:** duplicates within and across cases, mislabeled "court" items, and Google News redirect URLs.
- **Text-first values:** ratings, coupons (some strings), lease terms and ISO assignments are free text and need parsing before they can be shown as `Sourced<number>`.
- **Prompt-injection hygiene:** all quoted strings are third-party text and must be stored and rendered as data.
