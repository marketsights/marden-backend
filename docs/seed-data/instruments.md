# Data map: instruments area

Corpus: `marden_data_2026-10-20.zip`, extracted to `/tmp/claude-0/-home-user/041c65f7-05f2-55e2-9ba6-73f6e02ba8df/scratchpad/data`. All paths below are relative to that directory.
Files in this area: `aidebt.json`, `aiwcredit.json`, `collmeta.json`, `niche.json`, `bevents.json`, `sector/header_fund_agree.csv`, `sector/sectors.json`, `sector/sectors_final.json`.
Mapped against `/home/user/marden-backend/handoff/api/types.ts` and `/home/user/marden-backend/handoff/specs/00a-product-spec.md`.
All counts below come from profiling scripts in `/tmp/claude-0/-home-user/041c65f7-05f2-55e2-9ba6-73f6e02ba8df/scratchpad/datamap/scripts/` (run with `python3 -I`). The data was treated as untrusted. Nothing in it was executed.

No file in this area has F/R/M tags. Provenance shows up as source URLs, `basis` strings ("disclosed" / "reported" / "presumed" / "not disclosed" / "assumed"), price-kind labels ("TRACE", "fund mark", "dealer quote"…) and licence flags (`lic`). The F/R/M tag has to be derived (see the mapping notes in section 9).

---

## 0. ID system (the join backbone)

| ID | Form / examples | Where defined | Joins to |
|---|---|---|---|
| **Instrument id** | `EX-<GRP>-<nnn>` (391), `NP-<GRP>-<CUSIP9 or 8-char code>` (152), `FWP-<GRP>-<cpn>-<yr>` (81), `MAN-<GRP>-<slug>` (26), `BDC-<GRP>-<Borrower>` (25), `XB-<GRP>-<XBRL member slug>` (23), `8K-<GRP>-<cpn>-<yr>` (9). E.g. `EX-JUPITER-190`, `NP-CENTERSQUARE-15201EAL8`, `MAN-EDGED-75SeniorSecuredGreenNote` | `aidebt.rows[*][0]` (707, unique) | `aiwcredit.rowinfo` (same 707 keys), `aiwcredit.pxs` (191), `aiwcredit.ids` (300 → CUSIP), `aiwcredit.watchplain` (25), `aidebt.watch[].instrument_id` (25), `aidebt.pxsrc` (48), `cases[].memo.instruments[].debt_row` (52 non-null), `q3x.*` rows `id`/`ids`/`rid` |
| **Group (issuer family)** `grp` | `ORCL`, `CRWV`, `JUPITER`, `VANTAGE_ORCL`, `XAI_VCI` (79 distinct) | `aidebt.rows[*].grp`; display names for 29 of them in `aidebt.gn` (`{grp: [name, ticker]}`) | `aidebt.aiwx` entries of kind `G`, `collmeta` key prefix, `cases[].memo.chain[].grp`, `q3x.hold.grp`, `q3x.*.g` |
| **Case id** (financed project / site / deal) | `JUPITER`, `ABILENE`, `VANTAGE_TX`, `VANTAGE_WI`, `SALINE`, `BEIGNET`, `CRWV_DDTL3`, `XAI_VCI` … (29) | `aiwcredit.cases[].id` | `rowinfo[*].c` (instrument is the case's own debt) and `rowinfo[*].x` (`[case, role]`), `dockx[*].c`, `pw2c` values, `gpu.pwp[].id`, `q3x.site.sites[].case`, `q3x.dcmap.rows[].case`, `q3x.cat.rows[].case`, `q3x.dock.rows[].case`, `q3x.ctr.rows[].id`, `q3x.rv.rows[].id`, `q3x.fac.rows[].case`, `q3x.rec.proj.rows[].key` |
| **aiwx key** | `"Issuer|Project"`, e.g. `"Oracle|Project Jupiter (Stargate New Mexico)"` (80) | `aidebt.aiwx` keys (ordered) | `cases[].aiw_idx` holds integer **positions** into the `aiwx` key order (all resolve) |
| **CUSIP** | 9 chars, `00202DAA5` | `aiwcredit.ids` (300), `rowinfo[*].tr.c` (169), NP ids (136 with 9-char suffix) | TRACE series; `q3x.hold.inst` keys (162 are CUSIPs); `q3x.hold.fams[].pos[].cusip` |
| **Facility id** `fid` | `JUPITER_RED_CHILES`, `ANTH_COMPUTE_SPV_35B`, `CRWV_DDTL4` (213) | `aiwcredit.q3x.fac.rows` | `q3x.comp.rows[].fid` (117/117 resolve). **No link from fid to aidebt instrument ids** (0 of 213 rows mention one); only 12 fac rows carry `case` |
| **Power plant id** | `N-JUPITER-MICROGRID`, `P-097` | `aiwcredit.pw2c` keys (24) | Ids exist in `power.json` (outside this area; checked by grep); values are case ids |
| **Docket key** | `"FERC|EL25-49"`, `"PUCT|58317"` (56) | `aiwcredit.dockx` | `dockx[*].c = [[case, reason]]` |
| **Borrower key** `bkey` | lowercase normalized borrower name: `coreweave financing`, `retained vantage`, `firmus supercloud` | `sector/sectors*.json` keys, `niche.b` keys, `bevents.ev` keys, `aidebt.aiwx` entries of kind `B` | `fundpos.json` column `bkey` (outside this area; its `sec` vocabulary matches `sectors_final` values) |

---

## 1. `aidebt.json`: master instrument list (626,062 bytes, JSON object)

Top-level keys: `cols`, `rows`, `watch`, `aiwx`, `gn`, `pxsrc`. There is no as-of field (the zip is dated 2026-10-20; the latest `pxd` is 2026-10-20).

### 1a. `cols` + `rows`: 707 instruments (column-array format)
`cols` = `"id,issuer,grp,cat,inst,cpn,mat,amt_m,rank,rating,tenants,backstop,px,pxd,yld,holders,flags,score,why,src,chain,status"`. Every row has exactly 22 cells. All 707 ids are unique.

Each record is **one debt instrument or debt line** (bond, loan, ABS class, convertible, lease obligation, BDC private-credit position placeholder) tied to AI or data-center infrastructure, plus utility and IPP bonds whose link to AI is load growth.

| Field | Fill | Values / samples | Notes |
|---|---|---|---|
| `id` | 707 | `EX-ABILENE-000`, `NP-BE-093712AK3` | prefix = source path (EX curated, NP N-PORT, FWP free-writing prospectus, 8K, XB XBRL member, MAN manual, BDC) |
| `issuer` | 707 (245 distinct) | `Oracle Corporation` (60), `Abilene JV (Crusoe / Blue Owl / Primary Digital)` | free text; variants of the same name (e.g. `Vistra Operations Co LLC` vs `Co. LLC` in `inst`) |
| `grp` | 707 (79) | `ORCL` 63, `EQIX` 36, `QTS` 34, `VST` 33, `CRWV` 23 | only 29 have a `gn` display name |
| `cat` | 707 | `corp` 310, `abs` 159, `util` 89, `proj` 87, `bdc` 62 | |
| `inst` | 707 (588) | `Phase 1 construction loan`, `9.75% Sr Notes 2031`, `ADC 2021-1 A2` | 25 rows are the placeholder `BDC / private-credit loan(s) - link to product BDC marks by borrower name` |
| `cpn` | 610 | `5.25%`, `Floating`, `SOFR+2.50%`, `S+250 (6.64%)`, `13% fixed; 2.5% EOT` | free text, more than 30 format patterns |
| `mat` | 593 | `2031-03`, `2046-08-15`, `2026`, `2027-2066`, `Short-term`, `5 yr incl. extensions` | mixed precision: 312 YYYY-MM-DD, 232 YYYY-MM, 41 YYYY, 8 non-date |
| `amt_m` | 419 | `2300`, `18000`, `288000` (max) | $m. **Units mixed**: some rows hold holder par, not issue size (e.g. `NP-BE-093712AK3` amt_m=1; `EX-CRUSOE-079`=1). DDTL rows hold *drawn* (DDTL 4.0 = 2837) where `q3x.fac` holds *commitment* (8500) |
| `rank` | 707 (28) | `Senior unsecured` 242, `Secured (SPV)` 191, `Convertible` 48, `Guarantee / off-balance-sheet` 13 | maps to instrument seniority |
| `rating` | 600 non-empty (154) | `Moody's Baa2 (neg) / S&P BBB- / Fitch BBB (Oracle)` | free text, often an issuer rating; some say "No rating found for this instrument. Related: …" |
| `tenants` | 513 non-empty (59) | `Oracle -> OpenAI`, `n/d` (44) | free text; 153 contain "n/d" |
| `backstop` | 1 | `No guarantee disclosed. Oracle reportedly obligated…` | almost empty |
| `px` | 511 | `90.0`, `99.67` | price in points. Range 50.83 to 1415.995: convertibles above 130 (13 rows, e.g. Bloom `NP-BE-093712AM9` 1415.995, Cipher conv 537.5) |
| `pxd` | 511 | `2026-07-31` (197), `2026-10-20` (107), `2026-10-16` (74), `2026-06-30` (73) | range 2025-12-31 to 2026-10-20; every `px` has a `pxd` |
| `yld` | 403 | `{"value": 1.96, "basis": "calc YTM from listed mark (approx., semiannual)"}` | range -0.73 (NBIS conv) to 16.26 |
| `holders` | 291 rows non-empty, 1,373 strings | `"Fidelity Investment Grade Bond Central (par $20.0m) mark 100.22 as of 2026-06-30"`, `"PIMCO FUNDS ($21.7m)"`, `"Goldman Sachs Lending Partners (lead left) (par n/d)"` | **unstructured strings**: only 42 carry a mark, 45 an "as of" date |
| `flags` | 271 non-empty | `tenant concentration` 200, `financing gap` 140, `rating action` 68, `price < 90` 49, `COD slip` 35, `covenant amendment` 27, `litigation` 10, `cost overrun` 8, `power/permit delay` 5 | fixed 9-value vocabulary |
| `score` | 695 | 0 to 8 in steps of 0.5 (2.5 most common) | stress score; **differs from** `aiwcredit.rowinfo.s` (only 13 of 707 equal) |
| `why` | 707 | "Lowest-risk Oracle project: mostly built 1.2 GW campus…" | analyst text |
| `src` | 703 non-empty | SEC EDGAR URLs; 77 rows only have the generic `https://www.sec.gov/edgar/search/#/forms=NPORT-P` | source URLs (no per-URL date) |
| `chain` | 707 (172) | `Abilene JV (Crusoe/Blue Owl/PDI) -> Oracle 15-yr lease -> OpenAI` | free-text chain; not structured parties |
| `status` | 707 | `outstanding` 701, `proposed` 5, `repaid` 1 | |

Quality: there are 2 CUSIP collisions (`294136363` → `NP-EQIX-29390XAD6` and `NP-EQIX-31038551`; `629377CP5` → `NP-NRG-629377CP5` and an `XB-NRG-…` row), so the same bond appears twice. 16 NP ids end in an 8-char code that is not a CUSIP (`NP-QTS-48279104`). 57 rows have neither maturity nor amount. 196 rows have no `px`: corp 81, proj 49 of 87, bdc 38, abs 14, util 14. That includes the Vantage–Oracle and Abilene construction loans.

### 1b. `watch`: 25-row ranked watch / risk list
Keys (all 25): `rank` (1–25), `instrument_id` (all in rows), `issuer`, `instrument`, `amount_usd` (USD, **not $m**: 18000000000.0), `price` {`value`,`date`,`source`,`basis`} (4 null), `stress_score`, `stress_flags[]`, `trigger` (text), `quant` {`slip_months`, `financing_gap_usd`, `tenant_risk`, `cost_overrun_usd`, optional `*_note`}, `what_confirms`, `next_catalyst` {`date`,`event`,`source`,`date_is_estimate`}, `sources[]`.
Sample: rank 1 `EX-JUPITER-190`, price 90.0 on 2026-09-18 (“Dealer quotes 89–91 · FT via Reuters”), next catalyst 2026-10-26 NMED hearing. Catalyst dates run from 2026-10-26 to 2027-08-25 (4 null).
Quality: `watch.price` can differ from `rows.px` (e.g. CRWV-103: 81.062 vs 80.75). The next catalyst can differ from `aiwcredit.watchplain.next` (Jupiter: 26 Oct hearing vs 23 Nov deadline).

### 1c. `aiwx`: 80 "Issuer|Project" → linked-party lists (the chain graph)
Value = list of tuples. Tuple kinds (position 0):
- `G` = aidebt group: `[G, grp, role, regex?, label?]`. The regex matches `issuer + " " + inst` of rows in that grp (all 35 regexes resolve, e.g. `DDTL 3\.0` → `EX-CRWV-094`).
- `P` = private party: `[P, name, role, note]` (e.g. `OpenAI`, `Unnamed hyperscaler`, `SoftBank`).
- `L` = listed company: `[L, ticker, role, "Name|note"]` (e.g. `GOOGL … "Alphabet (Google)|Backstops Fluidstack's rent if Fluidstack can't pay"`).
- `B` = BDC borrower key: `[B, bkey, role]` (e.g. `coreweave`, `coreweave financing`, `crusoe`). 4 of the 9 bkeys are in `sectors_final`.
Role letters seen: T (tenant/paying), M (end user/customer), C (the project's credit / debt), B (borrower / BDC loans), D (developer/landlord), V (developer), K (equity/developer partner), G (guarantor/backstop), R (site/power partner). These letters are inferred from the labels; no legend is shipped.

### 1d. `gn`: 29 `grp → [display name, ticker]`, e.g. `"JUPITER": ["Project Jupiter construction loans", ""]`.

### 1e. `pxsrc`: 48 instrument ids → `{kind, label, url, note?}`
kind: `fund mark` 45 (N-PORT primary_doc.xml URLs), `issuer fair value` 2, `dealer quote` 1. Sample: `EX-CLOUDHQ-034` → "N-PORT fund mark · PGIM Total Return Bond Fund · 31 Jul".

---

## 2. `aiwcredit.json`: the computed credit workbench (7,054,605 bytes, JSON object)

Top-level keys: `watchplain`, `rowinfo`, `dockx`, `asof` (`"2026-10-20 15:01 UTC"`), `paths` (20000), `method` (HTML text), `cases`, `sprnote`, `pxs`, `ids`, `pw2c`, `sig` ([] empty), `gpu`, `q3x`, `acct`. Sub-blocks carry their own `made` timestamps (2026-10-18 to 2026-10-20) and `asof` (2026-10-15/16/17/20).

### 2a. `rowinfo`: 707 per-instrument computed fields (keys = aidebt ids, exact match)
| Key | Fill | Meaning / sample |
|---|---|---|
| `s` | 707 | score 0–8 (Jupiter 8.0) |
| `p` | 707 | score points `[[label, pts]]`, e.g. `["Yields 10.6%, 643 bp over Treasuries, at a price of 90.0", 2]`, `["Model: 22% odds of a loss", 2.5]`, `["Ranks behind secured debt", …]` |
| `c` | 53 non-null | case id when the instrument is the case's own debt (30 distinct, e.g. `JUPITER`, `CLOUDHQ`) |
| `x` | 226 non-empty | `[[case, role]]` exposure links; roles: Tenant 600, Borrower 157, Developer 40, Sponsor 38, Site owner 5, Power plant 3 (28 cases) |
| `y`, `sp`, `b` | 689 (240/241 null) | yield %, spread bp (−467…1221), benchmark `UST`/`SOFR`/`5y Treasury` |
| `tr` | 169 | TRACE: `c` CUSIP, `jul` (31-Jul px), `m1`, `w1` (1-month / 1-week-ago px), `y`, `ser` [[date, px]] daily 2026-01-05…2026-10-19 (1–60 points, 9,255 total); 121 also have `g` {d, s=G-spread bp, ust, chg1w, chg1m, chg_jun, mat} and `gs` [[date, gspread_bp, yield, ust, px]] (~200 points from 2026-01-02) |
| `lv` | 64 | date `2026-10-20` (meaning not documented; flags a same-day price) |
| `cv` | 51 | `1` = convertible |
| `ym` | 8 | `1` = yield from model/assumption |

### 2b. `pxs`: 191 instrument ids → current price record
Keys: `px`, `date`, `kind`, `label`, `url`, `lic` (+ `fund` alt-source object on 169; `n_funds`, `note`, `cpn`, `mat` on 22). kind: TRACE 169, fund mark 9, none 7, BDC mark 5, dealer quote 1. `lic`: `personal-only` 169 (all TRACE), `ok` 22. Dates 2026-02-12…2026-10-20. In 16 cases `pxs.px` differs from `aidebt.px` (e.g. `EX-FIRMUS-143` 97.96 vs 92.88; `EX-EDGECORE-140` date 2026-02-26 vs 2026-06-30).
**Licensing:** TRACE-derived prices are flagged `personal-only`, and one Frankfurt quote is `research-only`. That limits what can be displayed commercially.

### 2c. `ids`: 300 instrument id → CUSIP (no mismatches with `tr.c`).

### 2d. `watchplain`: 25 (same ids and order as `aidebt.watch`)
Plain-English cards: `project`, `borrower`, `parties`, `debt`, `security`, `lenders`, `why`, `confirm`, `next`, `headline`. Text only, no dates or URLs.

### 2e. `cases`: 29 financed projects / deals (875 KB). These are the core site-plus-instrument objects.
Keys (29 unless noted): `id`, `name`, `short`, `sub`, `title`, `place` (`"Santa Teresa, New Mexico"`), `kind` (28: building / gpu / pool; EDGE_CEDAR none), `inst`, `amt_m` (520–27,294), `price`, `price_kind`/`price_date` (28), `price_label`/`price_url` (26), `sim` (28; EDGE_CEDAR null), `sens`, `kv`, `drivers`, `holders`, `watch`, `events`, `notes`, `verdict`, `aiw_idx`, `memo`, `traded`, `tb`, `buildup` (28), `qchg` (3).

The 29 ids: ABILENE, APLD_PF1, APLD_PF1B4, APLD_PF2, BEIGNET, CIFR_BARBER, CIFR_BLACKPEARL, CIFR_STINGRAY, CLOUDHQ, CORZ_CW, CRWV_DDTL3, CRWV_DDTL4, DATABANK, FLASH_ABERNATHY, GLXY_H1, GLXY_H2, HUT_BP, HUT_RB, IREN_CHILDRESS, JUPITER, QTS, SALINE, SOPAIPILLA, SOUTHGATE (Melbourne, AU), VANTAGE_TX, VANTAGE_WI, WULF_LM, XAI_VCI, EDGE_CEDAR.

- `sim` (Monte Carlo, 20,000 paths): `p_full`, `p_loss`, `rec_given_loss`, `loss_rec_p10/p50/p90`, `exp_loss_pct`, `cause{abandon, late_termination, tenant_default, non_renewal, refinance}`, `cause_rec{…}`, `p_unfinished_at_maturity`, `p_bust`, `p_tenant_default`, `p_abandon`, `hist[10]`, `wal_yrs`, `yield_pct`, `spread_bp`, `el_bp`, `spread_after_el_bp`, `bench`, `px_be`, `px_200`, `h_tenant`, `h_sponsor`, `price`. Jupiter: p_loss 0.2176, rec_given_loss 0.6766, el_bp 377.
- `sens[]`: `{k, label, p_loss, d_p_loss, el_bp, d_el_bp, exp_loss_pct, d_exp_loss_pct}`; k ∈ tenant_x2 (28), delay_p6 (22), drivers_up (22), rent_m30 (20), no_support (9), gpu_fast (5), noi_m15 (3).
- `kv`: secured, relet, lease, rent, support, walk, build, assumed, mw (19), debt_mw (19). All are text strings.
- `drivers`: power / permits / water / construction / tenant / sponsor / other → `{level 0–3, status, detail, evidence[{d,w,q,u}], pd?}`; 208 evidence items with date, headline, quote and URL.
- `holders`: `{funds: [[name, par_m]], bdc: [] (always empty), banks: [names]}`.
- `watch[]` (186 total): monitored sources `{agency, id, kind, kindl, url, ok}`; kinds news_query 49, county_board 33, state_puc 33, state_env_permit 21, city_council 14, iso_queue 10, ferc_docket 9, court 7, sec_filings 7, zoning 3.
- `events[]` (125 total; 11 cases have 0): `{case, src, kind (news 110, ferc 8, county_board 3, city_council 3, ferc_issuance 1), d (2026-09-26…2026-10-20), t, u, seen, sev (−1/0/2/3), drv, cls (Force majeure 4, Tenant pull-out, Pipeline or fuel supply, Court case), by?, named?}`.
- `memo` (the project file): `headline`, `status`, `chain[]` (288: `{role, name, what, ticker, grp, basis}`; roles Borrower 35, Utility 32, Tenant 30, Sponsor 29, Lender 29, Site owner 22, Project company 17, Grid operator 14, Guarantor 12, End user 9, Power plant 8, Contractor 7…; basis disclosed 197 / reported 76 / presumed 15), `instruments[]` (78: name, borrower, amt_m, terms, ranking, price, price_date, price_kind, price_note, price_src, holders[], third_party, modeled, note, `debt_row` → aidebt id, 52 non-null), `security[]` (k/v/basis/src/s/q; k = Collateral, Title to the site, Lease term, Termination rights, Guarantee, Equity cushion, Reserves, Completion support…), `unknowns[]`, `unknowns_s[]`, `issues[]` (160: driver, level, title, what, why, next{date,event}, clears_if, src[], impact, clears, cat), `cap[]` (78 capital-stack rows: tranche, issuer, rank, coupon, maturity, amt_m, price, px_date, px_src, holders, modeled, note).
- `traded[]` (42): related listed companies `{name, role, what, ticker, eq{p,d,off_hi,m1}, bonds[{n,px,d,y}], nb, lo}`.
- `tb`: table row `{spon, spon_tk, ten, ten_tk, st, typ, cpn, mat, amt}`.
- `buildup`: loss-path decomposition `{p_full, paths[{k,p,rec,how}], resolve[{k,level,p_ab,p_full_if}], abandon…}`.
- `verdict` (text), `notes[]` (assumptions).

Quality: Jupiter's `price`=90.0 (18 Sep dealer quote) but `sim.price`=85.0 and the verdict cites "85.00 on 12 Oct". Maturity is `2029-12` in `pxs` but `2029-11` in memo. `bdc` is always empty. EDGE_CEDAR has no sim/price.

### 2f. `dockx`: 56 regulatory dockets → affected cases
Key `"AGENCY|docket"` (FERC 37, PUCT 8, LPSC 2, others 1 each). Value `{c: [[case, reason]], o: [other projects], lvl 0–3, why[], imp: "2 campuses, $8.1bn of debt in scope"}`. No dates or URLs.

### 2g. `pw2c`: 24 power plant id → case id (`N-JUPITER-MICROGRID → JUPITER`, `P-097 → VANTAGE_TX`).

### 2h. `gpu`: GPU collateral reference (asof 2026-10-17)
- `rent`: `{H100: 14, A100: 2, H200: 7, B200: 9}` points `{d, v ($/GPU-hr), src, lbl}`, 2024-09-15…2026-10-15 (Silicon Data, xenospectrum).
- `lists`: 13 provider list prices `{m, v, p, src}` (CoreWeave H100 6.16, Lambda 3.99, Nebius 4.5…).
- `resale`: 2 points (H100 $50,000 2024-07-15; $14,250 2026-08-15).
- `resid`: residual-value bands % for H100/H200/B200/B300 at 2026/2028/2030 (`H100 2028: [6.0, 10.5]`).
- `chips`: 11 `{k, name, ship, tdp, lo, hi (all-in $/GPU), what, csrc, fp8}`.
- `life`: 17 company depreciation lives `{c, y, src, n}` (CoreWeave 6, Amazon 5, Meta 5.5…).
- `pue` `{lo 1.1, hi 1.54}`, `cents` (16 states' EIA industrial ¢/kWh, 2024 annual), `pwg` (PJM/MISO capacity prices, EIA STEO).
- `ops`: 12 operators `{n, full, tk, role, rev, per, rev_ttm, ebitda, int, capex, dep, rpo, mw_a, mw_c, life, top (customer concentration text), gpud, othd, fac[17: n, amt, drawn, r, mat, am, col, cust, src, rid→aidebt id, px, pxd, y, sp, b, s, …], notes, util, src}`.
- `pwp`: 26 per-case power economics `{id=case, mw, mwb, pue, st, px ($/MWh), b26, b30, b30s, rent, payer, ifleaves, kw, liq, gen, debt_m, cpn, src[]}`.
- `lic`: 10 licence caveats (SemiAnalysis paywalled; Silicon Data commercial; Ornn via reposts).

### 2i. `acct`: 30 issuers' accounting red-flag screens (CRWV…ORCL, BE, SMCI, VRT, EQIX, DLR + 15 BDCs: ARCC, OBDC, BXSL, FSK, MAIN, GBDC, HTGC, PSEC, TSLX, OCSL, MFIC, GSBD, CGBD, NMFC, BBDC)
`{fy, form, url (10-K), peer, sic, np, nf, fs, ms (M-score), cav[], notes[], miss[], rows[{f, s, v, p, n, fired, thr, tv, lift, src, ne, w, pv, pp}]}`. XBRL-derived; FY 2025-12-31.

### 2j. `q3x`: 20 analytic tables (5.28 MB) behind the Q3-2026 views
| Block | Rows | What a record is | Key fields / joins |
|---|---|---|---|
| `fc` | anchors 8, pos 56, funds 32 | Q3 fund-mark **forecast**: anchor bond baskets (`CRWV_UNSEC`, `ORCL`, `META_PROJ`…; bonds with `id`, p0 @2026-06-30, p1, dy bp); BDC/fund positions | pos: `fund, cik, mgr, ft, borrower, lab, grp, fac, lien, tt, mat, par_m, fv_m, q2 (Q2 mark), nonacc, url (10-Q), filed, anc, beta, bi{events, first_share, avg_lag_q, score}, fc, lo, hi, usd_m, inst_id (9)`; `method` and `sha256` included |
| `ctg` | ten: ORCL (62 ins, 40 fams), CRWV | Tenant contagion: loss per 100/300 bp move (`l100`, `l300`) by instrument and fund family; `link` true = linked (not direct) debt | `ins[].id` → aidebt; `sum{direct_m, linked_m, l100_*, l300_*}` |
| `uw` | 12 loans | GPU-loan underwriting / LTV path | `ids[]` → aidebt (10 of 12 non-empty), `chip`, `bal_m`, `coll_m`, `ltv`, `ser [[t, bal, coll_cons, coll_mod]]`, `units [lo, base, hi]`, `balloon` |
| `sup` | big 3, rows 24 | Supplier / customer-concentration exposure (NVDA guarantees $105bn etc.) | `tk`, `items`, `score`, `top`, `pct`, `src` (10-K) |
| `rvc` | 72 | Price-change attribution (rates / credit / pull) Q3 and 1-month | `id` → aidebt; `q{d0,p0,y0,d1,p1,…}`, `m{…}` |
| `oa` | rows 86, fams 40, buckets 6 | OpenAI exposure through Oracle (share 0.45) and CoreWeave (0.22) | `id` → aidebt; `bucket`, `share`, `oa_m`, `holders`; `payer` (OpenAI revenue / burn, sourced) |
| `rec` | crwv cases, proj 8+, gpu 12 | Recovery: CoreWeave low/base/high by tranche (`id`, `rec` [lo, base, hi]); project recovery (`key` = case, `rec`, `debt_per_mw`); GPU recovery cons/mod | |
| `trg` | rows 15, terms 16 | Covenant / rating triggers read from agreements (`fac`, `ids`, `trig`, `thr`, `now`, `head`, `st` red/amber/green/nd, `q` verbatim quote, `src`); `terms[]` per agreement with section + quote; `ratings` ladder | `ids` → aidebt |
| `site` | 21 | Sentinel-2 construction check per site | `key`, `name`, `lat`, `lon`, `accuracy_note`, `case`, `scenes[{date, id, roof_ha, new_roof_ha, img}]`, `first_ha/last_ha/new_ha`, `flag` (slow/ok), `ids`, `debt_m`, `px`, `sched`, `orig`, `first_delivery` |
| `ins` | rows 127, grps 18, fams 18, buys 13 | Insurer Schedule D holdings (31 Dec 2025) + 2026 buys | `ent, fam, st, cusip ("076912-AA-2"), desc, naic, par, fv, bv, grp, g` |
| `hold` | fams 523 (6,713 positions), inst 500, grp 48 | **Holder positions**: fund family → positions | pos: `k, id (→aidebt, 4,675 of 6,713; 2,038 null), iss, g, seg, inst, kind (Bond 5201, ABS 1121, Loan 217, Statutory 104, Private loan 36, Bank loan 21, Bought 13), mat, cusip, par ($m), mv, who[] (fund series), rd (report date), src (N-PORT 6,539 / NAIC 104 / BDC 10-Q 36 / press 21 / NAIC Pt3 13), naic, mark, nw, px, pxd`. Totals: `tot` 79,416.8 $m, `npos` 17,024 raw, `nfam` 523, N-PORT report dates 2026-05-29…2026-07-31 |
| `cat` | 49 | Catalyst calendar | `d` (2026-10-19…2028-08-15), `dq` (scheduled/contractual/expected/deadline), `ev`, `who`, `case`, `cat`, `lvl`, `sev` (watch/material/severe), `debt[{id…}]`, `amt_m`, `src`, `h`/`hk` holders; `cov` = CoreWeave covenant calc (debt 35,551, LTM EBITDA 4,400, lev 5.85–6.82x vs 6.0x) |
| `dcmap` | 102 | Data-center map (85 US; 21 `cov`=true are financed) | `key`, `name`, `lat`, `lon`, `case` (19 distinct), `owner`, `users`, `tenant`, `state`, `loc`, `gross_mw`, `it_mw_plan`, `deliv_mw`, `sched`, `orig`, `pwr_status`, `pwr_detail`, `debt[{id, inst, amt_m, px, pxd, g, rating}]`, `debt_m`, `ep` (Epoch AI record: addr, owner, energy, build, h100e, mw, capex_bn, chips, bldg, status), `s2` (Sentinel images); `map` = US SVG paths |
| `heat` | 21 | MODIS night-temperature anomaly per site, monthly 2024-01…2026-10 | `key`, `months[]`, `dT[]`, `mw_now`, `mw_plan`, `status` |
| `ctr` | 29 (id = case id) | **Contract / lease terms** per case | `name, full, tenant, rating, guar, support[{t, p, amt, c}], term, start, tcv_m, rent_m, rb, exit, outside, renew, quote (verbatim filing), src[], amt_m, px, pxd, cover, idle, idle_t, nlease` |
| `fac` | 213 (`fid`) | Facility registry (189 active, 14 repaid, 7 proposed, 3 dup) | `borrower, sponsor, name, type (dc_abs 37, convert 36, gpu_loan 31, dc_construction_loan 29, dc_project_bond 21, corporate_loan 21, corporate_bond 19, gpu_lease 8…), size_m, drawn_m, date (2023-05…2026-10-16), maturity, rate, price (text), lenders[], collateral, chips, gpu_count, site, it_mw, customer, contract, advance_rate, ltv, covenants, docs[{t, url, kind}], confidence (high/medium/low), status, rating, case (12), gpu{disc / cost range}` |
| `docs` | 199 | SEC financing documents (16 public borrowers) | `iss, cik, form (8-K 111, S-1 17, 6-K 16…), date (2023-02-13…2026-10-16), acc, ex (EX-10.1), desc, kind (Indenture & notes 68, Security & guarantee 43, Customer contract & lease 32, Amendment 31, Credit agreement 25), url, title` |
| `dock` | 90 | Watched proceedings per case | `case` (24), `proj`, `name`, `key`, `kind` (Utility commission 29, County board 21, FERC 14…), `url`, `live`, `last[{d,t,u}]` (173), `next`, `flags`, `checked`; `speed[5]` = filings flagged before press |
| `rv` | 29 (id = case) + curves CRWV (4) / ORCL | Relative value: `px, pxk, y, spr, bench, el, sae, fair, vf, pl, rec, wal, call (Fair/Cheap/Rich), why[], risk[], chg` | curves → CurveTier points |
| `comp` | 117 (fid) | Comparable new-issue spreads 2023–2026 | `fid, type, tier (ig/nd/lab/aa/orcl/back/div), close, size, ten, bp, cpn, rt, now{px, s}` ; `seg[13]` medians by type×tier |

---

## 3. `collmeta.json`: collateral / site risk notes (55,493 bytes)
A dict with 108 entries. Key `"GRP|lowercase collateral label"` (e.g. `"APLD|polaris forge 1 eln-04"`). All 26 key prefixes are aidebt `grp` values (CORZ 9, CRWV 8, APLD 7, ORCL 7…).
Record = one collateral pool / site / contract behind a group's debt. Fields: `l` location label ("Ellendale, Dickey County, ND (Polaris Forge 1)"), `lk` location kind (site 59, portfolio 30, gpus 13, unknown 4, state_only 2), `r` risk (medium 48, high 37, low 23), `c` boolean (2 true; meaning undocumented), `d[]` 2–4 bullet facts, `b` one-line rationale, `s[]` 1–3 source URLs.
No dates or as-of. The keys do not match `aiwx` keys or case ids exactly, so joining needs a curated map or fuzzy match.

## 4. `niche.json`: software-sector BDC loan marks by niche (89,600 bytes; asof 2026-10-16)
This is not AI-infra debt. It tracks AI-disruption risk for BDC software borrowers.
- `lab`: 25 niche code → label (`cx` "Customer experience & contact center", `infra` "Hosting & network infrastructure").
- `drv`: 10 niche → driver text.
- `peers`: 25 niche → listed peer tickers (130 total; `infra` has none).
- `q`: 130 tickers → `{n, px, r6, r12, hi, lo52, hi52}` (equity quotes; no date per quote).
- `n`: 25 niches → `{nb, par (USD, e.g. 3579543435.0), wm (weighted mark), chg4, breadth, rec, pri, acc, na, fe, qs [[date, wm, chg, ?, ?, nb]] 13 quarter-ends 2023-06-30…2026-06-30, top [[bkey, mark, chg, par $m, flag]] (471), ndown, nd4}`. `infra` includes `retained vantage` (100.0, $584m), `firmus supercloud`, `coreweave compute`.
- `b`: 499 bkey → `[niche, display name, description, confidence high/med]`.
Quality: there are **no source URLs at all** (0 "http"). Units are mixed (niche `par` in USD vs `top` par in $m). The `qs` columns 4–5 are undocumented.

## 5. `bevents.json`: borrower distress events (30,927 bytes; asof 2026-10-16, window 2026-06-30)
- `lift`: event-type multipliers `{bankruptcy_court 11.0, credit 5.7, layoffs 2.1, warn 1.8}`.
- `ev`: 31 bkeys → `{p0, p1 (default prob before/after), items[{d, t, l, h, u, src}]}`; 70 items dated 2026-07-07…2026-10-16; t: layoffs 28, warn 22, credit 15, bankruptcy_court 5; src: Google News RSS (research only) 43, WARN 22, CourtListener 5.
AI-relevant keys: `coreweave` (p0 0.0119 → p1 0.0235; CNBC/FT 2026-10-15 distress headlines), `oracle` (WARN notices 2026-09-14). Most others are non-AI (petsmart, belk, merck…). 22 of 31 keys are in `sectors_final`; 2 are in `niche.b`.

## 6. `sector/header_fund_agree.csv`: BDC mark agreement (5,778 bytes)
112 rows, columns `name,n,agree`. Record = one BDC/private-credit fund (e.g. `ARES CAPITAL CORP,843.0,0.5646…`). `n` 1–843 (float-formatted counts; sum 18,190), `agree` 0–1 (presumably the share of that fund's marks agreeing with peer consensus; not documented). No duplicates or blanks. 20 of the 32 `q3x.fc.funds` names match exactly.

## 7. `sector/sectors.json` and `sector/sectors_final.json`: borrower → sector (477,470 / 525,253 bytes)
Both are `{sector: {bkey: label}, how: {bkey: method}}`. `sectors.json` has 6,605 keys and `sectors_final.json` has 7,236 (a superset: +631, 24 relabelled, 87 `how` changed name→heading). There are 14 sector labels (Software & technology 1,428, Healthcare 1,409…); `how` ∈ filing 5,088 / name 1,430 / heading 718. **Use `sectors_final.json`**; `sectors.json` is an older version.
Quality: normalization collisions, e.g. `vantage` → "Chemicals, packaging & materials" while `retained vantage` → Software; `oracle vision` → Healthcare. Name-only (`how=name`) labels are the weakest.

---

## 8. What each file should seed

| Source block | types.ts objects | DB tables |
|---|---|---|
| `aidebt.rows` + `rowinfo` + `pxs` + `ids` | `InstrumentRow` (id, name=inst, type=cat/rank, sizeB=amt_m/1000, matures=mat, mardenPrice←px, yieldPct←yld/rowinfo.y, change1w←tr.w1, chainStatus←derived), `InstrumentDetail.terms` (cpn, rank, rating, backstop, cusip), `priceHistory`←`rowinfo.tr.ser` / `gs` | **instrument**, **price** (px/pxd/kind/url/lic; TRACE daily series), **party** (issuer, grp+gn), **fact** (rating, flags, why) |
| `aidebt.watch` + `watchplain` + `rowinfo.p/s` | `RiskRow` (rank, why[]←stress_flags/p, price, odds←case sim p_loss), `RiskList.note`, `WatchItem.nextCatalyst` | **event** (next_catalyst), **fact** |
| `aidebt.aiwx` + `cases.memo.chain` + `rowinfo.x` | `ChainLink[]`, `ObligationNode/Edge`, `ProjectParty`, `Payer.sites` | **party**, **obligation edge**, **contract** (link role T/M/G…) |
| `cases` (29) | `SiteSummary`/`SiteDetail` (name, place, debtB=amt_m, leadLenders←holders.banks, `materialEvents`←events, `debtExposed`←memo.instruments + sim), `ProjectFile` (parties←memo.chain, `structure.tranches`←memo.cap, security/guarantee←memo.security, checklist←kv/unknowns), `recovery`←sim, `stress`←sens/buildup, `Milestone.latestEvidence`←drivers.evidence | **site**, **instrument** (debt_row link), **holder position** (banks/funds), **event**, **fact**, **document** (evidence URLs) |
| `q3x.ctr` | `LeaseRecord` (lessee←tenant, kind, totalB←tcv_m, termYears←term, commencement←start, `CushionProjection.outsideDate`←outside), `Payer.paidBy` | **contract/lease** |
| `q3x.hold`, `q3x.ins`, `q3x.fc.pos`, `aidebt.holders`, `cases.holders` | `Holder` (name←who/fam, filing←src, filingDate←rd, parMm←par, lastMark←mark, gapPts←mark−px), `holders.count/parMm`, `markGap`, `StreamEvent 'mark'` | **holder position** |
| `q3x.fac` + `q3x.comp` + `gpu.ops.fac` | `Tranche`, `GpuCollateral` (units←gpu, balance←drawn), `CurveTier`/comps, `PlantDebt.financing` | **instrument** (facility-level; needs fid→instrument map), **document** (docs[]) |
| `q3x.uw`, `q3x.rec`, `gpu.*` | `GpuCollateral` (ltvToday←ltv, ltvByQuarter←ser, recoveryIfCustomerWalks←rec.gpu), `GpuModelDetail` (index←rent, providers←lists, resale, valueByAgeYears←resid, assumptions←life/pue) | **price** (GPU rent index), **fact** |
| `q3x.trg`, `gpu.ops`, `acct` | `RatingTrigger`, `PayerCapacity` (rev/ebitda/capex/rpo), `ExposedInstrument.ratingTrigger` | **fact**, **party** |
| `q3x.ctg`, `q3x.oa` | `ContagionResult`, `StressResult.rows`, `Payer.stressSummary` | **obligation edge**, derived **fact** |
| `q3x.cat`, `cases.events`, `q3x.dock`, `dockx`, `cases.watch` | `Alert`, `Evidence`, `DcRecord`, `Milestone.events`, `nextCatalyst` | **event**, **document** |
| `q3x.dcmap`, `q3x.site`, `q3x.heat`, `gpu.pwp`, `pw2c` | `DataCenterRow` (lat/lon, owner, tenant, facilityMw←gross_mw, itMw←it_mw_plan, financed←debt), `SiteSchedule.imagery`←s2/scenes, `servedCampus` link | **campus**, **site**, **plant** (via pw2c) |
| `q3x.docs` | `SiteSchedule.documents`, `Evidence` | **document** |
| `q3x.rv`, `q3x.rvc`, `q3x.fc` | `CurveTier`, `InstrumentDetail.marketCheck`, `fundMarks`, `nextMarksPublic` | **price** (fair, attribution), **fact** |
| `collmeta` | `ChainLink.reason`, site status text, `Payer.credit` notes | **fact** (per grp collateral) |
| `niche`, `bevents`, `sector/*` | Not part of the V1 AI-infra screens. Could feed BDC-holder context (holder NAV share, fund mark-agreement), payer distress alerts (`bevents.coreweave/oracle` → `Alert`) | **fact**, **event** (bevents items), **party** (BDC funds from fund_agree) |

## 9. Mapping notes for Sourced<T>
- Tag F: SEC / N-PORT / BDC 10-Q / FERC / state permit URLs; `memo.chain.basis="disclosed"`; `pxs.kind` fund mark / BDC mark / TRACE.
- Tag R: `basis="reported"`, news URLs, `price_kind="dealer quote"` from press.
- Tag M: everything in `sim`, `sens`, `buildup`, `rowinfo.s/p/y/sp` where `ym=1`, `q3x.fc/rec/uw/ctg/rv`, `basis="presumed"/"assumed"`, `kv.rent` "(model input)".
- `methodVersion`: none stored. Only `aiwcredit.method` (text), `paths`=20000, `q3x.fc.method`/`sha256`, and `made` timestamps exist.
- asOf: available per price (`pxd`, `pxs.date`, `tr.ser`), per evidence (`d`), per holder (`rd`), and per block (`asof`/`made`). Missing for most text facts (`kv`, `memo.security`, `collmeta`, `aidebt.rating/tenants/chain`).

## 10. Gaps against the V1 screens (not in this area's data)
1. **No structured party/entity table**: parties live as free text (`issuer`, `tenants`, `chain`, `memo.chain.name`, `aiwx` P/L tuples), with no entity ids, LEIs or parent links. `ProjectParty.entityId` and `ObligationNode.id` need a new registry.
2. **No F/R/M tag field, sourceName or methodVersion** on any value. These have to be derived.
3. **Lease outside dates** are almost all null (`q3x.ctr.outside`: 2 rough text values of 29). `CushionProjection.outsideDate`, `cushionMonths`, p10/p50/p90 rent-start and `cushionHistory` cannot be filled from this area.
4. **Price history**: only TRACE `ser`/`gs` for 169 instruments (Jan–Oct 2026; licence `personal-only`). No history for loans or private debt. There is no daily Marden price series and no `change1w` for untraded debt.
5. **Holders**: there is no per-position filing URL for N-PORT rows (6,539 of 6,713 have `url` null), and no `navHitMm`/`shareOfNav` (no fund NAV). `rd` mixes `MM/DD/YYYY` and ISO formats. Bank shares are never disclosed. `cases.holders.bdc` is always empty.
6. **fac (213 facilities) does not join to aidebt instrument ids**, and aidebt sizes are drawn amounts where fac has commitments. A curated `fid ↔ instrument id` map is needed.
7. **ChainLink status / ruleCode, ChainStatus, driver** are not stored. Only levels (0–3) and severities exist, so the fixed rules must compute them.
8. **Stress scenarios** in the spec (OpenAI funding miss, Oracle downgrade, 6-month power delay) appear only as per-case sensitivities (`sens`) and the ORCL/CRWV contagion tables. There is no portfolio-level scenario output.
9. **Ratings** are free-text strings with no agency/outlook/date structure (`RatingState` needs parsing). CDS (`Payer.kpis.cds5y`) is absent.
10. **Alerts / AlertSettings / HomeLayout / watchlists**: no user state (expected).
11. **Buildings / footprints / permits** (`Building`, `AirPermit`, `PowerUnit`) are not in this area. Only `q3x.dcmap` campus-level rows and Sentinel scenes are.
12. `niche.json`, `bevents.json` and `sector/` are about the BDC software-loan book. They do not cover the 50-instrument AI universe beyond a few keys (coreweave, oracle, retained vantage, firmus supercloud, lambda).
13. Coverage vs the spec list: Anthropic "Big Sky" appears only as `q3x.fac` `ANTH_COMPUTE_SPV_35B`, and there is no "North Star" record. CoreWeave DDTL 1.0–5.5, IREN, Nebius, Nscale, Jupiter, Frontier, Lighthouse, Abilene, Saline and Beignet are present.

## 11. Data-quality summary
- There are several price snapshots per instrument with different dates and values (`aidebt.px`, `aidebt.watch.price`, `pxs.px`, `cases.price`, `sim.price`, `q3x.rec/rv/fc`). Pick one canonical source per date.
- Two score systems disagree (`aidebt.score` vs `rowinfo.s`; only 13 of 707 equal).
- Units are mixed: `amount_usd` in USD vs `amt_m` in $m. `niche.par` is in USD and `top` par in $m. Convertible prices above 130 (up to 1416). Negative yields on 2 converts.
- Maturities are partial or non-date strings (8 rows), and coupons are free text.
- There are 2 CUSIP collisions across instrument ids and 16 NP ids with non-CUSIP suffixes.
- `dcmap.state` mixes full names and abbreviations (`Texas` 14, `TX` 5).
- Licensing: TRACE prices are `personal-only`, Silicon Data and SemiAnalysis inputs are restricted, and Google News items are "research only".
