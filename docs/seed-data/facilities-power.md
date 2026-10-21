# Data map: facilities-power area

Scope: `power.json`, `dockets/` (incl. `dockets/out/`), `aiwc/q3x/` (subdirs `docs`, `epoch_dl`, `fac`, `geo`, `lst`, `night`, `osm`, plus every loose file in `q3x/`).
Root: `/tmp/claude-0/-home-user/041c65f7-05f2-55e2-9ba6-73f6e02ba8df/scratchpad/data` (from `marden_data_2026-10-20.zip`). All paths below are relative to that root.
Profiling scripts: `/tmp/claude-0/-home-user/041c65f7-05f2-55e2-9ba6-73f6e02ba8df/scratchpad/datamap/scripts/fp/` (read-only `python3 -I`; nothing in the corpus was executed).

Conventions in this note
- "Rows" = records in the main array. Sizes in bytes.
- Sample values are copied from real records. Where a field is free text, a short excerpt is given.
- "Tag" = the F/R/M tag of `types.ts` (`Filed | Reported | Marden model`). **No file in this area carries an explicit F/R/M tag field.** Where a proxy exists (confidence, basis, source kind, URL domain), it is named.
- Many q3x files are UI page caches: they carry `made` (build timestamp), `tiles` (pre-rendered KPI strings like `["Data centers mapped","102","85 in the US; 21 carry debt we price"]`) and `notes` (method text). `tiles` and `notes` are display copy, not seed data. The methods they describe can go on method pages.

---

## 0. One-screen summary

| File / dir | Size | Rows | Entity | Main IDs | Seeds |
|---|---|---|---|---|---|
| `power.json` | 371,423 | 182 projects, 56 dockets, 164 feed items, 44 party keys | Power projects (gas plants, large-load interconnections, SMRs, co-location), regulatory dockets, filing feed | `projects[].id` (`N-*`, `P-NNN`), docket `no`, party key (`gx`) | plant, event, document, party, obligation edge (power purchase), fact |
| `dockets/inventory.json` | 144,029 | 90 (case × proceeding) | Proceedings watched per financed project | `case_id`, `proceeding_key` | event source registry, document |
| `dockets/out/*.json` | 73 files, ~1.37 MB total | 5,801 items | Docket/agenda line items (date, title, URL) | file name = `proceeding_key` | event, document (DcRecord) |
| `dockets/seen.json`, `first_seen.json`, `new_filings.json` | 400,160 / 3,875 / 2 | 5,723 URLs / 11 / 0 | Monitor state | `proceeding_key`, URL | first-seen dates for events |
| `q3x/dcmap.json` | 514,192 | 102 | Data center campuses (21 financed + 81 Epoch-only) | `key`, `case`, `pw[].id`, `debt[].id` (EX-) | campus, site, building (counts), fact |
| `q3x/site.json`, `sites.json` | 51,954 / 16,366 | 21 / 21 | Financed campuses: coordinates, Sentinel-2 build progress, delivery dates | `key`, `case`, `ids` (EX-) | site, campus, imagery, schedule facts |
| `q3x/sqft.json` | 6,412 | 22 | Campus square footage / acres / building counts | `key` | campus facts, building totals |
| `q3x/s2_epoch.json` | 42,959 | 81 | Sentinel-2 scenes for Epoch-only campuses | map key = dcmap `key` | imagery |
| `q3x/heat.json`, `night/night_raw.json`, `lst/lst_raw.json` | 23,747 / 529,833 / 13,305 | 21 / 21 sites × ~300 / 3 sites | Night-time land-surface temperature (MODIS, Landsat) | `key` | (out of V1 scope per spec) |
| `q3x/pw_found.json`, `pw_missing.json` | 34,546 / 19,958 | 69 / 69 | Utility / ISO / power source for Epoch-only campuses | `key` | campus power facts |
| `q3x/dock.json`, `speed_press.json` | 85,814 / 1,767 | 90 / 5 | UI copy of docket inventory + "first press" lag check | `case`, `key` | event (first press date) |
| `q3x/cat.json` | 71,503 | 49 | Upcoming catalysts (permits, maturities, covenants) | `case`, `debt[].id` | event (scheduled), milestone |
| `q3x/epoch_dl/` | 2.4 MB | 94 DCs, 560 timeline rows, 234 chip rows, 1,300 shapes | Epoch AI "AI data centers" dataset (CC BY 4.0) | Epoch `Name` / `id` | campus, building footprints, capacity estimate, GPU counts |
| `q3x/osm/` | 27 MB, 32 files | 28 OSM API extracts + 4 Overpass results (3 bad) | OpenStreetMap features around 25 sites | OSM element ids | building footprints, substations (geometry) |
| `q3x/geo/` | 10 MB | us-atlas / world-atlas TopoJSON, 102 points | Base map | - | map layer only |
| `q3x/fac.json`, `fac/*.json` | 394,564 + 4 files | 213 (+233 in parts) | Debt facilities (loans, bonds, ABS, GPU leases) | `fid`, `case` | instrument, contract, party, document |
| `q3x/comp.json` | 161,870 | 117 | Deal comps: spread at issue, price now | `fid`, `case` | instrument, price |
| `q3x/ctr.json` | 58,970 | 29 | Lease / capacity contract terms per financed case | `id` (= case) | contract/lease |
| `q3x/hold.json`, `ins.json`, `oa.json`, `contagion.json`, `dry.json`, `forecast_q3_2026.json` | 3.0 MB / 52 KB / 69 KB / 77 KB / 69 KB / 68 KB | 523 fund families / 127 / 86 / 86 / 56 pos | Holders, marks, OpenAI/Oracle/CoreWeave exposure, mark forecasts | EX- ids, CUSIP, fund CIK | holder position, price, obligation edge |
| `q3x/rv.json`, `rvc.json`, `rec.json`, `underwater.json`, `trg.json`, `triggers_raw.json`, `cov_crwv.json`, `suppliers.json` | various | 29 / 72 / - / 12 / 15 / 16 / 1 / 24 | Relative value, price change decomposition, recovery, GPU LTV, covenant triggers, supplier concentration | EX- ids, case ids | price, fact, rating trigger |
| `q3x/docs.json`, `docs/` | 93,937 + 7.5 MB | 199 / 400 / 1,581 idx / 1,287 titles / 10,443 tickers | SEC EDGAR financing exhibits | accession no., CIK, URL | document |
| `q3x/ust2023..2026.csv` | ~19 KB each | 249/249/248/199 days | US Treasury par yield curve | date | price inputs (curve) |

---

## 1. `power.json` (371,423 bytes, JSON object)

Top level: `asof` = `"2026-10-20T13:20:18"`, `projects[182]`, `dockets[56]`, `feed[164]`, `sources{8}`, `gx{44}`, `gf{16}`.

### 1.1 `projects[]` (182) - power projects tied to data-center load
Real-world entity: one power-supply project or regulatory action serving data-center load: a utility gas plant, an on-site generation plant, a large-load interconnection request, an SMR/nuclear restart, a co-location deal, a transmission project, or a queue/policy item.

Fields (all 182 have the first 20; `x` 125, `nd` 62, `qw` 2):

| Field | Meaning (observed) | Sample |
|---|---|---|
| `id` | Project id. 53 `N-*` (named, curated) + 129 `P-NNN` (pipeline list). Unique. | `N-HYP-CCGT1`, `P-090` |
| `n` | Name | `Entergy Louisiana Hyperion CCGTs (first 3 plants, Richland/Franklin area)` |
| `ty` | Type: large-load interconnection 101, gas plant 33, data center campus power 23, SMR 7, solar+storage 6, co-location 5, fuel cell 3, nuclear restart 3, transmission 1 | `gas plant` |
| `dev` | Developer / sponsor (3 empty) | `Entergy Louisiana` |
| `ut` | Utility (94 empty) | `Entergy Louisiana`, `Xcel`, `ERCOT (Oncor area) + on-site generation` |
| `iso` | MISO 66, SPP 56, ERCOT 34, PJM 13, WECC/other 12, NYISO 1 | `MISO` |
| `st` | State, 2-letter (1 null: `N-PJM-LL-IRAS`). TX 39, KS 22, IA 18, ND 18, OK 16 ... | `LA` |
| `mw` | Capacity in MW: 117 numeric, 65 null | `2260` |
| `dc` | Data center served (text) | `Meta Hyperion (Richland Parish)` |
| `debt` | Linked debt (text; 148 empty) | `Beignet Investor LLC US$27.3bn 23.6-year 144A bond (...)` |
| `stage` | under construction 38, proposed 37, approved 34, permitting 27, stalled 23, queued 8, withdrawn 8, operating 4, denied 3 | `approved` |
| `why` | Latest change (139 empty) | `Air-permit expansion contested; residents granted more time (Sept 2026)` |
| `cod` | Expected COD (free text; 98 empty) | `2032-07-01`, `2030-05 (ERCOT projected COD serial 47635)` |
| `cod0` | Original COD (3 non-null, free text) | `2026-08` |
| `risk` | low 91, medium 67, high 24 | `medium` |
| `rw` | Risk write-up (27 empty) | `Approved Aug-2025; credit exposure is the Beignet bond ...` |
| `dk[]` | Regulatory refs `{a agency, no number, t title, la latest action, ld latest date, u url}`; 231 refs across 132 projects; 208 refs have **no URL**; `ld` 2022-07 .. 2026-10-15, 74 empty | `{"a":"LPSC","no":"U-37425","la":"Settlement approved","ld":"2025-08-20","u":"https://www.wafb.com/..."}` |
| `ev[]` | Evidence `{d date, w who/source, q headline, u url}`; 269 items, 2022-07 .. 2026-10-16; `w` starts with "News" for 117 | `{"d":"2025-08-21","w":"News (Power Engineering Magazine)",...}` |
| `src[]` | Source URLs (1-3 each) | `https://www.ucs.org/about/news/...` |
| `conf` | high 54, med 55, low 20, null 53 | `med` |
| `x[]` | Chain tuples `[kind, key, role, note]` (231 tuples). kind: G 117 (key in `gx`), L 82 ("Name|note" listed credit, e.g. `META`), P 25 (private name), B 7 (lowercase key, e.g. `galaxy helios`, not resolvable in this area). role (inferred from usage): C debt vehicle, B developer/builder, T tenant/payer, U utility, S power supplier, K (1, unknown) | `["L","META","T","Meta Platforms|AA- rated; pays the lease"]` |
| `nd` | `priv` 61, `hyp` 1 (meaning not documented; `hyp` row is "Unnamed ~1 GW Tier-1 hyperscaler self-build in North Dakota") | `priv` |
| `qw` | 1 on two queue-wide items (`N-ERCOT-BATCH0`, `N-PJM-LL-IRAS`) | `1` |

Joins
- `id` <- `q3x/dcmap.json rows[].pw[].id` (108 distinct ids referenced; 107 exist here; `MTEP-50539` exists only inside dcmap).
- `id` <- `feed[].p` (32 of 164 feed items).
- `x[].key` (kind G) -> `gx` keys; 19 of 44 `gx` keys equal EX- instrument group codes used in q3x (`BEIGNET`, `ABILENE`, `JUPITER`, `VANTAGE_ORCL`, `CRWV`, `ORCL`, `IREN`, `HUT`, `CIFR`, ...). The remaining gx keys are utilities/suppliers/developers (`ENTERGY_LA`, `ONCOR`, `AEP`, `CEG`, `TLN`, `BE`, `CRUSOE`, `QTS`, ...).
- `dk[].no` -> `power.dockets[].no` (21 of 36 distinct numbers) and -> `dockets/out/<AGENCY>_<no>.json` (8 match: `FERC_CP26-80`, `FERC_ER26-3265`, `FERC_ER26-3531`, `LPSC_U-37425`, `MPSC_U-21990`, `PUCT_58481`, `PUCT_59142`, `VASCC_PUR-2025-00058`).
- No `case` field: link to financed cases only via dcmap `pw[]` or via text in `dc`/`debt`.

Data quality
- `mw` mixes units and meanings: `N-ERCOT-BATCH0` = 474,000 (aggregate ERCOT queue, not a plant); `P-090` and `P-101` = 1.0 (GW parsed as MW); `P-087` = 2026.0 (year in name "Evergy Kansas 2026 predetermination"). 65 nulls.
- No lat/lon, no county for any project.
- `cod`/`cod0` free text mixing ISO dates, years and prose.
- `dk[].u` mostly missing; where present 13 are FERC eLibrary, others are news sites (wafb.com, nola.com, ktsm.com, wpr.org, datacenterdynamics.com) - so a docket ref is not automatically "Filed".
- Plant technology / units: OEM/model appears only in prose (`GE ` in 18 records, `Bloom` 4, `turbine` 4, `fuel cell` 3, `simple-cycle` 2, `CCGT` 1); no structured unit rows, counts or permit numbers (permit numbers appear in prose in ~7 records).
- Agency names in `dk[].a` are not normalized (e.g. `Haskell County` and `Haskell County Commissioners Court`; `PUCT`, `PUCT/SOAH`, `PUCT - Texas Energy Fund`).

### 1.2 `dockets[]` (56) - key regulatory proceedings
Fields: `a` agency (FERC 37, PUCT 8, LPSC 2, others 1 each), `no` (6 null), `t` title, `why`, `st` status text, `nx` next step (text, mostly null), `v` bool (51 true / 5 false; false on LPSC U-37425, MPSC U-21990, GA PSC 56298, VA SCC PUR-2025-00058, PUCO 24-508-EL-ATA; meaning undocumented), `u` docket URL, `lf[]` last filings `{d, by, t, u}` (mostly 5 each; 2025-05-14 .. 2026-10-16).
Sample: `{"a":"FERC","no":"EL25-49","t":"PJM co-location of large loads at generating facilities ...","st":"Compliance filing (ER26-1479, 08/17/2026) protested; PJM answered 10/15/2026; awaiting FERC order","u":"https://elibrary.ferc.gov/eLibrary/docketsheet?DOCKET=EL25-49-000"}`.
17 of these match a `dockets/out` file.

### 1.3 `feed[]` (164) - new filings 2026-08-26 .. 2026-10-20
Fields: `k` kind (ferc_filing 74, pjm_queue_recent_withdrawal 42, ercot_gis_cancellation 26, puct_filing 7, ferc_issuance 5, ercot_large_load_doc 5, spp_queue_status_change 2, spp_queue_dropped 2, ferc_keyword_hit 1), `a` agency, `d` date, `no` docket/queue number (5 null; e.g. `EL26-108`, `24INR0459`, `GEN-2017-220`), `t` title, `u` URL (FERC accession links), `p` project id (32 non-null), `m[]` matched keywords, `f` filer, `mw` (always null), `cov` 0/1 (90 ones; not equal to `p` non-null).
Sample: `{"k":"ferc_filing","a":"FERC","d":"2026-10-20","no":"EL26-108","t":"(doc-less) Motion to Intervene of BlackFlare, Inc. under EL26-108.","u":"https://elibrary.ferc.gov/eLibrary/filelist?accession_number=20261020-5064","p":null,"f":"BlackFlare, Inc.","cov":1}`.

### 1.4 `sources`, `gx`, `gf`
- `sources`: fetcher flags (`ferc_dockets`, `pjm_queue`, `pjm_eit`, `spp_queue`, `ercot_gis`, `ercot_ll_docs`, `puct` true; `ferc_keywords` false). Can drive the `pipeline` stream status.
- `gx`: party dictionary `key -> [display name, ticker or "private" or ""]`, 44 keys. Sample: `"ENTERGY_LA": ["Entergy Louisiana","ETR"]`, `"CRUSOE": ["Crusoe","private"]`, `"BEIGNET": ["Beignet bond (Meta Hyperion JV)",""]`.
- `gf`: party function for 16 keys: `u` utility (ENTERGY_*, ONCOR, AEP, WEC, GEORGIA_POWER, SOUTHERN, DOMINION*, DTE, XCEL) or `g` generator (VST, CEG, TLN, BE).

Tag proxies: `dk` with an official docket URL -> F; `ev` with `w` "News ..." -> R; `conf` low/med/high is a confidence, not a tag; `mw` derived values (e.g. "MW (~2.26 GW) derived as 7.5 GW total less 5.3" in `rw`) -> M.

Seeds: **plant** (gas plant / fuel cell / campus power / SMR rows), **event** (dk latest action, ev, feed), **document** (dk/ev/src/lf URLs), **party** (gx, gf, x), **obligation edge** (`x` tuples with roles T/U/S give tenant->developer, utility->site, supplier->site; amounts absent), **fact** (mw, stage, cod, risk). Large-load interconnection rows are better modelled as site power facts / `largeLoad` than as plants.

---

## 2. `dockets/`

### 2.1 `dockets/inventory.json` (144,029 bytes)
Top: `asof` `"2026-10-17"`, `generated_by` (`"dockets/build_inventory.py (fetchers in dockets/lib.py, registry in dockets/registry.py, raw results in dockets/out/)"` - those scripts are NOT in the corpus), `scope` text (lists excluded cases APLD_PF1, GLXY_H1, QTS, XAI_VCI, SOUTHGATE with reasons), `counts` `{rows:90, unique_proceedings:73, unique_fetchable:42, cases:24}`, `rows[90]`.
Row = one (financed case, proceeding) pair. Fields: `case_id` (24 cases: VANTAGE_WI 8, BEIGNET 7, JUPITER 7, SALINE 6, ABILENE 5, FLASH_ABERNATHY 5, VANTAGE_TX 5, ...), `project` text, `proceeding` (human name), `proceeding_key` (= `dockets/out` file stem), `kind` (state_puc 29, county_board 21, ferc_docket 14, city_council 7, state_air_permit 7, court 5, zoning 4, other/iso_queue/state_env_permit 1 each), `why_it_matters`, `list_url` (official portal), `fetch {method get|post|playwright|none, url, notes, data?}`, `works` (59 true / 31 false), `n_items`, `latest[] {d,t,u}` (2025-08-12 .. 2026-10-16), `next_date` (2026-10-19 .. 2026-12-14), `next_dates[] {d, what, src}`, `flags_30d[] {d, sev schedule|adverse, what, u}` (5 total), `notable_30d[] {d, what, u}` (12 total), `notes`.
Sample: `{"case_id":"ABILENE","proceeding_key":"ABILENE_CC","kind":"city_council","list_url":"https://abilenetx.portal.civicclerk.com/","works":true,"n_items":4,"next_date":"2026-10-22"}`.
Flags present: NDPSC_PU-26-22 (schedule), NMED_AQB_26_57 (schedule: hearing moved to 7 Dec 2026), FERC_ER26-3265 (adverse: deficiency letter), FERC_ER26-3531 (adverse: deficiency letter 2026-10-15), WIPSC_137-CE-229 (schedule: CPCN refiled).
Joins: `case_id` = financed case key used across q3x (`dcmap.case`, `site.case`, `cat.case`, `ctr.id`, `rv.id`, `fac.case`, `comp.case`). `proceeding_key` = `dockets/out/<key>.json` (73 = 73, exact). `q3x/dock.json` is a 1:1 UI copy (same 90 keys).

### 2.2 `dockets/out/*.json` (73 files)
One file per proceeding. Shape (all 73): `{key, works, error, items[], meta}`. `error` always null. 42 files `works:true` with items; 31 `works:false` with 0 items (e.g. `ABERNATHY_CITY`, `TAYLOR_CO`, `LOUDOUN_BOS`, `MI_COA`, `NM_SUPREME`, `TCEQ_MCCLOUD`, `CRWV_DDTL4_SITES`) - these 31 are known proceedings with no fetchable feed.
`items[]` = `{d, t, u}` only (5,801 items). Dates 2007-11-14 .. 2027-07-22.
File patterns (by prefix):
- `FERC_*` (9 files: CP26-80, EL26-67/69/70/73, ER26-1323/247/3265/3531): eLibrary filings, `meta {totalHits, retrieved}`. Sample item: `{"d":"2026-10-16","t":"Sidley Austin LLP submits request to update the service list ...","u":"https://elibrary.ferc.gov/eLibrary/filelist?accession_number=20261016-5182"}`.
- `PUCT_*` (6 working: 58000, 58317, 58481, 58482, 59076, 59142): Texas PUC Interchange items, `meta {case_style}`; titles prefixed with doc type, e.g. `[PRJ] PUC OPDM: ORDER ADOPTING NEW 16 TAC §25.194`.
- `TCEQ_*` (4 working: 178644, 182126, 182467, 182627): air-permit status events, `meta {project_status}` (e.g. `PENDING`); every item in a file points to the same project-report URL (44 repeated URLs in 182126, 33 in 182467). Sample: `{"d":"2026-08-24","t":"PUBLIC HEARING REQUESTED (DATE)","u":"https://www2.tceq.texas.gov/airperm/index.cfm?fuseaction=airpermits.project_report&proj_id=400428"}`.
- `NMED_AQB_26_57` (109 items): NM air-permit hearing record, `meta {post_id, status, heading:"AQB 26-57(P): Yucca Growth Infrastructure, LLC – YGI Microgrid (Project Jupiter)"}`.
- `WIPSC_*` (6630-TE-113: 3,090 items; 137-CE-229: 376), `LPSC_U-37425`/`U-37882` (50 items each; `meta.total` 182 and 122), `NDPSC_PU-26-22`/`PU-26-293`, `NYPSC_26-E-0045`, `VASCC_PUR-2025-00058` (`meta {MATTER_NO, status:"Under Appeal", disposition, disposition_date:"11/25/2025"}`), `ERCOT_PGRR145` (131).
- County/city bodies (`ABERDEEN_CC`, `ABILENE_CC`, `DENTON_CC`, `DICKENS_CO`, `DICKEY_CO`, `DONAANA_BOCC`, `DONAANA_PZ`, `ELPASO_CC`, `OZAUKEE_CO`, `SALINE_TWP_BOARD`, `SALINE_TWP_PC`, `SHACKELFORD_CO`, `WINKLER_CO`): meeting-level items (agenda/minutes), not agenda items; CivicClerk ones list future meetings.
Data quality: future dates are scheduled meetings (DONAANA_BOCC to 2026-12-08, OZAUKEE_CO to 2026-11-25, WINKLER_CO to 2027-01-25), except `NMED_AQB_26_57` item dated 2027-07-22 whose file is in a `/2026/07/` upload folder (likely a 2026 date typo at source). DENTON_CC has 57 items before 2024 (from 2021-11-09); SALINE_TWP_BOARD has 5 from 2007. LPSC/NDPSC/VASCC capped at 50 most recent items. Items have no type/agency field; agency comes from inventory `proceeding`, type from inventory `kind` or a bracketed title prefix.

### 2.3 Monitor state
- `seen.json` (400,160): `{proceeding_key: [url, ...]}` for all 73 keys (31 empty); 5,723 URLs, 5,633 unique. Biggest: WIPSC_6630-TE-113 3,090.
- `first_seen.json` (3,875): `{url: {key, seen, d, t, u}}`, 11 entries, `seen` 2026-10-19T12:27Z .. 2026-10-20T13:44Z (OZAUKEE_CO 3, NYPSC_26-E-0045 2, WIPSC_137-CE-229 2, FERC_EL26-67, FERC_CP26-80, LPSC_U-37882, PUCT_58317 1 each). Per `q3x/dock.json` notes the monitor launched 2026-10-17; earlier filings are stamped with that date.
- `new_filings.json`: `[]`.

Seeds (dockets/): **event** (each item: date, source = agency, type from kind, link affected = case), **document** (url + title), **DcRecord** (`type` from kind: Permit/Zoning/Utility/Court/Filing; `agency` from inventory), first-seen timestamps for `Alert.at` / `DataCenterRow.lastChange`. Tag: official portal items -> F.

---

## 3. `aiwc/q3x/` facility and power files

### 3.1 `dcmap.json` (514,192) - data center campus map
Top: `made` 2026-10-19T01:56:26Z, `tiles`, `rows[102]`, `map {w:960,h:600,states[56] (name, SVG path), borders}`, `src[3]` (Epoch AI CC BY 4.0; us-atlas; Sentinel-2 via Element 84), `notes[4]`.
Row = one campus. 21 rows `cov:true` (financed, hand-researched; keys like `stargate_abilene`), 81 rows keyed `ep:<Epoch name>` (Epoch-only).
Fields (all rows): `key` (unique), `name`, `lat`, `lon`, `case` (19 non-null), `cov`, `owner` (Google 20, Meta 17, '' 10, CoreWeave 9, ...), `users`, `tenant`, `state`, `loc` (address text; for covered rows includes county, e.g. `Lancium Clean Campus, 615 FM 2404 area, Abilene, Taylor County, TX`), `gross_mw` (11 non-null), `it_mw_plan` (11), `deliv_mw` (16), `sched`, `orig`, `pwr_status` (19 non-null text, e.g. `energized (grid + on-site gas); expansion permit pending`), `pwr_detail`, `psrc` (Grid 88, On-site + grid 10, On-site generation 3, not disclosed 1), `pw[]` (copies of power.json projects with keys `id,n,ty,ut,iso,mw,stage,risk,cod,dk,rw`; dk here is a list of arrays `[agency, no, status, date, url]`), `debt[] {id EX-..., inst, amt_m, px, pxd, g, rating}` (19 rows), `debt_m` (18), `h[]` holders/banks `[name, 0]` (amount 0 = not public), `s2 {imgs[[date, jpg, area_ha], ...], flag, txt, read, last_ha, d}` (third element is an area in ha; for stargate_abilene the last value 12.3 equals `last_ha`), `ep` (Epoch record, 94 rows: `src,name,lat,lon,city,state,country,addr,owner,owner_c,users,project,energy,build,investors,h100e,mw,capex_bn,chips{model:count},chips_plan,it_mw,bldg,water,status,status_d,plan_mw,plan_h100e,plan_d,next[],opcost_bn,tl[[date,mw,h100e]],calc,ep`), `ep_km` (distance Marden coord vs Epoch coord, 0-1.8 km), `xy` (projected map coords; 17 null).
Optional: `util`, `pw_src`, `pwr_detail2`, `pw_conf` (68 rows, from pw_found), `shared`, `docs[[title, text, url]]`, `sq[]` (21 covered rows; = sqft.json records).
Sample (real, row 0): `key:"stargate_abilene"`, `case:"ABILENE"`, `owner:"Oracle"`, `users:"OpenAI"`, `gross_mw:1200`, `deliv_mw:618`, `pw:[N-ABILENE-ONSITE]`, `debt:[{"id":"EX-ABILENE-000","inst":"Phase 1 construction loan","amt_m":2300},{"id":"EX-ABILENE-001","inst":"Phase 2 construction loan","amt_m":7100}]`, `debt_m:9400`, `ep.mw:421.0`, `ep.chips:{"B200":151200,"B300":100800}`, `ep.status_d:"2026-07-28"`, `ep.plan_mw:1180.0`.
Dates: Epoch `status_d` 2025-11-16 .. 2026-10-02; Epoch `tl` from 2024-05-31 to projected `plan_d` (e.g. 2026-11-01).
Joins: `key` -> site.json/sites.json/sqft.json/heat.json/night_raw/lst_raw/s2_epoch/pw_found/pw_missing; `case` -> dockets inventory, cat, ctr, rv, fac, comp; `pw[].id` -> power.json projects; `debt[].id` -> EX- instrument ids (hold.json, contagion, oa, rvc ...); `ep.name` -> Epoch CSV `Name`.
Data quality:
- `state` mixes full names and codes (`Texas` 14 + `TX` 5; `ND`, `LA`), includes non-US provinces (Johor, Inner Mongolia, ...), 2 empty, 1 null. 17 rows are outside the US (Epoch country: China 4, Malaysia 3, Norway 2, UK, Indonesia, Finland, Australia, Portugal, Iceland, Sweden, UAE) - V1 is US-only.
- `pw[]` links on Epoch-only rows are fuzzy and partly wrong: `ep:Bridge Data Centres MY06` (Malaysia) links 62 US power projects; `ep:EcoDataCenter 2` (Sweden) 59; `ep:Start Campus Sines` (Portugal) 23; `ep:OpenAI Stargate UAE` -> N-ABILENE-ONSITE, N-FRONTIER-SHACK, N-DTE-SALINE, P-097; `ep:CoreWeave Norway` -> N-GALAXY-HELIOS-BZ. Only the 21 covered rows' `pw[]` should be trusted.
- `pw[].dk` format differs from power.json (arrays vs objects).
- `px` on debt is null for most; prices differ by file and date (see section 5).
- Image files referenced in `s2.imgs` (e.g. `stargate_abilene_2025-08-28.jpg`) are **not in the corpus** (0 jpg/png files anywhere).

### 3.2 `site.json` (51,954) and `sites.json` (16,366) - financed campuses
`site.json`: `made` 2026-10-18T15:45:44Z, `sites[21]`, `method` (Sentinel-2 new-roof-area method), `src`, `reviewed` 2026-10-18.
Fields: `key`, `name`, `lat`, `lon`, `accuracy_note`, `coord_src` (Epoch 12, OSM 3, ArcGIS 2, other 4), `first_delivery` (text), `delivery_src`, `case`, `km` (crop window), `scenes[4] {win[start,end], date, id (S2 scene id), cc, cloud_px, roof_ha, new_roof_ha, img}` (scene dates 2025-06-02 .. 2026-10-17; 2 scenes have null date), `debt_m`, `ids` (EX- ids), `px`, `sched`, `orig`, `tenant`, `first_ha`, `last_ha`, `new_ha`, `d6`, `flag` (ok 15, slow 1, watch 1, null 4), `flag_txt` (on track 7, built / expanding 4, built 3, location approximate 3, behind schedule 1, shell up check fit-out 1, early works 1, power site 1), `read` (analyst read of imagery), `short`.
Sample (real): `key:"project_jupiter", case:"JUPITER", lat:31.8183, lon:-106.6653, debt_m:18000, ids:["EX-JUPITER-190"], px:85.0, first_ha:2.4, last_ha:21.9, flag:"slow", flag_txt:"behind schedule"`, scene `{"date":"2025-06-08","id":"S2A_13SCR_20250608_0_L2A","roof_ha":2.4,"img":"project_jupiter_2025-06-08.jpg"}`.
`sites.json` = same 21 keys with only the location/delivery fields (`key,name,lat,lon,accuracy_note,coord_src,first_delivery,delivery_src,case`, `km` on 8); order differs.
Keys: 20 cases + `xai_colossus2_southaven_power` (case null).
Seeds: **site/campus** (location, accuracy note, tenant, delivery dates as R facts), `SiteSchedule.imagery` (month, caption from `read`, `changeScore` from new_roof_ha), Building `firstVisible`/`changeScore` at campus level only.

### 3.3 `sqft.json` (6,412) - 22 records, 20 keys
Fields: `key`, `sqft_total` (9 null), `sqft_note`, `buildings` (9 null), `acres` (5 null), `src`, `src_kind` (company 6, permit 5, press 5, filing 5, county 1), `asof` (YYYY-MM, 2024-11 .. 2026-09; 2 null). Two records for stargate_abilene and vantage_frontier (different sources).
Sample: `{"key":"stargate_abilene","sqft_total":4000000,"sqft_note":"8 buildings; phase 1 was 2 buildings, 980,000 sq ft","buildings":8,"acres":800,"src":"https://crusoe.ai/newsroom/...","src_kind":"company","asof":"2025-03"}`.
Tag proxy: `src_kind` permit/filing/county -> F; company/press -> R. Seeds campus facts; `buildings.total`. Campus-level, not per building.

### 3.4 `s2_epoch.json` (42,959) - 81 Epoch-only campuses
`{ "ep:<name>": {key, lat, lon, km:3.0, scenes[2] {win, date, id, cc, cloud_px, roof_ha, new_roof_ha, img}} }`. Keys = the 81 `ep:` keys in dcmap. Sample scene: `{"date":"2025-09-10","id":"S2C_16SGC_20250910_0_L2A","roof_ha":35.4}` for `ep:Microsoft Fairwater Atlanta`. Seeds imagery rows (images missing).

### 3.5 Thermal: `heat.json`, `night/night_raw.json`, `lst/lst_raw.json`
- `heat.json` (23,747): `rows[21] {key, name, months[34] (2024-01..2026-10), dT[34], chg[34], now, mw[34], mw_now, mw_plan, n, status, eco}`, `corr:-0.03`, `eco:false`, `notes` (method; states heat does not measure load).
- `night_raw.json` (529,833): 21 sites `{key, name, lon, lat, scenes[] {d, s:"mod", core, bg, dT, bgsd}}`, 6,070 scenes 2023-01-01 .. 2026-10-08 (MODIS 8-day composites).
- `lst_raw.json` (13,305): 3 sites (stargate_abilene 76 scenes, project_jupiter 46, xai_colossus2 16) `{key, name, how, area_ha, scenes[] {d, sat:"8"|"9" (Landsat), core, ring, dT, n, dTc?}}`, 2025-01-02 .. 2026-10-05.
Spec says utilization from thermal is out of V1 scope and thermal gave "no usable signal"; these should not seed V1 screens (keep as research facts at most).

### 3.6 `pw_found.json` (34,546) and `pw_missing.json` (19,958)
- `pw_missing`: list of 69 Epoch-era campuses lacking power data: `{key, name, city, state, owner, users, addr, lat, lon, country}`. Some rows have shifted fields (e.g. `iren_childress`: `city` holds the full address, `addr` null; `ep:Meta Gallatin`: `city` = "United States").
- `pw_found`: `{key: {utility, iso, source, detail, mw, url, url2, confidence}}` for the same 69 keys. `iso`: PJM 14, ERCOT 11, none/WECC-bilateral 8, SPP 7, MISO 6, SERC-vertically integrated 6, NYISO 1, null 16; `source`: Grid 64, On-site + grid 4, On-site generation 1; `mw` null 40; `confidence` high 36 / medium 24 / low 9. Sample: `sb_energy_milam -> {"utility":"SB Energy (new self-built solar + storage); ERCOT grid utility not disclosed","iso":"ERCOT","source":"On-site + grid","mw":1200,"url":"https://epoch.ai/blog/openai-stargate-where-the-us-sites-stand","confidence":"medium"}`.
- These values are merged into dcmap rows as `util`, `pw_src`, `pwr_detail2`, `pw_conf` (37 rows identical).
Seeds `DataCenterRow.utility`, `iso`, `powerSource` (R-tag; source URLs are mostly press/Epoch).

### 3.7 `dock.json` (85,814) and `speed_press.json` (1,767)
- `dock.json`: `rows[90] {case, proj, name, key, kind, why, url, live, last[] {d,t,u}, next {d,what,src}, flags[], checked}` - UI copy of dockets/inventory (same 90 keys; `kind` humanized: Utility commission 29, County board 21, FERC 14, ...). `speed[5]` = the 5 flags with `seen` (monitor first-seen), `press {date,title,link,source,source_url}` (first news article found; null for 3 of 5), `press_checked`, `press_q`. `notes` (mechanic's-lien caveat: recorder liens not searchable).
- `speed_press.json`: `{proceeding_key: {checked, q, first {date,title,link,source,source_url}|null, n}}` for the same 5 proceedings.
Seeds: Evidence for docket flags; "filed vs press" lead time (backtest input).

### 3.8 `cat.json` (71,503) - catalyst calendar
`rows[49] {d (2026-10-19 .. 2028-08-15), dq (scheduled 27, contractual 16, expected 4, deadline 2), ev, evl, who, case (23 non-null), cat (Maturity 15, Construction 9, Permit 8, Sponsor 4, Financing 3, Earnings 3, Power 3, ...), lvl 1-3, sev (watch 32, material 13, severe 4), issue, why, down, up, debt[] {id, iss, inst, amt_m, px, pxd, g}, amt_m, px, src[], h[] [name, amount], hk (inst/issuer/bdc), ndebt?}`, plus `cov` (CoreWeave leverage covenant snapshot = cov_crwv.json + prev) and `notes` (date-label definitions).
Sample (real): `{"d":"2026-10-19","dq":"scheduled","ev":"TCEQ permit pause update","case":"VANTAGE_TX","cat":"Permit","sev":"material","issue":"Air permit for the VoltaGrid power plant unconfirmed under a state permit pause","debt":[{"id":"EX-VANTAGE_ORCL-366","amt_m":38000}],"px":97.68}`.
Seeds: **event** (future, with `dq` as certainty), Milestone/`nextCatalyst`, `RiskRow.why`. `src` URLs are mostly official (TCEQ, NMED, SEC).

---

## 4. `aiwc/q3x/epoch_dl/` - Epoch AI dataset (CC BY 4.0)

`x/data_centers.csv` (70,660; 94 rows, 16 cols): `Name` (unique; joins dcmap `ep.name` and `ep:<Name>` keys), `Current H100 equivalents`, `Current power (MW)`, `Current total capital cost (2025 USD billions)`, `Owner` (with confidence suffix, e.g. `Microsoft #confident`; 9 empty), `Users` (`OpenAI #likely, Microsoft #likely`), `Selected Sources` (markdown links), `Calculations sheet` (Google Sheets URL), `Project` (17), `Current chip types`, `All chip types`, `Investors` (10), `Construction companies` (6), `Energy companies` (19), `Country` (US 77), `Address` (81). **No lat/lon in the CSV** (coordinates are in map_props / dcmap). Sample: `Colossus 2, 1111672.56, 946 MW, 35.84 bn, SpaceXAI #confident, 5420 Tulane Rd, Memphis, TN 38109`.

`x/data_center_timelines.csv` (189,956; 560 rows): `Data center`, `Date` (2018-12-18 .. 2030-01-01; 95 rows after 2026-10-21 = projections), `Construction status` (text), `Buildings operational`, `IT power (MW)`, `Power (MW)`, `H100 equivalents`, `Performance (8-bit OP/s)`, `Total/Compute/Construction capital cost`, `Annual operating cost`, `Water use (MGD)` (92 non-null), `Current H100 equivalents (from Data center)`. All 94 DCs join.

`x/data_center_chip_quantities.csv` (85,646; 234 rows): `Handle`, `Date` (2021-01-01 .. 2029-01-01), `Chip type` (15 types), `Number of Units`, `Chip type source` / `Number of Units source` (`Estimate` | `Company disclosure`), `Owner`, `User`, `Notes`, `Data center` (84 DCs), `Timeline`. Tag proxy: `Company disclosure` -> R/F, `Estimate` -> third-party model.

`x/chip_types.csv` (7,334; 38): `Name`, `ML Hardware (linked)`, `Designer`, `Primarily for Chinese market`, `TDP (W)`, `16-bit FLOP/s`, `8-bit OP/s`, `Memory`, `Memory bandwidth`, `H100e`, `Cost per chip (approx.)` (25), `Release date` (29), `Unofficial estimated TDP (W)`, `Notes`. Useful for `kwPerGpu` / `GpuModelDetail.launch` defaults.

`x/data_center_chillers.csv` (34,665; 143) and `x/data_center_cooling_towers.csv` (85,832; 527): equipment catalogs (model, manufacturer, capacity kW, dimensions, fans, source link). Inputs to the "Cooling" capacity method (CapacityMethod), not facility records.

`map_props.json` (1,966,710): `{dataCenters[94], ownerColors, userColors, data-astro-cid-*}`. Each DC: `id` (= CSV Name), `timeline[] {date, notes, lowRes (Earth Engine tile URL), highRes[] (224 non-empty; cloudfront tiles.json), attribution[], imageryDate, h100e, power, cost, thumbnail, previewImage}` (3,604 entries), `facts {operational (74 true / 20 false), city, state, country, estimationMethods[], additionalInformation, sources, calculationsSheet, owner{value,confidence}, project{value,confidence}, users[{value,confidence}]}`, `geoJsonFile`, `img`, `lngLat`, `bounds`, `shapes` (GeoJSON FeatureCollection, 93 DCs, 1,300 features: Polygon 1,233 / LineString 67; `properties.type` = Cooling 392, Building 384, Backup generator 326, Power substation 184, Power generator 14; `complete` 0/1 on 1,098; `buildingNumber` on 419; `startDate`/`endDate` on 47).
This is the only per-building footprint source in the area: **Building.footprint, label (buildingNumber), status (complete), backupGenerators.count**, substations and on-site generator polygons.

---

## 5. Other loose files in `aiwc/q3x/` (finance-side; overlap with instruments/holders areas)

| File | Shape | Entity / key fields | IDs / joins | Notes |
|---|---|---|---|---|
| `fac.json` (394,564) | `rows[213]`, `tiles`, `method[4]`, `note` | Debt facility: `fid` (unique), `borrower`, `sponsor`, `name`, `type` (dc_abs 37, convert 36, gpu_loan 31, dc_construction_loan 29, dc_project_bond 21, corporate_loan 21, corporate_bond 19, gpu_lease 8, other 8, pref_equity 2, gpu_abs 1), `size_m`, `drawn_m`, `drawn_asof`, `date` (2023-05 .. 2026-10-16), `maturity`, `rate`, `price` (str, 44), `lenders[]`, `collateral`, `chips`, `chips_basis`, `gpu_count` (2), `site` (107), `it_mw` (44), `mw_basis`, `customer`, `contract`, `advance_rate`, `ltv`, `covenants[]`, `docs[] {t,url,kind}`, `notes`, `confidence` (high 125/medium 58/low 30), `status` (active 189, repaid 14, proposed 7, dup 3), `rating`, `big`, `case`, `gpu {disc,quote,src,basis,disc_per}`, short labels `*_s` | `fid`; `case` -> cases | `site` is text; only 44 have IT MW. Seeds instrument + collateral + GpuCollateral inputs |
| `fac/operators.json` 69, `hyperscale.json` 42, `miners.json` 58, `neocloud.json` 64 | lists of the same facility schema (no `case/status/big/*_s`; `ratings` in operators) | segment splits of fac.json | 226 unique `fid`; all 213 fac.json fids present; 13 extra fids only here | `price` is an object here with **inconsistent keys** (`{px,date,src,basis}` in operators vs `{value,date,source,kind}` in hyperscale) vs a string in fac.json |
| `comp.json` (161,870) | `rows[117]`, `seg[13]`, `tiles`, `note` | Deal comps: `fid`, `bor`, `spon`, `type`/`traw`, `close` (2023-07-30 .. 2026-10-15), `size`, `ten`, `mat`, `bench`, `bp` (spread at issue), `cpn`, `ipx`, `how`, `rate`, `tier` (ig 39, nd 28, lab 20, aa 13, orcl 9, back 5, div 3), `rt`, `coll`, `cov`, `ctr`, `lend[]`, `chips`, `now {px,d,k,s,sd,s1m}` (k = TRACE last sale 16, fund marks ...), `chg`, `case`, `status`, `docs[]`, `conf` | all 117 `fid` in fac.json | Seeds Price (TRACE/fund mark, R/F) and CurveTier inputs |
| `ctr.json` (58,970) | `rows[29]`, `tiles`, `notes` | Contract/lease per case: `id` (case key), `name`, `full`, `tenant`, `rating`, `guar`, `support[] {t,p,amt,c}`, `term`, `start`, `tcv_m`, `rent_m`, `rb`, `exit`, `outside`, `renew`, `quote`, `src[]`, `amt_m`, `px`, `pxd`, `cover`, `idle` (lease 23, gpu 4, eq 1, pool 1), `idle_t`, `nlease` | `id` = case (29 cases incl. QTS, SOUTHGATE, GLXY_H1, APLD_PF1) | Real row 0: `BEIGNET`, start `2029`, outside null, tcv_m 12310, amt_m 27294, cover 0.45. Seeds **contract/lease** (outside date, rent start, termination) |
| `hold.json` (3,010,150) | `fams[523]` with `pos[]` (17,024 positions), `inst{500}`, `grp{48}`, `segs`, `segtot`, `tot`, `exo`, `nport_rd` ["2026-05-29","2026-07-31"], `src`, `notes` | Holder positions: `k/id` (EX- id), `iss`, `g`, `inst`, `kind`, `mat`, `cusip`, `par`, `mv`, `who[]` (fund series), `rd` (report date), `src` (N-PORT 6,539, NAIC Sch. D 104, BDC 10-Q 36, Credit agreement/press 21, NAIC Pt 3 13), `url` (filing), `mark`, `px`, `pxd` | `inst` map keys mix EX- ids, raw CUSIPs (`92212KAJ5`), `NP-<grp>-<cusip>` and `GRP|name|kind` composites | `rd` formats mixed (`03/18/2026` vs ISO). Seeds **holder position** (F) |
| `ins.json` (51,840) | `grps[18]`, `fams[18]`, `rows[127]`, `buys[13]`, `src[5]`, `notes` | Insurer holdings 31 Dec 2025: `ent`, `fam`, `st`, `cusip` (dashed format `076912-AA-2`), `desc`, `naic`, `par`, `fv`, `bv`, `grp`, `g` | `g` = group key | CUSIP format differs from hold.json (`076912AA2`) |
| `oa.json` (69,069) | `rows[86]`, `buckets[6]`, `fams[40]`, `banks[5]`, `payer[7]`, `notin[7]`, `shares {ORCL:0.45, CRWV:0.22}`, `src` | OpenAI exposure per instrument: `id`, `grp`, `nm`, `inst`, `bucket`, `share`, `chain` (`OpenAI → Oracle lease → Vantage SPVs`), `amt_m`, `oa_m`, `px`, `pxd`, `p0`, `dmv`, `rating`, `mat`, `banks`, `holders[]` | EX- ids | Seeds obligation-edge shares (M) |
| `contagion.json` (76,985) | `ten {ORCL, CRWV}` each with `ins[]` (62 / 24), `fams[40]`, `sum`, `anchor_*`, `beta_med`, `rd_range` | Instrument beta/loss under tenant shock | EX- ids | ContagionResult inputs (M) |
| `dry.json` (69,221) / `forecast_q3_2026.json` (68,343) | `q`, `made`, `anchors{8}`, `pos[56]`, `funds[36]`, `method`, `sha256` | Q3 2026 fund-mark forecast: anchors (bond groups with 30 Jun / 30 Sep prices), fund positions with Q2 marks and forecast | EX- ids, fund CIK | Same `q/anchors/method`; `pos/funds/sha256/made` differ - two runs of one model; keep one |
| `rv.json` (24,995) | `rows[29]`, `groups[6]`, `curves {CRWV, ORCL}`, `band`, `asof` 2026-10-20 | Relative value per case: `id` (case), `px`, `pxd`, `pxk`, `y`, `spr`, `fair`, `vf`, `pl`, `rec`, `wal`, `call`, `why[]`, `risk[]`, `chg {t,d,e}` | case ids | Marden price/fair value (M) |
| `rvc.json` (51,459) | `rows[72]`, `asof` 2026-10-16, `m1` 2026-09-16 | Price change decomposition (rates vs credit) per bond, quarter and month windows | EX- ids (`grp` ORCL 53, CRWV 4, ...) | priceHistory points |
| `rec.json` (24,642) | `crwv {cases{low,base,high}, tranches, asm, src}`, `proj {rows, asm}`, `gpu[12]` | Recovery waterfalls: CoreWeave, project loans (`proj.rows` keyed by case: debt_m, cost_m, mw, pct built, rec [low,base,high], debt_per_mw), GPU loans | case keys, EX- ids | Recovery value (M) |
| `underwater.json` (28,772) | `loans[12]`, `tiles`, `asm` | GPU loan LTV: `ids`, `loan`, `chip`, `chip_basis`, `bal_m`, `mat`, `cost_m`, `coll_m`, `ltv`, `gap_m`, `ser[[t,bal,coll,coll_mod]]`, `units`, `src` | EX-CRWV-091..097, EX-XAI_VCI-388/389, EX-IREN-184/185; 2 Nscale loans have no id | GpuCollateral (M) |
| `trg.json` (92,858) / `triggers_raw.json` (159,262) | `rows[15]` triggers, `ratings[8]`, `terms[16]`, `notfound[6]` / 16 facility records with `terms[]` quotes, `verified_in_source`, `doc_local_path` | Covenant / rating triggers with verbatim quotes and SEC URLs | EX- ids | `triggers_raw` paths point to another machine's scratchpad (`/tmp/claude-0/-home-claude/...`, `gpuloans/raw/*.txt`) - files not in corpus. Seeds RatingTrigger, terms (F) |
| `cov_crwv.json` (804) | object | CoreWeave leverage covenant calc: asof 2026-06-30, filed 2026-08-12, 10-Q accession, debt_m 35551, lev_hi 6.8243 / lev_lo 5.8532 vs limit 6.0 | CoreWeave CIK 1769628 | Duplicated inside cat.json `cov` |
| `suppliers.json` (44,447) | `big[3]` (NVDA, AVGO, AMD), `rows[24]` | Supplier customer concentration: `tk`, `score`, `reason`, `top`, `pct`, `per`, `q`, `src`, `links[] {who,what,amt,filed,src,date}`, `fin[]` | tickers | `filed` bool = explicit F/R proxy |
| `docs.json` (93,937) | `docs[199]`, `n`, `dropped` 201, `tiles` | Curated SEC financing exhibits: `iss`, `cik`, `form`, `date`, `acc`, `ex`, `desc`, `kind` (Indenture & notes 68, Security & guarantee 43, Customer contract & lease 32, Amendment 31, Credit agreement 25), `url`, `title`, `party` (71 null), `dated` | all 199 URLs in docs/library.json | Seeds **document** (F) |
| `ust2023.csv` .. `ust2026.csv` | CSV `Date` (MM/DD/YYYY) + tenor columns | Treasury par yields; 2023/2024: 13 tenors; 2025/2026 add `1.5 Month` (2025-01-02 has it blank). 2026 runs 01/02/2026 .. 10/19/2026 | date | Benchmark for spreads (price v-method) |

Instrument id scheme: `EX-<GROUP>-<NNN>` (130 distinct across the q3x loose files; groups ORCL 56, CRWV 19, IREN 8, NBIS 8, GLXY 5, WULF 5, APLD 4, CIFR 4, XAI_VCI 3, HUT 3, ABILENE 2, FLUIDSTACK 2, one each JUPITER, BEIGNET, SOPAIPILLA, SALINE, VANTAGE_ORCL, STACK, DATABANK, CLOUDHQ, CYRUSONE, CORZ, ARK). `fac.json`/`comp.json` use `fid` (e.g. `VANTAGE_FRONTIER_TX_2025`) and do **not** carry EX- ids; the fid <-> EX- mapping is not in this area.
Price inconsistency to resolve: the same Vantage construction debt `EX-VANTAGE_ORCL-366` shows `px 97.68` (cat.json; PGIM mark 2026-07-31 per fac/operators) and `px 85.0, pxd 2026-10-12` (oa.json); Jupiter `EX-JUPITER-190` shows `px 85.0` (site.json) and `px 90.0, pxd 2026-09-18` (cat.json debt). Every price needs its own as-of and source.

---

## 6. `aiwc/q3x/docs/` - SEC document index

- `library.json` (154,511): `{made, n:400, docs[400] {iss, cik, form, date (2023-02-01 .. 2026-10-16), acc, ex, desc, kind (lease / capacity contract 84, amendment 78, indenture 69, credit agreement 58, equity / pref 57, security / guarantee 49, note purchase 5), url}}`. 17 issuers (Applied Digital 75, CoreWeave 47, Soluna 34, Hut 8 29, CleanSpark 28, Bitdeer 27, IREN 24, TeraWulf 23, Nebius 21, Cipher 20, Core Scientific 18, WhiteFiber 17, Bit Digital 15, Riot 11, Galaxy 6, HIVE 4, Valor Compute (xAI) 1). No duplicate URLs. `docs.json` (section 5) is a 199-row curated subset.
- `idx/*.json` (1,581 files, 6.3 MB): one per EDGAR accession (file name = accession no.); each a list of `[exhibit type, description, url]` (6,524 entries; 53 files empty). All 231 library accessions present. Pattern: `idx/0001144879-25-*.json` etc.
- `titles.json` (299,129): `{url: first-line title}` for 1,287 exhibit URLs (365 in library). Sample: `".../ex41-indenture.htm": "Trustee INDENTURE Dated as of September 22, 2026 2"`.
- `tickers.json` (799,264): SEC `company_tickers.json` (10,443 `{cik_str, ticker, title}`). Reference data for Party CIK/ticker.
Seeds: **document** (F), Party CIK/ticker. Not facility data.

---

## 7. `aiwc/q3x/fac/` (beyond the four segment lists)

- `pg/vwp.json` (27,280): **not JSON** - a Cloudflare "Attention Required" HTML block page (failed fetch). Discard.
- `dl/sub_<cik>.json` (15 files) + `crwv_sub.json`, `iren_sub.json`, `nbis_sub.json`, `shaz_sub.json`: SEC EDGAR submissions JSON (`cik, name, tickers, sic, filings.recent{accessionNumber[], filingDate[], form[], items[], primaryDocument[], ...}`) for CoreWeave, IREN (twice: `iren_sub.json` and `sub_1878848.json` are byte-identical), Nebius, SharonAI, TeraWulf, Applied Digital, Riot, MARA, Bit Digital, HIVE, Keel, Cipher, Core Scientific, Galaxy, Hut 8, WhiteFiber, Soluna, CleanSpark. Recent filings up to 2026-10-16.
- `dl/crwv_sub1.json` (24,599): older CoreWeave filings page (155, 2019-03-11 .. 2025-08-27), columnar.
- `dl/k8_extract.json` (433,920): 155 8-K Item 1.01/2.03 extracts `{cik, name, filed (2024-02-06 .. 2026-10-16), items, url, dor (date of report text), txt (<= 2,500 chars)}`; Applied Digital 36, TeraWulf 15, Cipher 15, ...
- `dl/exhibits_*.json` (5 files): `{deal code: [{type, desc, url}]}` exhibit lists per deal (e.g. `WULF2030`, `CIFRBP`, `APLDCC1`, `GLXYH2`, `CORZ31`, `HUTRB`, `FLASH`).
- `dl/nb/idx_*.json` (43 files): EDGAR directory listings `{directory:{item:[{last-modified,name,type,size}]}}` for Nebius 6-K accessions.
- `dl/wyfi.json` (18,186): EDGAR full-text search response (30 hits for "WhiteFiber" in 10-Q exhibits).
Seeds: document (F), party (CIK/SIC). No facility geometry or MW.

---

## 8. `aiwc/q3x/geo/`

- `pts.json` (3,222): 102 `[lon, lat]` = dcmap rows' coordinates in order (verified equal).
- `proj.json` (193,249): `{w:960, h:600, states[56] [name, SVG path], borders, xy[102]}` = identical to `dcmap.map` + `dcmap.rows[].xy` (verified).
- `us-atlas-3.0.1/` (ISC): TopoJSON `states-10m` (56 states), `counties-10m` (3,231 counties), `nation-10m`, Albers variants, `package.json`.
- `world-atlas-2.0.2/` (ISC): `countries-10m/50m/110m`, `land-10m/50m/110m`, `package.json`.
- `package.json`, `package-lock.json`, `node_modules/` (only 6 `package.json` manifests for d3-geo, d3-array, internmap, topojson-client, commander; no code).
Use: `counties-10m` can derive `county` for every site by point-in-polygon (county FIPS ids are geometry ids). Otherwise map layer only.

---

## 9. `aiwc/q3x/osm/` (32 files, 27 MB) - OpenStreetMap around sites

- 28 `*_map.json`: OSM API `/map` bbox downloads (`version, generator "openstreetmap-cgimap 2.1.0", copyright, attribution, license, bounds{minlat,minlon,maxlat,maxlon}, elements[]` with nodes (lat/lon) and ways/relations referencing node ids). Files: ab1/ab2 (Abernathy TX), cedarcreek, childress, childress2, colcity (Colorado City TX, Barber Lake), colossus2, denton, ellendale, elpaso, harwood2, helios, hyperion, jupiter, mariner (Lake Mariner), milam, portwash, rb (River Bend), robstown/robstown2 (Beacon Point), saline, shack (Shackelford), stfran/stfran2/stfran3 (St. Francisville LA). Content is mostly power infrastructure: e.g. ab2 4,507 `power=tower`, ellendale 1,427 poles + 1,400 towers; data-center-tagged elements are few (ellendale 10, colossus2 2, harwood2 1, helios 1, portwash 1 `construction=data_center`). No timestamp per file.
- 4 Overpass results (`abilene.json`, `childress.json`, `denton.json`, `harwood.json`): `{version, generator, osm3s{timestamp_osm_base}, elements[]}` with `center` points only (no polygons). `abilene.json` (OSM base 2026-07-15) has 40 `building=data_center` ways and 13 substations (e.g. `Mulberry Creek Substation`, AEP, 345/138 kV); `harwood.json` has `Applied Digital` node (6805 County Rd 81 N, Harwood ND).
- 3 files are **HTML 500 error pages, not JSON**: `cedarcreek.json`, `colcity.json`, `ellendale.json` (646 bytes each).
Seeds: Building footprints (needs node->way assembly for `_map` files), substation locations / voltages for power facts. Tag: OSM is a community map (R at best).

---

## 10. ID and join map

```
case key (ABILENE, JUPITER, VANTAGE_TX, BEIGNET, ...)  <- the site slug candidate
  dockets/inventory.rows.case_id (24)  -> proceeding_key -> dockets/out/<key>.json, seen.json, first_seen.key
  q3x/dcmap.rows.case (19) ; site.json/sites.json.case (20) ; cat.case (18) ; ctr.id (29) ; rv.id (29) ; fac.case / comp.case (27) ; rec.proj.rows.key
campus key (stargate_abilene, project_jupiter, ep:<Epoch name>)
  dcmap.key (102) = site/sites/sqft/heat/night_raw/lst_raw keys (21 covered) ; s2_epoch, pw_found, pw_missing keys (Epoch-only)
  dcmap.ep.name / "ep:<Name>" = epoch_dl CSV Name = map_props.dataCenters.id (94)
power project id (N-*, P-NNN)
  power.json.projects.id <- dcmap.rows.pw[].id ; power.feed.p
  power.projects.dk.no -> power.dockets.no -> dockets/out/<AGENCY>_<no>
party key (gx: BEIGNET, ORCL, ENTERGY_LA, ...)
  power.x[].key ; = EX- group code for 19 keys ; dcmap.debt[].g ; cat.debt[].g ; hold/ins `g`
instrument id EX-<GROUP>-<NNN>
  dcmap.debt.id, site.ids, cat.debt.id, hold, contagion, oa, rvc, dry/forecast, trg, underwater, rec
  fac.fid / comp.fid are a separate key space (no mapping here)
SEC accession / CIK
  docs.json & docs/library.acc -> docs/idx/<acc>.json ; fac/dl/sub_<cik>.json ; tickers.json
```
Mismatches: dcmap/site use `APLD_PF1`, inventory uses `APLD_PF1B4` (different instrument on the same campus); `XAI_VCI`, `QTS`, `GLXY_H1`, `SOUTHGATE` are in cat/ctr/rv/fac but excluded from the docket inventory (operating or non-US).

---

## 11. What this area should seed

| types.ts object | DB table | Source in this area |
|---|---|---|
| `DataCenterRow` / `DataCenterDetail` | campus, site | dcmap rows (id=key, name, lat/lon, state, owner/users/tenant, powerSource=psrc, utility/iso=pw_found or pw[0].ut/iso, plantId=pw[].id for covered rows, financed=case+debt_m), Epoch `ep` (facilityMw=ep.mw, itMw=ep.it_mw, onlineYear from tl), sqft (sqft/acres/buildings), site.json (coord accuracy, first_delivery, tenant) |
| `DataCenterSummary` | (computed) | dcmap + Epoch; `coverage` fill rates computable from nulls above |
| `Building` | building | epoch_dl/map_props `shapes` (type=Building polygons, buildingNumber, complete); OSM ways (after assembly). sqft/MW/permit/CO per building: none |
| `DataCenterDetail.backupGenerators.count` | fact | map_props features type=Backup generator (count per DC); MW and fuel absent |
| `DcRecord` / records tab, `SiteSchedule.documents` | document, event | dockets/out items + inventory (agency, kind); power.dk/ev; cat.src |
| `DcHistoryEvent` | event | Epoch timelines (power, buildings operational, by date); site.json scenes (roof area); no field-level before/after log |
| `Milestone`, `CushionProjection` inputs | event, fact | cat (scheduled/contractual dates), site.first_delivery/sched/orig, ctr.start/outside, inventory next_dates, power.cod/cod0 |
| `SiteSchedule.imagery` | document (imagery) | site.json scenes / s2_epoch / dcmap.s2 (dates, roof area, read text); image files missing |
| `PowerPlantRow` / `PowerPlantDetail` | plant | power.json projects with ty gas plant / fuel cell / data center campus power / SMR / nuclear (name, dev->operator, iso, st, mw->permittedMw, stage->status, dc->serves, debt text) |
| `AirPermit` | plant permit, event | dockets/out TCEQ_178644/182126/182467/182627 (status events, project_status), NMED_AQB_26_57 (hearing record), inventory air-permit rows |
| `PowerPlantSummary` | (computed) | power.json stage/ty/mw (after unit clean-up) |
| `largeLoad` (DataCenterDetail) | fact | power.json ty=large-load interconnection rows, feed (queue withdrawals/cancellations) |
| `Evidence`, `Alert`, `StreamEvent pipeline` | event | dockets first_seen/flags, dock.speed press lag, power.feed, power.sources flags |
| `ChainLink` / `ObligationEdge` (Power purchase, Lease) | party, obligation edge | power.x tuples (roles T/U/S/C/B), gx/gf; ctr (lease terms) |
| `LeaseRecord`, contract | contract/lease | ctr.json (29) |
| `ProjectFile.parties / construction.equipment` | party, fact | fac lenders/sponsor; cat; power.rw text (OEM mentions only) |
| Instruments, holders, prices (overlap) | instrument, holder position, price | fac/comp/hold/ins/rvc/rv/rec/underwater/trg (see other area notes) |
| `document` | document | docs.json, docs/library, docs/idx, docs/titles, fac/dl/* |

---

## 12. Gaps for the V1 screens (not in this area)

Data centers / buildings (spec 14-17)
- `county`: no field; only inside `loc` text for covered rows; derive via `geo/us-atlas counties-10m` point-in-polygon.
- `developer` vs `tenant` vs `endUser` as separate structured fields: only text (`owner`, `users`, `tenant`).
- `DcStatus` enum: absent; must map from Epoch `operational`, `status` text, site `flag_txt`, power stage.
- Per-building `sqft`, `facilityMw`, `itMw`, `buildingPermit`, `coDate`, `firstVisible`: absent (sqft/acres only per campus; TDLR mentions only in text).
- `ownerOfRecord`, `parcelIds`, `balancingAuthority`, interconnection `queuePosition`: absent as fields (queue serials appear in power project names, e.g. `ERCOT 33INR0005`).
- `firstSeen` and `lastChange` per campus: absent (only docket first_seen for 11 filings).
- Imagery files: referenced (`*.jpg`) but not shipped; Epoch `lowRes` Earth Engine URLs are external and may expire.
- Backup generator MW and fuel: absent (counts derivable from Epoch polygons).

Power plants (spec 16, 20)
- Plant lat/lon/county: absent for all 182 power projects.
- `PowerUnit` rows (OEM, model, count, rating each, fuel, online year, permit ref): absent; OEM appears only in prose.
- Air permit number/type/filed/decided as fields: absent; only TCEQ/NMED docket events and prose.
- `operator` separate from developer, `fuelSupply`, pipeline/`inService`, `gridMode` (Islanded/Grid-tied): absent (Green Chile pipeline only as project `N-GREENCHILE` + FERC CP26-80 items).
- `PlantDebt`: UCC liens, mortgages, equipment orders: absent in this area.
- `servedCampus.loadCoverPct`: absent.

Cross-cutting
- No explicit F/R/M tag or per-value `sourceUrl`/`asOf` on most fields; tags must be assigned by rule (official docket/filing URL -> F, news/company/Epoch -> R, derived -> M). Epoch estimates need a policy (third-party model, not Marden model).
- No mechanic's-lien data (dock.json notes say recorder liens are not searchable).
- Opposition: no structured opposition records (only court/zoning proceedings in inventory and prose).
- No fid <-> EX- instrument id mapping; no site slug other than case keys / campus keys (two key spaces to reconcile, e.g. `JUPITER` vs `project_jupiter`).
- Scripts named in inventory (`dockets/build_inventory.py`, `lib.py`, `registry.py`) and `triggers_raw` local paths are not in the corpus.

---

## 13. Data quality checklist (consolidated)

1. power.json `mw`: 474,000 aggregate queue row; 1.0 values meaning GW; 2026.0 = year; 65 nulls.
2. power.json `dk[].u` missing on 208 of 231 refs; several are news URLs.
3. dcmap `state` mixed full/abbrev; 17 non-US rows; fuzzy `pw[]` links on Epoch-only rows (62/59/23 links for Malaysia/Sweden/Portugal campuses).
4. dcmap `pw[].dk` format (arrays) differs from power.json (objects); `MTEP-50539` only in dcmap.
5. pw_missing fields shifted in some rows (address in `city`).
6. dockets/out: 31 of 73 proceedings unfetchable (0 items); TCEQ items share one URL; NMED item dated 2027-07-22 (likely 2026); LPSC/NDPSC/VASCC capped at 50 items.
7. Duplicates: `fac/dl/iren_sub.json` = `sub_1878848.json`; `sites.json` subset of `site.json`; `proj.json`/`pts.json` duplicate dcmap; `dock.json` duplicates inventory; `cov_crwv.json` duplicated in cat.json; `dry.json` vs `forecast_q3_2026.json` two runs of the same forecast.
8. Non-JSON payloads with `.json` names: `fac/pg/vwp.json` (Cloudflare block page), `osm/cedarcreek.json`, `osm/colcity.json`, `osm/ellendale.json` (HTTP 500 pages).
9. Inconsistent schemas: `fac*.price` (string vs two different object shapes); CUSIP formats (`076912AA2` vs `076912-AA-2`); date formats (`03/18/2026`, `2026-07-31`, `16 Feb 2023`, `2026.04.02 09:30 am`, `YYYY-MM`).
10. Prices for the same instrument differ across files with different as-of dates (Vantage 97.68 vs 85.0; Jupiter 85.0 vs 90.0).
11. Epoch timelines include projections up to 2030-01-01 (95 future rows) and chip quantities to 2029-01-01; must be tagged as projections.
