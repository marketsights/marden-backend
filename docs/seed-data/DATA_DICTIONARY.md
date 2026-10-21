# Marden seed data: data dictionary

Corpus: `marden_data_2026-10-20.zip`, extracted to
`/tmp/claude-0/-home-user/041c65f7-05f2-55e2-9ba6-73f6e02ba8df/scratchpad/data` (2,171 files). Paths below are relative to that root.
Inputs: the four profiler notes (`datamap/instruments.md`, `holdings-prices.md`, `facilities-power.md`, `cases-research.md`), `marden-backend/handoff/api/types.ts`, `specs/00a-product-spec.md`, plus specs 17 (data model), 18–20 and 22–25 (sources and collectors).
Re-checks for conflicting or vague points were run read-only with `python3 -I` (scripts in `datamap/scripts/dd/c1.py`–`c18.py`). Counts come from the profiler notes unless section 0.2 corrects them.

---

## 0. Before loading: canonical copies and corrections

### 0.1 Which copy to load (duplicates verified)

| Data | Copies in the corpus | Load | Do not load |
|---|---|---|---|
| Q3 analytic tables | `aiwcredit.json` → `q3x.*` and loose `aiwc/q3x/*.json` | Either. They are JSON-identical: 16 same-name blocks, plus `ctg` = `contagion.json`, `fc` = `forecast_q3_2026.json`, `sup` = `suppliers.json`, `uw` = `underwater.json` | `aiwc/q3x/dry.json`: an earlier run of the same forecast (made 2026-10-17T23:54Z, 36 funds vs 32) |
| Case memo | `aiwcredit.cases[].memo` and `aiwc/memo/<CASE>.json` | Either (identical in 29 of 29) | `aiwc/memo.bak_secfill/` (before the SEC-fill pass) and `aiwc/memo_prefix/` (older generation) |
| Model output per case | `aiwcredit.cases[].sim` and `aiwc/out/<CASE>.json` | `aiwc/out/<CASE>.json`. The scalar fields match in 28 of 28 | `aiwc/out/all.json` (earlier run; JUPITER p_loss 0.045 vs 0.2176) |
| Sensitivities | `aiwcredit.cases[].sens` and `aiwc/out/<CASE>.json.sens` | Undecided. They match in only 6 of 28 and use different `k` sets | — |
| Watch cards | `aiwcredit.watchplain`, `aiwc/memo/watch_plain.json`, `aidebt.watch` | `aidebt.watch` for the structured data, `watchplain` for the text. Same 25 ids in the same order; `watchplain` = `watch_plain.json` | — |
| Instruments | `aidebt.json`, `aiwc/aidebt.pretrace.json`, `aiwc/case_seeds.json → debt_rows` | `aidebt.json` | pretrace (the state before the TRACE refresh); `case_seeds.debt_rows` (stale copies) |
| Project tracker | `aiwc/aiw_proj.json` and HTML `aiwatch.proj` | Either (identical, 80 rows) | — |
| Case events | `aiwc/state/events.json` (601) ⊇ `aiwc/out/live.json` (246) ⊇ `aiwcredit.cases[].events` (125) | `aiwc/state/events.json` (verified superset) | `watch/*/aiwlive.json` (122 events in the latest copy; mislabeled `src`) |
| Power feed | `power.json.feed` (164) and `watch/out/db/power.json.feed` (150), same `asof` 2026-10-20T13:20:18 | `power.json` | — |
| Other duplicates | `sites.json` ⊂ `site.json`; `geo/pts.json` and `geo/proj.json` = dcmap; `dock.json` = `dockets/inventory.json`; `cov_crwv.json` is inside `cat.json`; `research/dl/sub.json` = `dl_b/sub_0001769628.json`; `fac/dl/iren_sub.json` = `sub_1878848.json`; `sector/sectors.json` is superseded by `sectors_final.json` | First of each pair | — |
| Not data | `aiwc/q3x/fac/pg/vwp.json` (Cloudflare block page); `aiwc/q3x/osm/cedarcreek.json`, `colcity.json`, `ellendale.json` (HTTP 500 pages); `aiwc/memo/check_*.json` (QA audit) | — | All of these |

### 0.2 Corrections to the profiler notes (re-verified)

1. **`q3x.fac` rows carrying `case`: 31** (27 distinct cases), not 12. The facility-to-instrument join therefore exists indirectly through the case id.
2. **`q3x/site.json` has 21 keys: 19 linked to a case and 2 with `case` null** (`sb_energy_milam`, `xai_colossus2_southaven_power`). It is not 20 cases plus 1.
3. **`q3x.ctr.outside` is filled in 3 of 29 rows**, not 2. Only IREN_CHILDRESS holds a date ("2027-04 (approx.…)"). CORZ_CW holds a rule and CRWV_DDTL4 is redacted.
4. **`q3x.hold` has 6,713 position rows in 523 fund families.** 17,024 is the value of its `npos` field (the raw count), not the number of rows shipped.
5. **`q3x.fc` has 32 funds** (= `forecast_q3_2026.json`). The 36 figure is `dry.json`.
6. **`rowinfo.c` has 29 distinct case ids** across 53 instruments, not 30. All 29 are case ids.
7. `aiwc/cases` risk evidence totals 236 items (not 235). `cases.lease[].term_yrs` is filled in 27 of 40 (not 26).
8. **JUPITER price.** The newest files (`aidebt.json`, `pxsrc/case_px.json`, `q3x.rv`) use 90.0 (dealer quote, 2026-09-18). The `case_px` note says the later ~84–85 October reports "credit no source and are not used". `aidebt.pretrace`, `params/JUPITER.json`, `out/JUPITER.json`, `cases.sim.price` and `site.json` use 85.0 (2026-10-12). So the model output was run on the rejected price, and "params carry the newest prices" is not true for JUPITER.
9. **NP id suffixes are not reliably CUSIPs.** For 129 of 136 NP ids with a 9-character suffix, the suffix equals `aiwcredit.ids`. Example of a mismatch: `NP-EQIX-29390XAD6` → `ids` = `294136363`.

---

## 1. Entity universe

The V1 targets are 50 instruments of $1B or more and about 25 sites (US only, 2024–2026 vintages). The corpus has a broader, unlocked universe.

| Entity | Count | File(s) | Notes |
|---|---|---|---|
| **Financed cases** (deal or site spine) | **29** | `aiwcredit.json → cases[]`; `aiwc/cases/` (29), `aiwc/memo/` (29), `aiwc/case_seeds.json` (29); `aiwc/params/` and `aiwc/out/` (28, no EDGE_CEDAR) | `kind`: building 20, gpu 5 (CRWV_DDTL3, CRWV_DDTL4, IREN_CHILDRESS, SOUTHGATE, XAI_VCI), pool 3 (CLOUDHQ, DATABANK, QTS), none 1 (EDGE_CEDAR). SOUTHGATE is in Melbourne, Australia (outside V1). CLOUDHQ, DATABANK and QTS are ABS or master-trust pools |
| **Financed sites with coordinates** | **21** campus rows (19 case-linked + 2 with no case) | `aiwc/q3x/site.json` (= `dcmap.rows` with `cov:true`) | 10 cases have no campus row: APLD_PF1B4 (Polaris Forge 1 by name), GLXY_H1 (Helios by name), CIFR_BLACKPEARL, CIFR_STINGRAY, CLOUDHQ, CRWV_DDTL3, CRWV_DDTL4, DATABANK, QTS, SOUTHGATE |
| **Campuses** (data-center map) | **102**: 21 covered + 81 Epoch-only; 85 US, 17 non-US | `aiwc/q3x/dcmap.json` | 64 Epoch-only rows are US (by Epoch `country`). 13 of the 21 covered rows have an Epoch record (`ep`) |
| Epoch AI data centers | 94 | `aiwc/q3x/epoch_dl/x/data_centers.csv`; `map_props.json`; timelines (560 rows), chip quantities (234) | 77 US by `Country`. Licence CC BY 4.0 |
| **Buildings** | No building table. **384 `Building` polygons** across 93 Epoch DCs, **53** of them on the covered campuses | `epoch_dl/map_props.json → dataCenters[].shapes` | Per covered campus: Abilene 8, Frontier 10, Lighthouse 3, Barn 2, Milam 1, Polaris Forge 1 3, Helios 1, Denton 8, Hyperion 9, Colossus 2 4, Barber Lake 1, Lake Mariner 3. Jupiter has 0 polygons, though `sqft.buildings` = 4. Campus-level counts: `aiwc/q3x/sqft.json` (22 records, 20 keys, `buildings` null in 9). OSM: `osm/abilene.json` has 40 `building=data_center` centre points (no polygons) |
| **Power projects / plants** | **182** (53 `N-*`, 129 `P-NNN`) | `power.json → projects[]`; `aiwc/feedx/proj_x.json` (`raw` = the same 182) | `ty`: large-load interconnection 101, gas plant 33, data center campus power 23, SMR 7, solar+storage 6, co-location 5, fuel cell 3, nuclear restart 3, transmission 1. Linked to cases: 24 plant ids → 16 cases (`aiwcredit.pw2c`) |
| **Instruments** | **707** master rows | `aidebt.json → rows` (+ `aiwcredit.rowinfo`, same 707 keys) | Prefixes: EX 391, NP 152, FWP 81, MAN 26, BDC 25, XB 23, 8K 9. `cat`: corp 310, abs 159, util 89, proj 87, bdc 62. `status`: outstanding 701, proposed 5, repaid 1. 176 rows have `amt_m` ≥ 1000 (units mixed; see §5) |
| Instrument lists that are not the master | — | `aidebt.watch` 25; `pxsrc/targets.json` 46 instruments + 30 case→instrument rows (26 cases); `memo.instruments` 78 (52 with `debt_row`, 51 unique); `aiwc/cases debt[]` 46; `rowinfo.c` 53 (the case's own debt); HTML `aichain.issuers[].caps` 296 (no ids) | Use these to pick the V1 50. None of them is a locked list |
| Debt facilities | **213** `fid` (189 active, 14 repaid, 7 proposed, 3 dup); 226 unique fids including the segment files | `aiwc/q3x/fac.json`; `fac/{operators,hyperscale,miners,neocloud}.json` | Separate key space. 31 rows carry `case` |
| Deal comps | 117 | `aiwc/q3x/comp.json` | Every `fid` is in `fac` |
| **Issuers / parties** | 245 distinct `issuer` strings; **79** `grp` codes (29 named in `aidebt.gn`) | `aidebt.json` | Other party lists: `power.gx` 44 keys (`gf` gives a function for 16); HTML `aichain.tenants` 16 and `aichain.issuers` 26 sheets; `memo.chain` 288 rows / 212 distinct names; `gpu/operators.json` 12; `aiwcredit.acct` 30 issuers; CIK-identified borrowers: `q3x/docs.json` 16, `q3x/docs/library.json` 17; SEC ticker map `q3x/docs/tickers.json` (10,443) |
| **Holders / funds** | BDCs: 180 in the universe, 178 with a fund index `fi` and `fundpos` key (1 with no rows). N-PORT series (by name): 947 in `w2` rows, 756 in `agg_*`. Fund families: 523 (`q3x.hold`). Insurer families: 18 (`q3x.ins`) | `watch/ref_meta.json`; `fundpos.json`; HTML `data.funds` (178); `aiwc/pxsrc/w2/**`; `aiwc/pxsrc/agg_*.json`; `aiwc/q3x/hold.json`; `aiwc/q3x/ins.json`; `sector/header_fund_agree.csv` (112) | HTML `data.summary`: 98 managers, `nport_funds` 1,964, `nport_borrowers` 2,354 |
| **Positions** | BDC: 26,491 (`fundpos`, a subset of the HTML's 56,756). N-PORT per series: 3,810 (`w2`, 962 filings) + 1,213 (`agg_*`, 776 filings). Family-aggregated: 6,713 (`q3x.hold`; 4,675 carry an instrument id, 125 distinct). Insurer: 127 (`q3x.ins`). Chain sheet holders: 328 (`aichain`) | as listed | Model forecasts, not positions: `watch/calls_r1.json` 4,175 rows; `q3x.fc.pos` 56 rows |
| **Leases / contracts** | 29 (one per case) | `aiwc/q3x/ctr.json` | Also `aiwc/cases lease[]` 40; `aichain.issuers[].cps` 110; `aiw_proj.json` 80 project rows (counterparty, term, RFS dates); `decks/iren_apld_facts.json` (IREN–Microsoft contract facts) |
| **Obligation edges** | 85 tenant/customer/guarantor edges | HTML `aichain.edges` (tenant 44, customer 30, end user 5, guarantor 5, credit support 1) | Also: `aidebt.aiwx` 80 keys / 187 tuples (G 129, L 25, P 23, B 10); `memo.chain` 288 role rows; `rowinfo.x` 843 links on 226 instruments (Tenant 600, Borrower 157, Developer 40, Sponsor 38, Site owner 5, Power plant 3); `aiwc/cases credit_support[]` 42; `power.projects[].x` 231 tuples; `q3x.oa` 86 OpenAI-share rows (M) |
| **Events** | see the right column | — | `aiwc/state/events.json` 601 (case news, docket and court items, 2026-09-26..10-20); `dockets/out/*.json` 5,801 items in 73 proceedings; `power.json`: `feed` 164, `projects[].ev` 269, `dk` 231, `dockets[].lf` 245; `aiwc/feedx`: `feed_x` 142, `docket_x` 245; `q3x.cat` 49 future catalysts; `memo.issues` 160 (23 with `next.date`); `aiw_proj.changes` 97; `aiwc/cases` risk evidence 236; `aiwcredit.cases.drivers` evidence 208; HTML `data.alerts` 95 (issuer 8-K/10-Q); `q3snap` alerts: 3 live + 887 replay; `watch/tells_db.json` 23; `bevents.json` 70 items over 31 bkeys; `dockets/inventory.json` 173 latest / 17 next dates / 5 flags |
| **Documents** | 12,609 distinct URLs (excluding geo, OSM, `map_props`, `docs/idx`, `tickers`, `state/pages`, `state/news`, `dockets/seen`, `trace/eod`); 4,093 on www.sec.gov | everywhere | Curated SEC exhibits: `q3x/docs.json` 199 ⊂ `docs/library.json` 400; `docs/idx/` 1,581 accession files (6,524 exhibit entries); `docs/titles.json` 1,287. `fac/dl/k8_extract.json` 155 8-K extracts. N-PORT filings: 962 (`w2`) + 776 (`agg`). Deck facts: 376 from 7 PDFs (the PDFs are not in the zip and have no URL). Image files referenced by `s2.imgs`/`scenes.img`: 0 in the corpus |
| Prices | see notes | — | TRACE: `trace/eod.json` 207 CUSIPs / 26,700 daily rows (2026-01-02..10-19); `trace_rows.json` 169 instruments; `spreads.json` 121. Current price records: `aiwcredit.pxs` 191 (TRACE 169, fund mark 9, none 7, BDC mark 5, dealer quote 1). `pxsrc/out1`+`out2`: 46 instruments + 30 case keys. Börse Frankfurt `bondq`: 163 ISINs, 3 snapshots. Model: `aiwc/out` 28 cases |

### 1.1 Spec-named V1 examples → ids in the data

| Spec example | Case | Instrument id(s) | Gap |
|---|---|---|---|
| Jupiter | JUPITER | `EX-JUPITER-190` | — |
| Frontier | VANTAGE_TX | `EX-VANTAGE_ORCL-366` ("Construction debt (TX + WI)", amt_m 38000); `EX-VANTAGE_ORCL-367` (PGIM-held slice, amt_m 16) | The modeled row is the $16m slice |
| Lighthouse | VANTAGE_WI | `EX-VANTAGE_ORCL-366`; `EX-VANTAGE_ORCL-368` (PGIM-held slice, amt_m 10) | Same as Frontier |
| Abilene | ABILENE | `EX-ABILENE-000` (Phase 1, 2300), `EX-ABILENE-001` (Phase 2, 7100) | No public price |
| The Barn | SALINE | `EX-SALINE-308` | — |
| Meta Hyperion (Beignet) | BEIGNET | `EX-BEIGNET-022` | — |
| Anthropic Big Sky | — | none in `aidebt` | Only `q3x.fac` `ANTH_COMPUTE_SPV_35B` (gpu_lease, 35000, case null) |
| Anthropic North Star | — | none | "North Star" appears only as unrelated BDC borrowers (`bcpe north star`, `north star acquisitionco`) |
| CoreWeave DDTL 1.0–5.0 | CRWV_DDTL3, CRWV_DDTL4 | `EX-CRWV-091` (1.0), `-092` (2.0), `-093` (2.1), `-094` (3.0), `-095` (4.0), `-096` (5.0), `-097` (5.5) | — |
| IREN | IREN_CHILDRESS | `EX-IREN-184` (DDTL), `EX-IREN-185` (USPP); converts `-178`..`-183` | — |
| Nebius | — | `EX-NBIS-196`..`-204` | No case |
| Nscale | — | `EX-NSCALE-205`..`-215`, `BDC-NSCALE-Nscale` | No case; mostly no amt/price |
| CoreWeave 2031s and 2032s | — | `EX-CRWV-102` (9.00% 2031), `-103` (9.75% 2031), `-104` (9.625% 2032), `-105` (8.50% EUR 2032), `-106`/`-107` (converts 2031/2032) | — |
| Vantage sub loan | — | `EX-VANTAGE-361` ("Retained Vantage HoldCo sr sub loan", 662, 2031-12) | The BDC side is keyed by bkey `retained vantage` in `fundpos` |
| Tenant credits | — | ORCL (63 rows), CRWV (23) | OpenAI, Anthropic, Microsoft: no instruments; present only as `aichain.tenants` ids |

---

## 2. Canonical IDs and join keys

| Key | Form / example | Canonical source | Joins (verified unless noted) | Problems |
|---|---|---|---|---|
| **Case id** | `ABILENE`, `VANTAGE_TX`, `CRWV_DDTL3` (29) | `aiwcredit.cases[].id` | `rowinfo.c` (53 instruments) and `rowinfo.x`; `q3x.site/dcmap.case` (19); `q3x.ctr.id`, `q3x.rv.id` (29); `q3x.fac.case` (31 rows) and `comp.case`; `q3x.cat.case` (18 distinct); `dockets/inventory.case_id` (24); `pw2c` values; `gpu.pwp[].id` / `site_power` (26); `pxsrc/case_px` (26); `targets.project_modeled` (26); `"CASE\|…"` prefixes in `aiwc/state/*` | Not the site slug the app expects (see the site slug row). One case can mean several sites (CORZ_CW 5 counties; QTS, DATABANK, CLOUDHQ pools; CRWV_DDTL4 "four US sites"). Two cases can share one campus (APLD_PF1/APLD_PF1B4; GLXY_H1/GLXY_H2). The docket inventory is filed under APLD_PF1B4 and GLXY_H2, the campus maps under APLD_PF1 and GLXY_H2 |
| **Site slug** (`SiteSummary.slug`) | fixtures: `abilene`, `frontier`, `lighthouse`, `the-barn`, `jupiter`, `childress`, `milam`, `barstow`, `vineland` | **none in the data** | — | Needs a curated case id → slug map (VANTAGE_TX → `frontier`, SALINE → `the-barn` cannot be derived). `barstow` appears nowhere in the corpus. `vineland` appears in 12 files (e.g. the Epoch CSV, `pw_found`, `collmeta`, `aidebt` text) but is not a case. `milam` is site key `sb_energy_milam`, with no case |
| **Campus key** (`DataCenterRow.id`) | `stargate_abilene`, `ep:<Epoch Name>` (102) | `q3x/dcmap.rows[].key` | `site.json`, `sqft.json`, `heat.json`, `night_raw`, `lst_raw`, `s2_epoch`, `pw_found`, `pw_missing`; `ep.name` = Epoch CSV `Name` = `map_props.dataCenters[].id` | Fixtures use kebab case (`stargate-abilene`, `vantage-frontier`). The `ep:` keys contain spaces |
| **Instrument id** | `EX-<GRP>-<nnn>`, `NP-<GRP>-<CUSIP or code>`, `FWP-<GRP>-<cpn>-<yr>`, `MAN-<GRP>-<slug>`, `BDC-<GRP>-<name>`, `XB-<GRP>-<member>`, `8K-<GRP>-<cpn>-<yr>` | `aidebt.rows[][0]` (707, unique) | `rowinfo`, `pxs`, `ids`, `watch`, `pxsrc` (48), `memo.instruments.debt_row`, `case_seeds.debt_rows`, `targets`, `out1`/`out2`, `trace/*`, `watch/bondq_list`, `q3x` hold/oa/ctg/rvc/trg/uw/rec/cat/dcmap/site | Fixture ids (`jupiter-loan`, `crwv-9-2031`, `anthropic-junior`, `nscale-facilities`, `oracle-notes`, `barn-bonds`, `frontier-loan`, `lighthouse-loan`) match nothing. `anthropic-junior` has no instrument. `dcmap.debt` has 4 entries with `id` null although instruments exist (galaxy_helios 9.875% notes → `EX-GLXY-163`; iren_childress DDTL and USPP → `EX-IREN-184`/`-185`, with different amounts; edgeconnex_cedar_creek → `MAN-EDGECONNEX-Proposedconstructiondebt`). `dcmap`/`site.json` link vantage_lighthouse to `-366`, while `rowinfo.c` gives VANTAGE_WI `-368` |
| **Group** `grp` | `ORCL`, `CRWV`, `VANTAGE_ORCL` (79) | `aidebt.rows.grp`; names in `aidebt.gn` (29) | `memo.chain.grp` (108 of 288); `collmeta` key prefixes (26); `aiwx` G tuples; `q3x.hold.g` / `ins.g`; `dcmap.debt.g`; 19 of 44 `power.gx` keys | `power.gx` mixes groups with utility and party keys. 50 groups have no display name |
| **Project key** `"Issuer\|Project"` | `"Oracle\|Project Jupiter (Stargate New Mexico)"` (80) | `aidebt.aiwx` keys = `aiw_proj.json` issuer+project | `cases.aiw_idx` / `case_seeds.aiw_idx` hold integer positions into this order | Positional join. GLXY_H1 and GLXY_H2 both carry index 0 |
| **Facility id** `fid` | `JUPITER_RED_CHILES`, `CRWV_DDTL4` (213; 226 with the segment files) | `q3x/fac.json` | `comp.fid` (117/117); `fac.case` (31 rows) | No fid ↔ instrument id map. `fac` sizes are commitments where `aidebt.amt_m` is often drawn or holder par |
| **CUSIP** | `21873SAG3`; RegS `U2069EAB6`; NAIC dashed `076912-AA-2`, `74751*AK2` | `aiwcredit.ids` (300 ids → 1 CUSIP); `trace/match.json` (170 ids → 267 CUSIPs) | `ids` and `match` agree on all 169 shared ids (`ids` has 131 more; `match` has 1 more). TRACE files are keyed by CUSIP; `agg_<CUSIP>.json`; `w2.cusip`; `q3x.hold.inst` keys (161 bare CUSIPs, 75 dashed/`*`, 139 `GRP\|name\|kind` composites, 97 EX-style, 28 NP) | One instrument has 1–4 CUSIPs (144A, RegS, tranches); the spec's single `CUSIP` field cannot hold them. 2 collisions: `294136363` → `NP-EQIX-29390XAD6` and `NP-EQIX-31038551`; `629377CP5` → `NP-NRG-629377CP5` and `XB-NRG-A2450SeniorSecuredNotesDue2027Member`. Loans carry `000000000` / `999999999` / `N/A` |
| **ISIN** | `US21873SAD09` | `watch/bondq_list.json` (163; `XS2941363637` listed twice) | `US` ISIN body = `ids` CUSIP for 144 of 150 | 6 mismatches where the ISIN is another line of the same bond (e.g. `EX-CRWV-102`: ISIN `USU2069EAB66` vs `ids` `21873SAC2`; `EX-NBIS-198`/`-199`; `NP-MARA-565788AJ5`). 13 non-US ISINs |
| **LoanX / other** | `LX276526`, `LX284984`, `LX289647` | inside `fac.name`, `aidebt.inst` text, `w2.other` (769 rows) | — | No field. The DDTL and loan holders in N-PORT can be joined only through this code |
| **BDC CIK / fund index** | CIK `"1287750"`; `fi` 0–177 | `watch/ref_meta.json → universe[180]`, `fund_idx{178}` | `fundpos.f` keys; `q3snap` `fi`/`cik`; HTML `data.funds[fi]` (url, filed), `realnav.nav[fi]`, `q3cal.f`, `marklag` | Universe CIKs 2077250 and 2107057 have no `fi` and no positions. CIK 1495584 has 0 rows |
| **Issuer CIK** | CoreWeave 1769628, IREN 1878848, Hut 8 1964789, APLD 1144879, CIFR 1819989, WULF 1083301, GLXY 1859392, CORZ 1839341, NBIS 1513845 | `q3x/docs.json` (16 issuers), `docs/library.json` (17), `gpu/operators.json`, `fac/dl/sub_<cik>.json` | Filing URLs | No issuer CIK on `aidebt` rows. The link to CIK is by name or `grp` |
| **N-PORT fund series** | name only: `"Aristotle Pacific EXclusive Series (I)"`; filer `"Aristotle Funds Series Trust (CIK 0001959372)"` | `w2.fund` / `w2.filer`; `agg.fund` / `agg.reg`; `q3x.hold.pos.who[]` | Filing URL (`primary_doc.xml`), `adsh` | **No SEC series id (S0000…) anywhere in the corpus.** The registrant CIK sits inside the `filer` string. `q3x.hold` sums series into families (`who[]` lists names), so per-series Holder rows must come from `w2`/`agg` |
| **Borrower key** `bkey` | `coreweave financing`, `retained vantage` | `fundpos` col 0; HTML `data.facilities[20]`/`borrowers[14]`; `sector/sectors_final.json`; `niche.b`; `bevents.ev`; `aiwx` B tuples | BDC facility key `bkey\|lien\|spread\|YYYY-MM` (in `opps`, `ew`, `susp`) | No join to instrument ids. Name collisions (`vantage bidco`, `oracle vision bidco`, `meta buyer`, `galaxy universal` are not the AI names). 27 unresolved bkeys (`_unres`) |
| **Power project id** | `N-JUPITER-MICROGRID`, `P-097` (182) | `power.json.projects[].id` | `case_seeds.power_ids` (case → ids); `aiwcredit.pw2c` (24 id → 1 case); `dcmap.pw[].id`; `power.feed[].p` (32); `proj_x.feeds` | `pw2c` is one case per plant, `case_seeds` is many: `P-004` → APLD_PF1B4 in `pw2c` but both APLD cases in `case_seeds`; `N-GALAXY-HELIOS-BZ`/`P-104` → GLXY_H2 vs both GLXY cases. `dcmap` adds `P-005` to apld_polaris_forge_2 and `MTEP-50539` (in no other file) to meta_hyperion. `pw[]` on Epoch-only rows is fuzzy and wrong (a Malaysian campus links 62 US projects) |
| **Docket / proceeding** | `"FERC\|EL25-49"`; proceeding key `FERC_ER26-3265`, `ABILENE_CC` | `power.json.dockets` (56); `aiwcredit.dockx` keys (56) = `feedx/docket_x.docket` set (56); `dockets/inventory.rows[].proceeding_key` (73 unique in 90 rows) | Same 56 dockets in all three. For the 6 with `no` null, `dockx`/`docket_x` put the title in the key (`"LPSC\|Entergy Louisiana – 7 additional gas plants…"`), so join those on agency + title. Inventory key = `dockets/out/<key>.json` (73 = 73); 8 `power.dk.no` values match a `dockets/out` file | Two key spaces (`AGENCY\|no` vs `AGENCY_no` or local names). Agency names are not normalized (`PUCT`, `PUCT/SOAH`, `PUCT - Texas Energy Fund`) |
| **Chain sheet / cap** | `ABILENE:0+1`, `XAI_VCI:11` | HTML `aichain.issuers[sheet].caps[i]`; `aicov.dc/gpu[].id` | — | Caps carry no instrument id or CUSIP; they join by name, coupon and maturity only |
| **SEC accession** | `0001213900-26-107187` | `docs.json`/`library.json` `acc`; `w2.adsh`; `watch/state.json` | `docs/idx/<acc>.json` (all 231 library accessions present) | — |
| **Epoch name** | `OpenAI Stargate Abilene` | `epoch_dl` CSV `Name` | `dcmap.ep.name`; `"ep:<Name>"` keys | Owner/users carry confidence suffixes (`#confident`, `#likely`) |

---

## 3. Per target table

Table names: `dc_campus`, `dc_building`, `power_plant`, `power_unit`, `plant_serves`, `site_link`, `entity` and `fact` come from spec 17. Site, Contract, Instrument, Holder, Event and Price are product-spec objects; obligation edge is spec 18, and document is the "source document" of the spec 17 `fact` table. The handoff gives no column list for the product-spec and spec-18 tables, so their target fields below use the product-spec "Key fields" and `types.ts` names.

**Tag and source rules that apply to every table.** No file has an F/R/M field, a `sourceName` or a `methodVersion`. Tags must be derived:
- **F**: sec.gov filings (8-K/10-K/10-Q exhibits, N-PORT `primary_doc.xml`, BDC 10-Q), elibrary.ferc.gov, state commission and permit portals (interchange.puc.texas.gov, tceq, env.nm.gov, apps.psc.wi.gov, lpsc, psc.nd.gov, scc.virginia.gov, dps.ny.gov), county portals (civicclerk, granicus), FINRA TRACE, `basis: "disclosed"`. Equity prices are F per spec 22.
- **R**: news hosts, Google News redirect URLs, `basis: "reported"`, press dealer quotes. Spec 22 tags dealer quotes, ratings and CDS as R.
- **M**: `aiwc/out`, `params`, `q3x` fc/rec/uw/ctg/oa/rv, `calls_r1`, `livenav`, `rowinfo.s/p` (and `y/sp` where `ym=1`), `kv` "(model input)", decks `derived:true`.
- **Undecided**: (a) `basis` presumed/inferred/assumed. `types.ts` `Tag` has no "inferred" value; only `LeaseRecord.attribution` has `'Inferred'`. (b) Epoch AI estimates (a third-party model). (c) Börse Frankfurt fixings. (d) investor-deck facts. (e) OSM.
- **sourceName** has to be derived from the host. The `types.ts` examples are "FERC eLibrary", "N-PORT", "Reuters".
- **asOf**: use the record date where one exists, otherwise the file `asof`/`made`. File-level as-of dates: `aiwcredit` 2026-10-20 15:01 UTC; `power.json` 2026-10-20T13:20:18; `aiwc/cases` and `dockets/inventory` 2026-10-17; `aiwc/out/live.json` 2026-10-20T17:23Z; `q3snap` 2026-10-16T02:27:35Z; `aichain`/`aicov` 2026-10-14; `aiwatch.eq` 2026-10-15; `bevents`, `niche` 2026-10-16; `gpu/*` 2026-10-17; `q3x.rv` 2026-10-20; `q3x.rvc` 2026-10-16; TRACE data through 2026-10-19. `fundpos.json` and HTML `data` are the 2026-06-30 BDC schedules (`data.summary.asof`). `aidebt.json` and `collmeta.json` have no as-of.
- **fact rows**: spec 17 `fact` needs (value, tag, source document, quote, valid_from, learned_at). Quotes exist only in `aiwc/cases` (`quote` on debt, collateral, capacity, lease, credit_support, plus risk evidence `q`), `memo.security[].q` (32), `q3x.ctr.quote` (29), `q3x.trg` / `triggers_raw` terms, deck facts `q`, `aiwatch.proj.changes[].quote`, `power.ev[].q` (headline). For `learned_at`, use the zip date 2026-10-20 or the file `made`/`seen` time; `state/events.seen` holds real first-seen times.

### 3.1 Site (financed site; product-spec Site; `SiteSummary`/`SiteDetail`)
- **Rows:** 29 cases. V1 is US-only, so SOUTHGATE is excluded.
- **Key:** case id. The slug comes from a curated map (§2).

| Target field | Source |
|---|---|
| name, place | `aiwc/memo/titles.json[CASE]` = [name, place]; also `aiwcredit.cases[].name`/`place` |
| county, state | `aiwc/cases/<CASE>.site.county` (27/29), `.state` (28/29). Free text and multi-valued for 5 cases ("TX, GA, NC, OK", "VA/MN", "multi", "TN/MS", "VIC/TAS (Australia)"). Fallback: point-in-polygon of `site.json` lat/lon on `q3x/geo/us-atlas-3.0.1/counties-10m` |
| location (lat/lon) | `q3x/site.json` (19 case-linked keys): `lat`, `lon`, `accuracy_note`, `coord_src` (Epoch 12, OSM 3, ArcGIS 2, other 4) |
| MW (IT, power) | `aiwc/cases.capacity`: `critical_it_mw` 19/29, `gross_mw` 16/29, `delivered_mw` 23/29; `gpu/site_power.json.projects[CASE].it_mw` (26); `dcmap` `gross_mw`/`it_mw_plan`/`deliv_mw` |
| power source and status | `gpu/site_power.json` `site_power.{iso, utility, supply, onsite_generation}` (`iso` is free text, e.g. "ERCOT (presumed)"); `dcmap.psrc`, `pwr_status`, `pwr_detail` |
| interconnection queue ID | no field. It appears only in `power.projects.n` text (e.g. `ERCOT 33INR0005`) and in `feed_x.id` for queue items |
| permits | `aiwc/cases.watch[]` (`kind` state_env_permit 21, zoning 3, …; `id` 71/186); `dockets/inventory` rows with kind `state_air_permit` (7) |
| construction stage | `cases.capacity.pct_complete` (9/29); `site.json` `flag`/`flag_txt`/`read`; `aiw_proj.status` |
| expected energization date | `cases.capacity.scheduled_completion` / `original_completion` (free text); `site.json` `first_delivery`, `sched`, `orig`; `aiw_proj` `rfs_latest`/`rfs_prior` (61/58 dated) |
| endUser / tenant / developer | `memo/shortnames.json` = [sponsor short, payer short]; `memo.chain` roles End user / Tenant / Developer / Sponsor |
| debtB, leadLenders | `aiwcredit.cases[].amt_m` (Abilene's 9400 merges two loans); `cases[].holders.banks` |
| facts line | `memo.headline` |

- **Source URL, as-of, tag:** each `aiwc/cases` block has `src[]` and `quote`. Tag by URL host. `inferred: true` appears on some cost, lease, site, capacity and credit_support blocks. The as-of is file-level (2026-10-17). Site coordinates come from `site.json` (reviewed 2026-10-18) and are R or third-party by `coord_src`.

### 3.2 `dc_campus` (spec 17; `DataCenterRow`)
- **Rows:** `q3x/dcmap.rows` (102). V1 drops the 17 non-US rows.
- **Key:** `id` ← `key`.

| Target field | Source |
|---|---|
| `name` | `name` |
| `aliases[]` | `ep.name` for the 13 covered rows that have one; nothing else |
| `developer_entity_id` | `owner` (text) → entity resolution |
| `owner_entity_id` | none (assessor gap) |
| `county` | covered rows: inside `loc` text; otherwise derive from `us-atlas` counties |
| `state` | `state` (mixed: `Texas` 14 / `TX` 5; non-US provinces) |
| `centroid` | `lat`/`lon` |
| `iso` | `pw_found[key].iso` (69 Epoch-era rows) or covered `pw[0].iso` |
| `balancing_authority` | none |
| `utility` | `pw_found.utility` / `util` |
| `power_source` | `psrc`: Grid 88, On-site + grid 10, On-site generation 3, not disclosed 1. It must be mapped to `PowerSource` (Grid / On-site gas / Hybrid / Unknown); "On-site generation" does not state the fuel |
| `first_seen_at` | none |

- **Extra facts:** `ep.mw`, `ep.it_mw`, `ep.h100e`, `ep.capex_bn`, `ep.status`/`status_d`, `ep.tl` (timeline; projections to 2030), `ep.chips`; `sqft.json` (`sqft_total`, `buildings`, `acres`, `src_kind`, `asof` YYYY-MM).
- **`DataCenterRow.status`** (DcStatus) has no source field. It must be mapped from Epoch `operational`/`status`, `site.json.flag_txt` and power stage.
- **`financed`:** `case` → site, plus `debt[]` / `debt_m`.
- **Tag, as-of:** Epoch = third-party (CC BY 4.0, attribution required); `pw_found` carries `url` and `confidence`; `sqft.src_kind` permit/filing/county → F, company/press → R. The as-of is `dcmap.made` 2026-10-19T01:56Z; `ep.status_d` is per row.

### 3.3 `dc_building` (spec 17; `Building`)
- **Rows:** `epoch_dl/map_props.json` `dataCenters[].shapes.features` where `properties.type = "Building"` (384; 53 on covered campuses).

| Target field | Source |
|---|---|
| `footprint` | Polygon geometry |
| `label` | `properties.buildingNumber` (on 419 features of all types) |
| `status` | `properties.complete` 0/1 (on 1,098 features) |
| dates | `startDate`/`endDate` (47 features) |
| `campus_id` | DC `id` (Epoch name) → `"ep:"+id` or the covered dcmap key whose `ep.name` matches |

- **Also usable:** the other feature types (Cooling 392, Backup generator 326, Power substation 184, Power generator 14). They feed `backupGenerators.count`, and spec 27 capacity methods (MW and fuel are absent).
- **Not in the data:** `parcel_ids`, `facility_mw`, `it_mw`, `sqft`, `permit_ids`, `co_date`, `firstVisible`.
- **OSM `_map.json`** files need node→way assembly.
- **Tag:** third-party (Epoch) / community (OSM). There is no per-polygon as-of beyond `timeline[].imageryDate`.

### 3.4 `power_plant`, `power_unit`, `plant_serves` (spec 17; `PowerPlantRow`/`Detail`)
- **Plants:** `power.json.projects`, rows whose `ty` is gas plant (33), fuel cell (3), data center campus power (23), SMR (7) or nuclear restart (3). The 101 large-load interconnection rows are better kept as site/campus power facts (`DataCenterDetail.largeLoad`).

| Target field | Source |
|---|---|
| `id` | `id` |
| `name` | `n` |
| `operator_entity_id` | `dev` (developer; there is no separate operator) |
| `county`, `location` | none for all 182 |
| `state` | `st` (1 null) |
| `technology` | `ty`. Simple-cycle, CCGT and OEM appear only in `rw`/`n` prose |
| `status` | `stage` (under construction 38, proposed 37, approved 34, permitting 27, stalled 23, queued 8, withdrawn 8, operating 4, denied 3). It needs a mapping to `PlantStatus`; `stalled` has no equivalent |
| `permitted_mw` | `mw` (117 numeric). Errors: `N-ERCOT-BATCH0` 474000 (queue aggregate), `P-090`/`P-101` = 1.0 (GW), `P-087` = 2026.0 (a year) |
| `eia_plant_id` | none |
| `grid_mode` | none |
| other | `serves` ← `dc` (text); `debt` (text); `cod`/`cod0` (free text); `risk`/`rw`; `dk[]` (231 refs, 208 without a URL); `ev[]` (269); `src[]`; `conf`; `x[]` party tuples |

- **`power_unit`:** no source (OEM, model, count and rating are not fields).
- **`AirPermit`:** only as events: `dockets/out/TCEQ_178644`, `_182126`, `_182467`, `_182627` (status items, `meta.project_status`); `NMED_AQB_26_57` (hearing record); 7 inventory rows of kind `state_air_permit`.
- **`plant_serves` (plant → campus):** derive through the case. Use `case_seeds.power_ids` (case → plants) or `aiwcredit.pw2c` (plant → case), then `site_link`, or covered `dcmap.pw[]`. Never use `pw[]` on Epoch-only rows. The `pw2c` and `case_seeds` conflicts are listed in §2.
- **Tag, as-of:** a `dk` entry with an official docket URL → F; `ev` with `w` = "News …" → R; `conf` is a confidence, not a tag. As-of: `dk.ld`/`ev.d` per item, `power.asof` for the file.

### 3.5 `site_link` (spec 17)
- **Data has:** financed site (case) → campus for 19 pairs (`site.json.case` = `dcmap.case`). APLD_PF1B4 → `apld_polaris_forge_1` and GLXY_H1 → `galaxy_helios` follow only from names, not a field. 8 cases have no campus.
- **Not in the data:** `building_ids`, `evidence_fact_id`, `approved_by`/`approved_at`. Spec 17 requires one human approval per link, so every link has to be approved.

### 3.6 `entity` (spec 17; product-spec Party; `ProjectParty`)
- **Sources:**
  - `aidebt` `issuer`/`grp` + `gn`
  - `power.gx` (44 [name, ticker|"private"]) + `gf` (u utility / g generator)
  - HTML `aichain.tenants` (16 ids with `kind`) and `aichain.issuers` (26: name, sector, ticker, latestFiling)
  - `memo.chain` (288: role, name, what, ticker 54, grp 108, basis)
  - `aiwx` P/L tuples
  - `cases.parties` (sponsor, guarantor, ratings as text)
  - `gpu/operators.json` (12 with CIK)
  - BDC funds: `ref_meta.universe` (180) and HTML `data.funds` (name, mgr, type)
  - fund families: `q3x.hold.fams` (523, `typ`)
  - `banksdata` lenders
  - `q3x/docs/tickers.json` for CIK/ticker
- **Field mapping:** legal entity ← name strings (`tracking` SPV names such as "Red Chiles" appear in `fac.name`/`fid`); `parent_id` ← none; role ← `memo.chain.role` (19 values). `LinkRole` mapping: End user → end_user, Tenant → tenant, Developer/Sponsor → developer, Lender → lenders. The other roles have no `LinkRole`.
- **rating:** free text in `aidebt.rating` (600 non-empty, often an issuer rating), `cases.parties.tenant_rating`/`sponsor_rating`, `q3x.ctr.rating`, `trace/secs*.moodysRating`/`standardAndPoorsRating`.
- **CDS:** none.
- **Funding needs:** `cases.parties.sponsor_liquidity` (7), `q3x.oa.payer` (OpenAI revenue/burn), `research/dl/facts.json` (CoreWeave XBRL only), `q3x.sup`.
- **Tag:** `memo.chain.basis` disclosed 197 → F, reported 76 → R, presumed 15 → undecided. Needs entity resolution (spec 17 Linker).

### 3.7 Contract / lease (product-spec Contract; `LeaseRecord`)
- **Primary:** `q3x/ctr.json` (29, `id` = case).

| Target field | Source |
|---|---|
| lessee | `tenant` |
| `rating`, guarantee | `rating`; `guar` (17/29); `support[]` |
| term | `term` (21/29) |
| rent start / commencement | `start` (21/29; "2029", "H1 CY2027 (Oracle guidance)") |
| total | `tcv_m` (13/29) |
| annual rent | `rent_m` (2/29) |
| rent basis, termination | `rb`, `exit` (termination/FM text) |
| outside date | `outside` (3/29; 1 date) |
| renewal | `renew` (18/29) |
| quote, source | `quote` (29), `src[]` (29) |
| other | `cover` (13), `idle`, `nlease` |

- **Secondary:** `aiwc/cases/<CASE>.lease[]` (40):

| Field | Fill |
|---|---|
| `tenant` | 40 |
| `mw` | 30 |
| `term_yrs` | 27 (mixed types) |
| `start` | 26 |
| `tcv_m` | 14 |
| `annual_rent_m` | 3 |
| `outside_date` | 3 (text) |
| `termination_rights` | 40 |
| `renewals`, `src`, `quote`, `inferred` | — |

- **Also:** `aichain` cps (110); `aiw_proj` (term, counterparty, mw, RFS dates); `decks/iren_apld_facts.json` (`msft_tcv` 9.7 USD bn, asof 2025-11-03).
- **Tag:** the quote's source (10-Q → F). The `inferred` flag maps to `attribution: 'Inferred'`. There is no per-row as-of in `ctr`; use the filing date in `src` or the file `made` time.

### 3.8 Obligation edge (spec 18; `ObligationEdge`/`Node`)
- **Primary:** HTML `aichain.edges` (85).

| Target field | Source |
|---|---|
| from / to | `sheet` → `tenant` (16 tenant ids) |
| type | `role` (tenant 44, customer 30, end user 5, guarantor 5, credit support 1). Mapping to `EdgeType` is a decision: tenant → Lease and guarantor/credit support → Guarantee or backstop are direct; customer and end user are not |
| linked instruments | `debt[]` = cap indexes (no instrument ids) |
| conditions | `contract`, `backstop` (text) |
| other | `mw` (string), `risk`, `inferred` (8), `src` (keys into `issuers[sheet].sources`) |

- **Node totals:** `aichain.tenants[].direct.total`/`via.total` ($m).
- **Supplementary:** `cases.credit_support[]` (42: type, provider, amount_m 12/42); `aidebt.aiwx` (role letters T/M/C/B/D/V/K/G/R; no legend shipped); `rowinfo.x` (instrument → case role); `power.projects.x` (roles C/B/T/U/S/K, inferred from usage); `q3x.oa` (OpenAI share per instrument, M).
- **Not in the data:** `annual[]`, `start`, `termYears` as fields; `EdgeStatus`.

### 3.9 Instrument (product-spec Instrument; `InstrumentRow`/`Detail`)
- **Primary:** `aidebt.json` (707) + `aiwcredit.rowinfo`.

| Target field | Source |
|---|---|
| id | `id` |
| name | `inst` |
| type | `cat`; `rank` ("Convertible", "Guarantee / off-balance-sheet") |
| size | `amt_m` ($m; 419 filled; units mixed) |
| seniority | `rank` (28 values) |
| coupon or spread | `cpn` (free text, 610) |
| maturity | `mat` (YYYY-MM-DD 312, YYYY-MM 232, YYYY 41, non-date 8) |
| amortization | `aiwc/cases debt[].amort`; `params.debt.amort` |
| collateral | `cases.collateral`, `memo.security` (k = Collateral), `fac.collateral`, `collmeta` (by grp) |
| guarantee or backstop | `aidebt.backstop` (1 filled), `memo.security` (Guarantee, Backstop, Completion support), `cases.credit_support` |
| recourse, DSCR tests | `memo.security`, `q3x.trg` (15 trigger rows + 16 agreements with quotes), `fac.covenants`, `cases.metrics` (QTS only) |
| CUSIP | `aiwcredit.ids` + `trace/match.json` (multi), `bondq_list` ISIN |
| rating | `aidebt.rating`; `trace/secs` |
| other | `issuer`; `grp`; `tenants`; `flags` (9-value vocabulary); `status`; `src[]` (703; 77 only the generic N-PORT search URL); `chain` (text) |

- **Facility-level terms** (`q3x.fac`: size, drawn, lenders, advance_rate, ltv, docs[]) join only through `case` (31 rows). `gpu.ops[].fac[].rid` gives an `aidebt` id for operator facilities.
- **Computed fields:** `rowinfo` `s`/`p` (score and points, M), `y`/`sp`/`b` (yield, spread, benchmark), `cv` (convertible), `lv`, `ym`. `aidebt.score` disagrees with `rowinfo.s` (13/707 equal).
- **`chainStatus`:** not stored.
- **Universe:** choose the V1 50 from `targets.json` (46), `rowinfo.c` (53) and `aidebt.watch` (25). No locked list exists.
- **Tag:** `src` URL host. There is no per-field as-of on `aidebt` rows.

### 3.10 Holder position (product-spec Holder; `Holder`)

| Source | Holder.name | filing | filingUrl | filingDate | parMm | lastMark | shareOfNav | Instrument join |
|---|---|---|---|---|---|---|---|---|
| `aiwc/pxsrc/w2/**/rows_*.json` (3,810) | `fund` (`filer` holds the registrant CIK) | N-PORT | `url` | `filed` (2026-08-06..10-15); period `period` | `bal`/1e6 | `px` | `pctVal` | `cusip` → `ids`/`match`; loans via `other` (LoanX) |
| `aiwc/pxsrc/agg_<CUSIP>.json` (13 files, 1,213) | `fund` (`reg` = registrant) | N-PORT | `url` | `filed`; period `per` | `bal`/1e6 | `px` (null for NS) | — | CUSIP in the filename |
| `fundpos.json` (26,491) | fund via CIK → `ref_meta.fund_idx` → HTML `data.funds[fi][0]` | 10-Q (some 10-K) | `data.funds[fi].url` | `data.funds[fi].filed` (YYYYMMDD) | `par_k`/1000 | `m` | `realnav.nav[fi]` (NAV) | none (`bkey`/`lien`/`sp_bp`/`mat`) |
| `q3x/hold.json` (6,713) | `fam` (family) + `who[]` | `src` (N-PORT 6,539, NAIC 104+13, BDC 10-Q 36, press 21) | `url` (36 only) | `rd` (13 MM/DD/YYYY, 21 null) | `par` ($m) | `mark` | — | `id` (4,675 rows) |
| `q3x/ins.json` (127) | `ent` / `fam` | NAIC Sch. D (31 Dec 2025) | — | — | `par` | `fv`/`par` | — | dashed CUSIP |
| HTML `aichain.issuers[].holders` (328) | `holder` / `fund` | `kind` | `furl` | `filed`; as-of `asOf` | `parM` | `mark` | — | caps (no id) |

- **Text only:** `aidebt.holders` (1,373 strings; 42 with a mark), `memo.instruments.holders`, `cases.holders` (`bdc` always empty).
- **gapPts / navHitMm** need a Marden price (absent, §3.11).
- The `Holder.filing` enum (`'N-PORT' | '10-Q'`) does not cover 10-K, NAIC or press.
- **Tag:** F for filings, R for the "Credit agreement / press" bank rows.
- **As-of:** the report period (`per`/`period`, `rd`, 2026-06-30 for BDCs). Keep the filing date separately.

### 3.11 Price (product-spec Price; `Sourced<number>` prices)
- **Observed prices (one row per instrument, date and kind):**

| Kind | Sources |
|---|---|
| TRACE | `trace/eod.json` (CUSIP-level daily; 749 zero-price no-trade rows), `trace_rows.json` (169 ids; `ser` [date, px, yld]), `spreads.json` (121; G-spread vs UST), `aiwcredit.rowinfo.tr` (169; `ser` [date, px]; `gs`) |
| Fund / BDC marks | `aiwcredit.pxs` (`kind`, `label`, `url`, `lic`), `aidebt.pxsrc` (48), `pxsrc/out1`/`out2` (`kind`, `cpn_src`, `mat_src`, `lic`), `case_px` (26) |
| Dealer quotes | `case_px`/`out2`, `aidebt.watch.price` |
| Exchange fixings | `watch/*/bondq.json` (3 snapshots, 163 ISINs) |
| Latest headline price | `aidebt` `px`/`pxd` (511) |
| Equity | `research/dl/y_{CRWV,NBIS,ORCL}.json` (2026-08-17..10-16), HTML `aiwatch.eq` (29 tickers, 2026-10-15), `realnav.px` |
| Rates | `q3x/ust2023..2026.csv` (to 2026-10-19) |
| GPU | `gpu/gpu_out.json` `rent`/`lists`/`resale`, `watch/out/db/pulse.json` |

- **Marden model fields (M):** `aiwc/out/<CASE>.json` for the one modeled instrument per case (28): `p_loss`, `rec_given_loss`, `loss_rec_p10/50/90`, `yield_pct`, `spread_bp`, `el_bp`, `spread_after_el_bp`, `px_be`, `px_200`, `price` (the input). `params/<CASE>.json` holds the inputs (`Price.inputs`). `q3x.rec` holds recovery ranges; `q3x.uw` GPU LTV; `q3x.fc` the Q3 mark forecast; `q3x.rv` relative value.
- **No field is a "Marden price".** `q3x.rv.fair` equals `out.px_200` in 28 of 28 cases. The method text in `aiwcredit.method` says: "There is no model 'fair value', because the model carries no premium for bearing credit risk". `px_be` and `px_200` are undocumented.
- **methodVersion:** no string exists. The method is `aiwcredit.method` (HTML text), `aiwc/globals.json` (`paths` 20000, `_assumptions`, `_calibration`) and `global_inputs.json`. `q3x.fc` and `calls_r1` carry `sha256`; `calls_r1.model` = "forward marks v1 (gradient boosting)".
- **Licence:** `pxs.lic` "personal-only" on all 169 TRACE prices; "research-only" on 2 `out*` rows; Google News items are research-only; SemiAnalysis is paywalled; Silicon Data is commercial (`gpu.lic`).

### 3.12 Event (product-spec Event; `Alert`, `Evidence`, `DcRecord`, `materialEvents`, `Milestone.events`)

| Source | date | source | type | link affected | Notes |
|---|---|---|---|---|---|
| `aiwc/state/events.json` (601) | `d` | `src`/`u` | `kind` (news 425, county_board 98, court 42, city_council 16, ferc_filing 9, ferc 8, …) | `case` | `sev` −1..3, `drv`, `cls` (rule label, 96 non-null), `seen` (first seen) |
| `dockets/out/<key>.json` (5,801) | `d` | agency ← inventory `proceeding` | inventory `kind` | case ← inventory `case_id` | `t`, `u` only. 31 of 73 proceedings have 0 items |
| `power.json` `feed` (164), `dk` (231), `ev` (269), `dockets.lf` (245) | `d` / `ld` | `a` / `w` | `k` | project id `p` (32 of 164) | — |
| `feedx/feed_x` (142), `docket_x` (245) | `d` | `src` / agency | `kind` / `status` | `links[].case` / `cases[]` | `lvl` 0–2 |
| `q3x/cat.json` (49) | `d` (future) | `src[]` | `cat` | `case`, `debt[].id` | `dq` (scheduled / contractual / expected / deadline), `sev` |
| HTML `data.alerts` (95) | `date` | `url` (SEC) | `form` | issuer / ticker | `level` |
| `q3snap` alerts | `filed` (YYYYMMDD) | fund `url` via `watch/out/alerts.json` | cut / up / na | `bkey` | Mark events (StreamEvent `mark`) |
| `memo.issues` (160) | `next.date` (23) | `src[]` | `driver`, `cat` | case | `level` 0–3 |
| `aiw_proj.changes` (97) | `date` | `src` | — | project | `quote` |
| `aiwc/cases` risk evidence (236) | `d` | `u` | driver key | case | `q` |
| `bevents` (70) | `d` | `u`, `src` | `t` | `bkey` | — |
| `watch/tells_db.json` (23) | `date` | `url` | `cat` | `cik` / `fi` | — |

- **Status change:** not stored. Severity → `LinkStatus` is a rule decision (cases-research suggests sev 3 → red, 2 → amber). Candidate material events are `sev ≥ 2` with `cls` set.

### 3.13 Document (spec 17 `fact` "source document")
- **Sources:** every URL above; `q3x/docs.json` (199: iss, cik, form, date, acc, ex, desc, kind, url, title); `docs/library.json` (400); `docs/idx/<acc>.json` (1,581); `docs/titles.json` (1,287 url → title); `fac/dl/sub_*.json` and `aiwc/dl_b/*.json` (EDGAR filing indexes); `fac/dl/k8_extract.json` (155 8-K extracts, `txt` ≤ 2,500 chars); `fac/dl/exhibits_*.json`; `research/dl/sub.json`; `dockets/out` item URLs; decks (`file` + `page`, no URL).
- **Not in the zip:** the raw documents. Spec 21 requires raw-first storage, so they must be re-fetched. Also missing: deck PDFs, `triggers_raw` `doc_local_path` targets, `aiwc.js`, `parallax-q3-2026-calls.json`, the `iren/`, `glxy/` and `crwv/txt` local refs, the Sentinel images, and the `dockets/*.py` scripts.
- **DocType** (`Permit`/`Zoning`/`Filing`/`Utility`/`Court`/`Press`/`Imagery`) maps from inventory `kind`. The 42 "court" events in `state/events.json` are DCD news articles, so they map to Press.

### 3.14 Fact (spec 17)
- **Holds:** every non-entity field value with its quote.
- **Biggest sources:** `aiwc/cases/*` (fields + `quote`), `memo.security` (217; k/v/basis/src/q), `research/dl/facts.json` (CoreWeave XBRL, 2,705 datapoints, `accn` → filing), deck facts (376), `global_inputs.json` (sourced market inputs with `reliability`), `gpu/market.json`, `sqft.json`, `collmeta.json` (108; no dates), `q3x.trg`/`triggers_raw` quotes, `q3x.sup`, `aiwcredit.acct` (30 XBRL screens).

---

## 4. What V1 screens need that the data lacks → collector that must fill it
Collector names are taken from spec 22 ("Where every data point comes from") and specs 17, 19, 20, 24 and 25.

| V1 need (screen / type) | What the corpus has | Must be filled by |
|---|---|---|
| **Daily Marden price and spread** on 50 instruments (`mardenPrice`, `markGap`, `gapPts`, `navHitMm`, `RiskRow.price`, `StressResult.priceAfter`) | 28 one-off model runs (one instrument per case), with no price field (`px_200` undocumented) | Model worker (pricing engine), daily. Inputs: FRED API (Treasuries, SOFR; the UST CSVs end 2026-10-19), FINRA TRACE (licensed feed; current prices are `personal-only`), fund marks, dealer quotes (agent with source quote, R) |
| `priceHistory`, `change1w` for loans and private debt | TRACE daily for 169 bond ids only; 2–3 dated points for the rest; 3 days of Frankfurt quotes | Model worker history (from the first V1 run); TRACE feed |
| **Holders and marks per instrument** (`Holder`, `fundMarks`, `nextMarksPublic`) | N-PORT per series for 13 CUSIPs (`agg`) and 24 case files (`w2`, 2 of them empty), periods to 2026-08-31; BDC schedules at 2026-06-30; family aggregates | EDGAR full-text search + N-PORT XML / BDC table parser, every 10 min (Q3 BDC season: `q3cal` has expected dates per fund). LoanX → instrument mapping for loan holders |
| `shareOfNav` / `navHitMm` | `pctVal` on `w2` rows only; BDC NAV in HTML `realnav` | Same N-PORT/BDC collector |
| **Lease outside date, rent, commencement** (`CushionProjection.outsideDate`, `LeaseRecord.annualB`) | `ctr.outside` 3/29 (1 date), `rent_m` 2/29; `cases.lease` `outside_date` 3/40, `annual_rent_m` 3/40 | Extraction agent on 8-K exhibits and filings (press → R); lessee 10-K/10-Q lease notes (spec 25) |
| **Schedule cushion** (p10/p50/p90, `cushionHistory`, `deltaWeek`, `Milestone` keys and status) | Free-text completion dates; `params.months_to_complete`/`outside_months` (M assumptions); `out.p_late` | Cushion model, fed by FERC eLibrary (hourly), state utility dockets, state air-permit portals (daily), ERCOT/PJM/MISO/SPP queue reports (weekly/monthly), Sentinel-2 via Planetary Computer (each pass; change score weekly) |
| Chain link status, `ruleCode`, `ChainStatus` | Only issue levels and event severities | Fixed rules in code (inputs exist: `memo.issues.level`, `events.sev`/`cls`) |
| Site/campus coordinates for the 10 cases without a campus; `county` everywhere | 19 case-linked coordinates; county text for 27 of 29 cases | County parcel (assessor) geometry, else geocoded permit address (spec 17) |
| Building footprints and per-building sqft, MW, permit, CO date, `firstVisible` | Epoch polygons (53 on financed campuses); campus-level sqft | Overture Maps / Microsoft footprints (quarterly); building permits and certificates of occupancy from county collectors (daily, top 100 counties); Sentinel-2 |
| `ownerOfRecord`, `parcelIds`, `balancingAuthority` | none | County assessor collector; HIFLD/EIA territory and BA boundaries (spatial join) |
| Interconnection `queuePosition`, `largeLoad` | Queue serials in names; queue withdrawal/cancellation feed items | ISO queue and large-load report collectors (ERCOT, PJM, MISO, SPP); state utility dockets |
| **Plant units** (`PowerUnit` OEM, model, count, rating), `AirPermit` number/type/filed/decided, plant lat/lon/county | Prose only; TCEQ/NMED status events | State air-permit portals + extraction agent reading the equipment list (TCEQ, NMED, Ohio EPA); EIA-860M |
| `fuelSupply`, pipeline in-service, `gridMode` | `N-GREENCHILE` + FERC CP26-80 items only | FERC eLibrary (interstate); state pipeline permits (Texas RRC) |
| Plant debt (`UccLien`, mortgages, equipment orders, power buyer) | none | Secretary of State UCC-1 searches; county deed/mortgage records; supplier filings (spec 20) |
| Mechanic's liens, contractor health (`ProjectConstruction.liens`) | none (`dock.json` notes say recorder liens are not searchable) | County recorder; court dockets; state WARN (spec 26) |
| Opposition records (`tabCounts.opposition`, opposition filter) | Court/zoning proceedings in the inventory; prose | County agendas/minutes + state court dockets, collector + classification agent (daily) |
| **Ratings as structure** (`RatingState`: agency, outlook, last action, triggers) | Free-text ratings; `q3x.trg.ratings` ladder (8) | Rating agency releases, collector + extraction (spec 24) |
| `cds5y`, `Payer.kpis.cds5y` | none | Bond-implied CDS from the pricing engine (QuantLib); a CDS feed only if licensed (spec 24); otherwise n/d |
| `PayerCapacity` (OCF, capex, lease commitments, funding gap) for Oracle, Microsoft, Meta, Amazon, Alphabet, Nebius, IREN | Full XBRL company facts for CoreWeave only (`research/dl/facts.json`); FY2025 XBRL red-flag screens for 30 issuers (`aiwcredit.acct`); `gpu/operators.json` quarterly figures for 12 operators | SEC XBRL company facts API (no parsing) + extraction agent on lease/commitment notes (spec 24) |
| Obligation edge amounts by year, start, term, status | Free text on 85 edges; tenant totals | Extraction agent on 10-K/10-Q commitments, RPO and lease notes, and counterparty 8-K EX-10 (spec 18) |
| Party registry: entity ids, parents, LEI-like keys | Free-text names, 79 groups, partial tickers/CIKs | Entity resolver / Linker agent + `entity` table (spec 17); Secretary of State business records (spec 25) |
| Equity prices (ORCL, CRWV, NBIS, IREN) daily | 2 months for 3 tickers; one date for 29 | Market data API (Polygon or Tiingo), end of day |
| GPU index (`GpuPricePoint` p25/p75/nProviders/region; `GpuProviderPrice.capturedAt`) | Sparse rent series (H100 14 points, …); 13 list prices with no capture date; 5 days of pulse history | Daily public price-page collectors, one row per provider/model/term/region (spec 19: public pages, not a licensed index); resale-listing collectors |
| GPU units financed/delivered/energized (`GpuDeployment`), `CompanyCip` | Operator GPU counts mostly null; no CIP concept in CoreWeave XBRL | Extraction agent on credit agreements, 8-Ks, 10-Qs and ABS collateral descriptions (spec 19); XBRL for PP&E/CIP |
| Coverage of spec names: Anthropic Big Sky / North Star; Nscale amounts; OpenAI/Anthropic/Microsoft reference credits | Big Sky only as `fac ANTH_COMPUTE_SPV_35B`; no North Star | EDGAR full-text search on counterparty names (spec 18) + Researcher agent for n/d fields |
| Backtest of ~15 past repricings (`RiskList.earlierCalls`) | Only BDC mark backtests (`rt_hist`, `rt_up`, `nowcast_bt`, `readthru`) | Needs a dated price archive (TRACE + Marden price history) and dated evidence (`learned_at`) |
| Imagery (`SiteSchedule.imagery`, `DataCenterDetail.imagery`) | Scene metadata and roof areas; no image files | Sentinel-2 via Planetary Computer → R2 |
| Raw documents for audit and re-parse | URLs only | Every collector saves the raw file to R2 with hash and fetch time (spec 21, "Raw first") |
| `firstSeen` / `lastChange` per campus; `DcHistoryEvent` before/after | 11 docket first-seen dates; no field-level history | Fact store `valid_from` / `learned_at`, from first V1 ingestion |
| Watchlist, alerts, alert settings, home layout | none (expected) | App state |

---

## 5. Data quality issues to handle in loaders

**Versions and duplicates.** Load only the canonical copies in §0.1. Treat `aidebt.pretrace.json` as one earlier price point (381 of 707 rows differ), not as current data.

**Prices.** Every price needs its own date, kind and source; none of them is "the" price.
- JUPITER: 90.0 (09-18, dealer quote, used by `aidebt`/`case_px`/`rv`) vs 85.0 (10-12; `params`, `out`, `sim`, `site.json`, pretrace). `case_px` rejects the 85.
- `EX-VANTAGE_ORCL-366`: 97.68 (`cat`; PGIM 2026-07-31) vs 85.0 / 2026-10-12 (`oa`).
- CIFR_BARBER: 102.065 / 99.03 / 101.239. SALINE: 98.75 issue / 86.5 TRACE / 96.886 N-PORT.
- `pxs.px` ≠ `aidebt.px` on 16 ids. `watch.price` ≠ `rows.px` (CRWV-103: 81.062 vs 80.75).
- Convertibles trade far above par (`px` up to 1415.995; 13 rows above 130; WULF-371 165.7). Exclude them from curves and spreads.
- `bondq`: `odd:true` on `EX-NBIS-199`; `EX-WULF-371` TRACE 165.7 vs fixing 76.4 is a mismatch with no flag.
- TRACE `eod` has 749 zero-price no-trade rows (+65 in `eod_new71`). `secs*` uses the placeholder maturity `3999-12-31`.

**Units.**
- `amt_m` is in $m, but some rows hold holder par, not issue size (`NP-BE-093712AK3` = 1, `EX-CRUSOE-079` = 1, `EX-VANTAGE_ORCL-367` = 16, `-368` = 10). DDTL rows hold drawn amounts where `fac` holds commitments (DDTL 4.0: 2837 vs 8500).
- `aidebt.watch.amount_usd` and `aiw_proj.financing.amount_usd` are in USD.
- `fundpos` `par_k`/`fv_k` are in $k. `q3snap` `par`/`imp` and `niche.par` are in USD. `q3x.hold.par` is in $m.
- `fundpos.m` has 36 rows above 150 or below 0 (max 39000.9) and some 0s.
- `power.mw`: aggregate, GW-as-MW and year-as-MW errors (§3.4).
- GPU resale mixes per-GPU and per-8-GPU-node prices.
- `annualizedRt` comes as both "7.75" and "0.0775".
- MW means IT, gross or delivered depending on the field.

**Dates.**
- ISO dates, YYYY-MM, YYYY, quarters ("2026-Q4"), halves ("2026-mid"), ranges and prose all occur.
- `q3x.hold.rd` mixes MM/DD/YYYY (13) with ISO and has 21 nulls.
- `q3snap` and HTML `data.funds` use YYYYMMDD. UST CSVs use MM/DD/YYYY. Yahoo uses unix seconds.
- `NMED_AQB_26_57` has an item dated 2027-07-22 in a 2026/07 upload folder.
- Epoch timelines project to 2030-01-01 (95 future rows); tag them as projections.

**IDs and joins.**
- 2 CUSIP collisions.
- NP suffix ≠ CUSIP in 7 of 136.
- Multi-CUSIP instruments; RegS vs 144A ISINs.
- Dashed and `*` NAIC CUSIPs (`ins`, `hold.inst`).
- Loan CUSIP placeholders.
- 4 null `dcmap.debt.id` values.
- Vantage TX/WI links to 366 vs 367/368.
- `pw2c` vs `case_seeds` vs `dcmap.pw` plant conflicts; fuzzy `pw[]` on Epoch-only rows.
- `GLXY_H1`/`GLXY_H2` share `aiw_idx` 0.
- `bkey` name collisions; 27 unresolved bkeys; the `vantage`/`oracle vision` sector mislabels.
- `collmeta` keys do not match case ids or `aiwx` keys.
- `aichain` caps have no ids.

**Events.**
- 14 duplicate (case, url) pairs in `state/events.json`; 81 URLs repeat across cases (GLXY_H1 and H2 get identical items).
- All 42 "court" items carry `src` "U.S. District Court, District of New Jersey" but are datacenterdynamics.com news.
- Most news is sev 0 / drv other.
- Google News redirect URLs are not canonical. Some sources are non-English.
- TCEQ files repeat one project URL per item.
- 31 of 73 proceedings return 0 items.
- LPSC, NDPSC and VASCC are capped at the 50 most recent items.
- DENTON_CC and SALINE_TWP_BOARD carry items back to 2021 and 2007.

**Text-first values.**
- Ratings, coupons (`cpn` has more than 30 patterns; `coupon_pct` vs a `coupon` string), lease terms, ISO and utility (free text, e.g. "ERCOT (presumed)") and `term_yrs` (mixed types) must be parsed before they can be shown as `Sourced<number>`.
- `aidebt.rating` is often the issuer's rating or says "No rating found for this instrument".
- `tenants` contains "n/d" in 153 rows.

**Geography and scope.**
- `dcmap.state` mixes `Texas`/`TX` and includes non-US provinces.
- 17 non-US campuses; SOUTHGATE (AU).
- Multi-site cases.
- `pw_missing` has shifted fields (the address sits in `city`).

**Schemas.**
- `fac.price` is a string in `fac.json`, `{px,date,src,basis}` in `operators.json` and `{value,date,source,kind}` in `hyperscale.json`.
- `power.dk` holds objects; `dcmap.pw[].dk` holds arrays.
- `fundpos` and HTML `data` arrays are positional (column names come from `cols` or the inline JS).
- `sec_search.json` bodies are double-encoded JSON.
- HTML `data` is a 15 MB block.

**State contradictions.**
- `watch/state.json`: the Saratoga filing is "parsed" with err "ValueError('no positions')" and npos 142.
- `q3snap` `call` is always null; `fc` is null on 18 live rows.
- `pulse.gpu_decline_12m` uses a confusing sign; RunPod H200 +618% in one day is noise; there are only 5 days of history.
- `rt_up` quarter labels are inconsistent.
- `nontraded.json`'s third value does not equal fund FV.
- Two filers' non-accrual flags are unusable (the HTML JS sets `BADNA` above 45%).

**Licensing and attribution.**
- TRACE-derived prices are `personal-only`.
- 2 rows are `research-only`.
- Google News is research only.
- SemiAnalysis and Silicon Data are restricted.
- Epoch data is CC BY 4.0 (attribution).
- OSM `_map.json` files carry their own `copyright`/`attribution`/`license` keys; keep them with any footprint derived from them.

**Hygiene.** All quoted strings are third-party text. Store and render them as data, and never execute or interpret them.
