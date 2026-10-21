# Data map: holdings-prices

Area: `fundpos.json`, `q3snap.json`, `trace/`, `aiwc/pxsrc/`, `watch/` (all subfolders), `parallax-credit-marks.html`.
Root: `/tmp/claude-0/-home-user/041c65f7-05f2-55e2-9ba6-73f6e02ba8df/scratchpad/data` (extracted from `marden_data_2026-10-20.zip`).
Profiling scripts: `/tmp/claude-0/-home-user/041c65f7-05f2-55e2-9ba6-73f6e02ba8df/scratchpad/datamap/scripts/hp/`. The JSON blocks embedded in the HTML were extracted to `/tmp/claude-0/-home-user/041c65f7-05f2-55e2-9ba6-73f6e02ba8df/scratchpad/datamap/hp_extract/` (one file per `<script type="application/json" id=...>`).

No file in this area has an explicit F/R/M tag field. Tags have to come from the source kind: SEC filing, N-PORT or FINRA TRACE gives F; a press-relayed dealer quote gives R; model output gives M. Where a file has a `kind` field (for example `"fund mark"`, `"BDC mark"`, `"TRACE last sale"`, `"dealer quote"`, `"exchange last"`, `"issue price"`, `"none"`), it maps directly to a tag.

---

## 0. Identifier systems and joins (read this first)

| ID | Looks like | Where it appears | Joins to |
|---|---|---|---|
| BDC CIK | `"1287750"` | `fundpos.f` keys, `q3snap.*.cik`, `watch/*` cik, `watch/ref_meta.universe` | `ref_meta.fund_idx[cik]` gives `fi` |
| Fund index `fi` / `f` | int 0–177 | `q3snap` (`fi`), `watch/out/db/*` (`fi`), `tells_db` (`fi`), HTML `data.funds[i]`, `data.facilities[].H[].f`, `q3cal.f`, `realnav.nav` keys, `marklag.f` keys | `ref_meta.fund_idx` (verified: all 178 `data.funds` CIKs, parsed from the filing URL, map to the same index) |
| Borrower key `bkey` | `"coreweave financing"`, `"retained vantage"` | `fundpos` col 0, `q3snap` alerts, `calls_r1.positions`, HTML `facilities[20]`, `borrowers[14]`, `aiwatch.bdc` | Normalized borrower name, used across all BDC files. Facility key = `bkey\|lien\|spread\|YYYY-MM` (e.g. `"fpg\|1L\|0.05\|2029-06"`, used in `opps`, `ew`, `susp`, `stray`) |
| Marden instrument ID | `EX-CRWV-106`, `NP-BE-093712AK3`, `FWP-CNP-6.4-2058`, `MAN-QTS-...`, `8K-ONCOR-5.8-2055`, `XB-...` | `trace/match.json`, `trace/trace_rows.json`, `trace/spreads.json`, `watch/bondq_list.json`, `watch/*/bondq.json`, `aiwc/pxsrc/targets.json`, `out1.json`, `out2.json` | Same IDs as `aidebt.json rows[][0]` (outside this area). The prefix seems to name the source (EX = exhibit, NP = N-PORT, FWP, 8K, MAN = manual); the middle token is the issuer group (CRWV, APLD, ORCL...) |
| CUSIP | `21873SAD0`; RegS `U2069EAB6` | `trace/cusips.json`, `match.json` (ID to 1–4 CUSIPs), `eod*.json` keys, `secs*.json`, `aiwc/pxsrc/agg_<CUSIP>.json` filenames, w2 rows `cusip` | `match.json` maps instrument ID to CUSIPs. Loans often carry `000000000` / `999999999` / `N/A` and only an `other` ID (LoanX such as `LX233753`, Bloomberg `BL5672813`, `B8AFB91T2`) |
| ISIN | `US21873SAD09` | `watch/bondq_list.json`, `bondq.json`, w2 rows `isin` | ISIN = `US` + CUSIP + check digit |
| Case ID | `APLD_PF1`, `JUPITER`, `CRWV_DDTL3`, `XAI_VCI` | `watch/*/aiwlive.json` cases, `aiwc/pxsrc/case_px.json`, `targets.project_modeled[].case`, `out2.json` (`case:APLD_PF1`, `case:ABILENE#2`) | Maps to an instrument ID through `targets.project_modeled[].debt_row` (e.g. `APLD_PF1 → EX-APLD-010`). Project or site financing level. No site slug in this area |
| aichain sheet / cap | `APLD`, `APLD:0`, `ABILENE:0+1` | HTML `aichain.issuers` keys, `edges[].sheet` + `debt[]` (cap indexes), `tenants[].direct.keys`, `aicov.dc[].id`, `aicov.gpu[].id` | `issuers[sheet].caps[i]`. Caps carry **no** EX instrument ID, so they join to `targets`/`out*` only by name, coupon and maturity |
| SEC accession `adsh` | `0001213900-26-107187` | `watch/state.json`, `filings_q3n.csv`, `soi_q3n.csv`, `tells` ids, `nvda/seen.json`, w2 rows `adsh` | EDGAR filing |

---

## 1. `fundpos.json` (2,287,275 B, JSON)

- **What it is:** one row per BDC schedule-of-investments debt position (one fund, one borrower, one tranche) across 178 BDCs. The HTML app loads it with `fetch('fundpos.json')`.
- **Shape:** `{cols:[13 names], niche:[597 str], sec:[14 str], f:{<CIK>: [[13 values], ...]}}`.
  - `cols` = `bkey, b, lien, sp_bp, mat, par_k, m, chg, na, pik, niche, sec, fv_k`.
  - `niche` and `sec` are lookup lists; row cols `niche`/`sec` are indexes into them (`-1` = none).
- **Counts:** 178 fund keys (CIK 1495584 has 0 rows), 26,491 rows, 7,776 distinct `bkey`, 7,800 distinct `b`. Largest fund: CIK 1803498 (Blackstone Private Credit Fund) with 793 rows.
- **Fields and samples:**
  - `bkey` "coreweave financing"; `b` "CoreWeave Financing DDTL V LLC"
  - `lien` 1L 25,604 / 2L 400 / Sub 369 / Unsec 118
  - `sp_bp` spread in bp (450; null 2,652)
  - `mat` "YYYY-MM" (2018-03..2061-05; null 309)
  - `par_k` and `fv_k` in **$ thousands** (CoreWeave DDTL V: par 5319, fv 5616)
  - `m` mark, % of par (105.6). `fv_k ≈ par_k × m / 100`
  - `chg` points vs the prior quarter (null 7,187)
  - `na` non-accrual 0/1 (449 = 1); `pik` 0/1 (2,757 = 1)
- **As-of:** not stored in the file. It is the **30 Jun 2026** schedule, and `chg` is vs 31 Mar 2026. This is inferred because the Retained Vantage rows (m 100, chg 5.1) match the q3snap replay "Mar 31 → Jun 30" alerts (m2 94.9 → m3 100) and HTML `data.summary.asof = 2026-06-30`. No filing URL or filed date per row: join CIK → `ref_meta.fund_idx` → HTML `data.funds[fi]` (`url`, `filed`).
- **AI-relevant rows (exact):**
  - CoreWeave Compute Acquisition (1L, S+600 2029-05 / S+962 2028-07, marks 99–103.5)
  - CoreWeave Financing DDTL V LLC (S+450 2031-11; 7 funds; 102.2–106.0)
  - CoreWeave (1L 2030-08, Unsec 2032-07)
  - Crusoe Energy Systems (2029-11)
  - Galaxy Helios LLC (S+475 2028-08; 12,750k + 90,590k par; 97.6–98.7)
  - Hyscaleix Data Centers Holdings
  - Lambda Systems
  - North America Data Center Acquisition
  - Oracle Corp (S+300 2026-09)
  - QTS Project Ram (S+225 2028-05; 47,015k)
  - Retained Vantage Data Centers Intermediate (Sub 2031-12; 185,000k / 195,900k / 203,549k at 100.0)
  - Switch BBF
  - Together Computer
- **Quality:**
  - 10 exact duplicate rows inside funds.
  - 36 rows with `m` > 150 or < 0, max m = 39000.9 (unit errors). Some m = 0.
  - Name collisions: "Vantage Bidco", "Oracle Vision Bidco", "Meta Buyer" and "Galaxy Universal" are **not** the AI names.
  - `sp_bp` is null for fixed-rate and some floating rows.
  - The 26,491 rows are a subset of the HTML's 56,756 positions (HTML total FV $545.9B vs fundpos ∑fv $490.7B).
- **Seeds:**
  - `holder position` rows (fund, amount, mark, mark date = 2026-06-30, filing via fi): Holder.parMm (par_k/1000), Holder.lastMark (m), Holder.filing = '10-Q' (10-K for some).
  - `party` (BDC funds; borrowers).
  - InstrumentDetail.fundMarks (low/high across funds per bkey/tranche).
  - RiskRow.fundMark, InstrumentRow.markGap input.
  - Tag F (filed schedule).

## 2. `q3snap.json` (414,770 B, JSON)

- **What it is:** a snapshot of the "Q3 marks" monitor that compares each BDC's new filing to its prior-quarter marks. `taken_at` "2026-10-16T02:27:35Z". The HTML app loads it when its `db` capability is missing.
- **Shape:** `{live:{status, funds[], alerts[], nowcast[], ncsnap, ncfiled}, replay:{status, funds[], alerts[], nowcast[], ncsnap, ncfiled}, taken_at}`.
- **`live`** (Q3 2026 season):
  - `status`: `calls_sha`, `nuniverse` 180, `nfiled` 1, `nalerts` 3, `ncut` 3, `nna` 0, `updated` 2026-10-16T02:27:35Z.
  - `funds` (1 row): Saratoga, CIK 1377936, fi 138, filed "20261006", `url` (10-Q), `n` 76, `pts` −1.18, `usd` −11,603,649, `fc_*` forecast, `early` true.
  - `alerts` (3 cuts: Exigo 72.8→36.2, Madison Logic, JOBVITE).
  - `nowcast` (170 rows, fund-level forecast NAV change): `cik, fi, cov, ncov, n, pc, now, fc, usd, top[]`.
- **`replay`** ("replay Mar 31 -> Jun 30"):
  - 166 funds (filed 202607–202608).
  - 887 alerts. `t` cut 545 / up 248 / na 94. `tt` TL 842, DD 29, LO 11, FO 3, NEW 2.
  - 120 nowcast rows (with `act` actual).
  - `status.rt` / `rth` / `ncb`: backtest stats for 12 quarters, Sep 2023–Jun 2026. For example `rth.sum` n 1346, hit 1106, pct 82.2, lead 6 days.
- **Alert fields:**
  - Identity: `id` (16-hex), `t`, `fi`, `bkey`, `b`, `lien`, `tt`.
  - Marks: `m2` prior mark, `m3` new mark, `chg`.
  - Size: `par` (**USD units**, not $k), `imp` (USD value impact).
  - Status: `na`, `amended`, `call` (always null), `filed` (YYYYMMDD).
  - Peers: `peers[[fi, prior mark, new mark or null if not filed, ?, par $m (inferred), filed]]`, `np`, `nwait`, `nhigh`, `hipar`.
  - Sample AI alert: Retained Vantage `up` +5.1, par 185,000,000, fi 16.
- **nowcast `top`:** `[[bkey, name, lien, mark, chg, n?, par]]`.
- **Quality:**
  - Units differ from fundpos (USD vs $k).
  - Dates are YYYYMMDD strings here vs ISO elsewhere.
  - `call` is always null.
  - `fc` is null on 18 live rows.
- **Seeds:**
  - `event` (type price/mark: "fund mark published / cut 5+ pts", with filing URL via `url` in `watch/out/alerts.json`).
  - StreamEvent `mark`, Alert (`price_or_mark`), Holder lastMark update.
  - Backtest stats (`rth`) for the proof-before-selling backtest.
  - Tag F for alerts; M for `nowcast`/`fc_*`.

---

## 3. `trace/` (12 files): FINRA TRACE bond prices

| File | Size | Shape | Records | Notes |
|---|---|---|---|---|
| `cusips.json` | 2,691 | `[cusip]` | 207 | Universe of CUSIPs priced. All 207 appear in `match.json`; 201 appear in `secs*.json` |
| `match.json` | 6,901 | `{instrumentId: [cusip,...]}` | 170 IDs → 267 CUSIPs | Join table. e.g. `EX-CRWV-103: [21873SAG3, U2069EAD2, U2069EAC4]`. `8K-ONCOR-5.8-2055` has no trace row |
| `names.json` | 5,063 | `[issuerName]` | 184 | Issuer names searched in FINRA (e.g. "APLD COMPUTECO 2 LLC", "BEIGNET INVESTOR LLC", "BX 2026-VLT9") |
| `names2.json` | 5,362 | `[issuerName]` | 203 | Second pass with alternate spellings ("ALIGNED DATA CTRS ISSUER LLC") |
| `secs.json` | 158,787 | `{issuerName: [security]}` | 184 keys, 30 non-empty, 383 securities | FINRA security master. Fields: `cusip, is144A, issueSymbolIdentifier, issuerName, couponRate (str), maturityDate, lastTradeDate, lastSalePrice (str), lastSaleYield, isConvertible, moodysRating, standardAndPoorsRating, productSubTypeCode (CORP/ELN), couponType (FXPV/FXZC/FRFF/FROT)` |
| `secs2.json` | 114,015 | same | 203 keys, 18 non-empty, 269 | Same as secs.json, from the `names2` pass |
| `sec_search.json` | 7,973 | `{issuer: {status, body:<JSON string>}}` | 4 (COREWEAVE INC 12 recs, APLD COMPUTECO LLC 2, GALAXY HELIOS DATA CENTERS II 0, CORE SCIENTIFIC FINANCE 0) | Raw API responses; body is double-encoded JSON |
| `eod.json` | 3,278,720 | `{cusip: {eod:[{cusip, tradeDate, lastSaleYield, productType, lastSalePrice}], sec:[security]}}` | 207 CUSIPs, 26,700 daily rows, 2026-01-02 → 2026-10-19 | End-of-day TRACE. `productType` CA 26,680 / SP 20. **749 rows with `lastSalePrice` 0 and null yield** (no-trade days). No duplicate dates |
| `eod_new71.json` | 859,982 | same | 71 CUSIPs, 6,965 rows, 2026-05-01 → 2026-10-15 | A refresh of a 71-CUSIP subset (all 71 also in eod.json). 65 zero-price rows |
| `trace_rows.json` | 431,681 | `{instrumentId: {cusip, d, px, y, n, ser:[[date, px, yld]], jul, w1, m1, rating}}` | 169 IDs. Prefixes: EX 98, NP 53, FWP 12, MAN 4, XB 1, 8K 1. Issuer groups ORCL 53, EQIX 14, CEG 12, NRG 10, NBIS 8, CRWV 7, IREN 6... | Latest print per instrument (`d` 2026-02-12..2026-10-20) plus up to 90 daily points (ser 2026-01-02..2026-10-19). `jul`/`w1`/`m1` are reference prices (July, 1 week, 1 month ago). `rating` is a free string ("Baa2 / BBB-", "" in 40 rows). Sample: `EX-CRWV-106` 21873SAD0, d 2026-10-19, px 79.23, y 6.60, n 197, rating "B" |
| `spreads.json` | 734,250 | `{made, last, ust_last, note, b:{id: {c, mat, d, px, y, ust, s, chg1w, chg1m, chg_jun, ser:[[date, px, y, ust, s]]}}}` | 121 IDs (all in trace_rows) | G-spread in bp vs interpolated UST par yield (`note` states the method). `made` 2026-10-20T14:55Z, `last` 2026-10-19. Sample: EX-CRWV-102 px 81.75, y 14.91, s 1000, chg1w +121, chg_jun +491. Spread range 45–1052 bp |
| `refresh_state.json` | 75 | `{t, when, last}` | 1 | Last refresh 2026-10-20T10:55, data through 2026-10-19 |

- **Quality:**
  - Convertibles print far above par (e.g. EX-NBIS-196 486.5; NP-BE-093712AK3 1388.6, Bloom converts). Exclude them from spread or curve fits.
  - Zero-price no-trade rows.
  - `couponRate` and `lastSalePrice` are strings in `secs*`.
  - Placeholder maturity `3999-12-31` in secs.
  - Ratings are mixed Moody's/S&P free text.
  - Many issuers are utilities or REITs (AEP, CEG, EQIX, DLR, NRG), not AI SPVs.
- **Seeds:**
  - `price` (daily observed bond price, yield, spread; tag F, sourceName "FINRA TRACE").
  - InstrumentDetail.priceHistory (bonds only), InstrumentRow.yieldPct, change1w (`chg1w` in bp, price via `w1`).
  - CurveTier points (years from `mat`, `y`/`s`).
  - Instrument terms (coupon, maturity, 144A flag, ratings: `terms`).
  - marketCheck. StreamEvent `price`.

---

## 4. `aiwc/pxsrc/`: observed prices, N-PORT holders and price-source picks

### 4a. `agg_<CUSIP>.json` (13 files, 1.1 KB–131 KB, JSON array)

- **Shape:** each row is `{per, fund, reg, bal, val, px, units, url, filed, title, cpn, mat}`. The CUSIP is only in the filename.
- **What a row is:** one N-PORT holding of that CUSIP by one fund series. `bal` is par in USD, `val` is fair value in USD, `px` = val/bal×100, `units` PA (par) or NS (shares, px null), `url` is the N-PORT `primary_doc.xml`.
- **Files:**

| CUSIP | Instrument | Rows | Funds | Periods | Filed |
|---|---|---|---|---|---|
| 07367AAA7 | Beacon Point DC (Hut 8) 6.129% 2042 | 85 | 83 | 2026-07-31 | 09-18..10-08 |
| 15201EAL8 | Centersquare Issuer 5.2% 2055 | 3 | 3 | | |
| 17253NAA5 | Cipher Compute 7.125% 2030 | 118 | 114 | 2025-11..2026-08 | |
| 21873SAG3 | CRWV 9.75% 2031 | 334 | 326 | Apr–Jul 2026 | |
| 21873SAH1 | CRWV 1.75% conv 2032 | 19 | | | |
| 268332AB9 | EDI ABS Issuer 4.55% | 10 | | | |
| 28002AAA4 | Edged Compute 7.5% 2031 | 157 | | | |
| 36317GAC0 | Galaxy Digital 0.5% 2031 | 14 | | | |
| 36322DAA4 | Galaxy Helios II 9.875% 2031 | 71 | | | |
| 46270CAL3 | IREN 1% 2033 | 19 | | | |
| 68389XBX2 | ORCL 3.6% 2050 | 31 | | | |
| 74941YAA0 | RD Michigan Property Owner I (Saline) | 346 | 340 | | |
| 86800UAB0 | SMCI 3.5% 2029 | 6 | | | |

- **Quality:**
  - Mixed periods in one file (different fund fiscal months).
  - Title spelling varies ("RD Michigan" vs "Road Michigan").
  - A few duplicates.
  - NS rows have null px.
  - `cpn` is a string with varying precision.

### 4b. `w2/res/rows_<case>.json` (23 files) and `w2/rows_wulf.json`

- **Shape:** raw N-PORT holding records. Each row has the N-PORT XML fields `name, title, cusip, isin?, other?, balance (str), units, curCd, valUSD (str), pctVal (str, % of fund net assets), assetCat (DBT/LON/EC/EP/ABS-*), fairValLevel, maturityDt, couponKind, annualizedRt, isDefault`, plus parsed `px, bal, val, fund, period, filer (with CIK), adsh, url, filed`. 3,810 rows in total.
- **Files:** rows/funds per file
  - apld 750/383 (3 notes, 03772CAA1 / 00202DAA5 / 03790NAA5)
  - beacon 257
  - beignet 326 (076912AA2)
  - blackpearl 298
  - cipher 313
  - corz 230
  - crusoe 20
  - crwv 267 (13 CUSIPs; DDTL loans with LoanX `other`)
  - flash 181
  - galaxy 138
  - hut 218
  - iren 2 (IE US Hardware 3 LLC funded and unfunded, PGIM)
  - lambda 7
  - lohrasp 19
  - qts 90
  - saline 23
  - saline2 117
  - sopa 105
  - stingray 203
  - vantage 27 (VANTAGE ORACLE TX/WI funded and unfunded)
  - vci 96
  - wulf 123
  - **empty `[]`:** `rows_ccac7.json`, `rows_helios1.json`
- **Periods:** 2026-06-30 / 07-31 / 08-31. Filed 2026-08-06 → 2026-10-15.
- **Quality, false-positive name matches:**
  - `rows_saline.json` is Saline, MI school and Saline County KS municipal bonds, not the Related Digital project. `rows_saline2` holds the real one.
  - `rows_vantage` includes AAdvantage Loyalty, Advantage Solutions, Vantage Germany ABS.
  - `rows_crwv` includes "Options Clearing Corp." equity options and convertible preferred.
  - Loans have CUSIP `000000000` / `999999999` / `N/A`.
  - Unfunded commitments are separate rows at px 100.
  - `annualizedRt` units vary ("7.75000000" vs "0.07750000").

**Seeds for 4a and 4b:**
- `holder position` (Holder: name = fund, filing 'N-PORT', filingUrl = url, filingDate = filed, parMm = bal/1e6, lastMark = px, shareOfNav = pctVal).
- InstrumentDetail.holders, fundMarks low/high/date.
- `party` (fund families via `reg`/`filer` CIK).
- Tag F.

### 4c. `case_px.json` (7,440 B)

- **Shape:** `{caseId: {px, date, kind, label, url}}`, 26 cases.
- **kind counts:** TRACE last sale 16, fund mark 5, BDC mark 2, dealer quote 1, none 2. ABILENE and CRWV_DDTL3 are "No public price".
- **Sample:** `JUPITER` 90.0, 2026-09-18, dealer quote, "Dealer quotes 89–91 · FT via Reuters". `APLD_PF1` 102.875 TRACE, 2026-10-16, FINRA URL.

### 4d. `targets.json` (34,664 B)

- **`instruments[46]`:**
  - Fields: `id` (instrument ID), `issuer`, `instrument`, `coupon` (string), `maturity` (YYYY-MM or null ×7), `amt_m` (USD m; null ×6), `rank`, `price` (null ×11), `price_date`, `filing_src[]` (0–3 URLs).
  - Sample: `EX-JUPITER-190`, "Project Jupiter SPV (STACK / BorderPlex)", ~SOFR+250bp, 18000, Secured (SPV), 90.0, 2026-09-19.
- **`project_modeled[30]`:** `case, debt_row (instrument ID; null ×1), name, current_price, kind, date, note, src`. This is the case → instrument crosswalk.

### 4e. `out1.json` (23 IDs) and `out2.json` (53 keys)

- **Shape:** `{id: {px, date, kind, label, url, n_funds, cpn, cpn_src, mat, mat_src, lic, note}}`.
- Together they cover all 46 targets with no overlap. `out2` also has 30 `case:<CASE>` keys (including `#2`, `#fixed`, `#existing` sub-tranches).
- **kind counts:** fund mark 52, BDC mark 6, dealer quote 5, exchange last 2, none 11.
- **`lic`:** "ok" 74, "research-only" 2.
- **Sample:** `EX-FIRMUS-143` 97.96, BDC mark, BCRED 10-Q 30 Jun, n_funds 2, cpn "SOFR+5.13% (8.77% at 6/30)".

**Quality across 4c–4e:** the price for the same case or instrument differs by file and source:
- CIFR_BARBER: 102.065 fund mark (targets) vs 99.03 TRACE (case_px) vs 101.239 N-PORT (out2).
- SALINE: 98.75 issue price vs 86.5 TRACE vs 96.886 N-PORT.
- XAI_VCI: 102.74 BDC 6/30 vs 103.498 N-PORT 7/31.
- JUPITER date: 09-18 vs 09-19.

Each value needs its own as-of and kind, and none should be treated as "the" price.

**Seeds:** `price` (observed reference price per instrument with source and date; kind → tag), InstrumentDetail.marketCheck, dealerQuote (SiteDetail.debtExposed.dealerQuote), `instrument` terms (coupon, maturity, size, rank), `document` (filing_src URLs), and the case → instrument crosswalk.

---

## 5. `watch/`: live pipeline state and DB collection dumps

### 5a. Reference and config

| File | Size | Shape / records | Content |
|---|---|---|---|
| `ref_meta.json` | 40,833 | `universe[180][cik,name]`, `fund_idx{cik:fi}` (178), `name_cik{name:cik}` (185), `fund_calls[153]`, `calls_sha`, `frozen_day` "20261015" | **Master BDC list and the fi join.** Universe CIKs 2077250 (Stone Point Credit Income – Select) and 2107057 (TCW Specialty Lending IX) have no fi and no fundpos rows |
| `nontraded.json` | 4,750 | `[[name, cik, usd]]`, 79 | Non-traded BDCs. The third value is an unlabeled USD amount; it does **not** equal HTML fund FV (AGL 1.508B vs 1.227B) |
| `marklag_funds.json` | 11,532 | 52 × `{cik, n, first, late2, avglag, prem, par, fund, manager, fund_type, score}` | Per-fund "marks late" score (share of cuts the fund made first, share 2+ quarters late, average lag in quarters) |
| `marklag_mgr.json` | 2,303 | 21 × `{manager, n, funds, first, late2, avglag, score}` | Same, by manager |
| `fx.json` | 1,194 | `{CCY: {Q2:[rate,"2026-06-30"], Q1:[rate,"2026-03-31"]}}`, 11 currencies | FX for non-USD positions |
| `aliases.json` | 28 | `{"rep seko":"seko global"}` | bkey alias |
| `splits.json` | 54 | `{regex: bkey}` | bkey split rule |
| `nvda/seen.json` | 34 | `{seen:[adsh]}` | NVIDIA filing already processed |
| `bondq_list.json` | 17,726 | `[[group, ISIN, name, instrumentId]]`, 163 | Bond watch list for Frankfurt quotes. Groups: ORCL 52, EQIX 26, CEG 10, AEP 7, DTE 7, XCEL 6, CRWV 5, NBIS 4, DLR 4... ISIN XS2941363637 appears twice |
| `filings_q3n.csv` | 208 | `adsh,cik,name,form,period,filed,url`, 1 row | Saratoga 10-Q, period 20260831, filed 20261006 |
| `soi_q3n.csv` | 244 | `adsh,cik,name,per,status,reported,raw_sum,kept_sum,n,subtotals_dropped,cut_at,par_rows,par_fallback,coverage`, 1 row | Parse QA (coverage 97.6%) |
| `state.json` | 15,406 | `filings{adsh: {...status, err, npos}}` (1), `runs[64]` (2026-10-16T01:10Z..2026-10-20T17:18Z), `tells_since` "2026-10-01", `tfil{adsh: {cik, form, date, doc, status}}` (89: 8-K 86, 8-K/A 2, 10-Q 1; 2026-10-01..10-20) | Pipeline state. The Saratoga filing has status "parsed" but err "ValueError('no positions')" with npos 142, which is contradictory |
| `calls_r1.json` | 772,195 | `{target, made_from, model, supersedes, supersedes_sha256, frozen_at, reason, positions[4175], funds[153], sha256}` | **Model forecast (M):** Q3 2026 (2026-09-30) mark-cut calls made from Jun 30 filings, frozen 2026-10-15T23:50Z, "forward marks v1 (gradient boosting)". Positions: `fund, bkey, borrower, lien, mark, p_cut5 (%), exp_change (pts), par (USD)`. `funds` matches `ref_meta.fund_calls`. AI rows: CoreWeave Financing DDTL V (105.6–106.0, p_cut5 16–32%), plus false positives Vantage Bidco and Oracle Vision Bidco |
| `nowcast_bt.json`, `rt_hist.json`, `rt_up.json` | 2.3 KB, 1.5 KB, 1.7 KB | `{by:[12 quarters], sum}` | Backtests: nowcast correlation; read-through hit rate (cuts, sum n 1346 hit 82.2%, median lead 6 days); upgrades (n 242, 92.1%). `rt_up` quarter labels are inconsistent ("Q3_23"... then bare "Q3", "Q4") |
| `tells_db.json` | 11,824 | 23 × `{id (adsh:cat), cik, fi, date, form, cat, sev, s (quoted sentence), url}` | BDC stress "tells" from SEC filings, 2026-03-03..2026-09-23. Categories: merger 9, facility 7, review 3, ric 1, feewaiver 1, adviser_debt 1, liquidity 1. Identical to `out/db/tells.json` rows |

### 5b. DB collection dumps: `out/`, `out/db/`, `out/dbread/q3/`, `dbread/q3/`, `vers/q3/`

The same collections appear as snapshots taken at different times. `out/*.json` are bare arrays; `db`, `dbread` and `vers` wrap them as `{rows:[...]}` or keep them as objects. These are the rows the HTML app reads through `claude.use('db')` (collections `q3`, `q3replay`, live).

| Collection | Snapshots (asof) | Shape | Content |
|---|---|---|---|
| `status` | out (2026-10-20T17:18Z), out/db (same), out/dbread/q3 (10-16T15:03), dbread/q3 (10-18T19:49), vers/q3 (10-19T12:24) | same as q3snap.live.status | Identical values apart from `updated` |
| `funds` | out, out/db, out/dbread/q3, dbread/q3 | 1 row (Saratoga) | Identical across copies |
| `alerts` / `alerts_0` | out/alerts.json (array, includes `cik`, `url`); `alerts_0` in out/db, out/dbread/q3, dbread/q3 (`{rows, i:0}`, no cik or url) | 3 alerts | Same 3 Saratoga cuts |
| `nowcast` | 4 identical copies | 170 rows | = q3snap.live.nowcast |
| `tells` | 4 identical copies | 23 rows | = tells_db.json |
| `bondq` | dbread/q3 (10-18T19:51Z), vers/q3 (10-19T12:26Z), out/db (10-20T17:20Z); 39–56 KB | `{asof, src:"Börse Frankfurt price fixings (indicative; thin trading)", q:[163 × {g, id, isin, n, last, prev, chg (%), ts, hi52, lo52, tpx?, td?, odd?}]}` | **Daily indicative bond quotes** joined to instrument IDs. `tpx`/`td` (99 rows) are a TRACE price and date. Sample: EX-CRWV-102 last 80.695, tpx 81.75, 2026-10-20. Quality: `odd:true` on EX-NBIS-199 (tpx 133.1 vs last 94.4). EX-WULF-371 tpx 165.7 vs last 76.4 is a mismatch with no flag. `out/bondq_seen.json` (10 `ISIN\|ts`) is a dedupe log |
| `aiwlive` | dbread/q3 (10-18, 17 cases, 105 events), vers/q3 (10-19, 17, 107), out/db (10-20, 20 cases, 122 events) | `{asof, cases:{caseId:{events:[{d, t, u, src, sev, drv, cls}]}}, aifc:{made, sha, filed[], n, mae_fc, mae_flat, dir_hit, dir_n}}` | **News, docket and court events per AI financing case**, 2026-09-26..2026-10-20. `drv`: other 103, power 7, construction 6, tenant 4, permits 2. `cls`: Force majeure, Tenant pull-out, Pipeline or fuel supply, Court case, Financing or rating, Moratorium, Large-load tariff. `sev` −1..3. Out/db adds CIFR_STINGRAY, CLOUDHQ, DATABANK. Quality: mostly generic news. **`src` is mislabeled** (CRWV_DDTL3: DCD articles labeled "U.S. District Court, District of New Jersey"). The same headline repeats across cases (11). Google News redirect URLs. Non-English sources (富途牛牛) |
| `power` | out/dbread/q3 (10-16), dbread/q3 (10-18), vers/q3 (10-19), out/db (10-20T13:20) | `{asof, feed:[~150 × {k, a, d, no, t, u, p, m[], f, mw, cov}]}` | **Power and docket feed:** ferc_filing 74, pjm_queue_recent_withdrawal 28, ercot_gis_cancellation 26, puct_filing 7, ferc_issuance 5, ercot_large_load_doc 5, spp_* 4. Agencies FERC 80 / ERCOT 31 / PJM 28 / PUCT 7 / SPP 4. Dates 2026-09-25..10-20. `p` = node or plant ID (N-DOM-GS5, N-GREENCHILE, N-OH-FLAGSHIP, N-ATC-OZAUKEE...; 29/150 set; joins to `power.json`, outside this area). `mw` is always null even when the MW is in `t`. `cov` 0/1 |
| `pulse` | out/dbread/q3 (10-16), dbread/q3 (10-18), vers/q3 (10-19), out/db (10-20T17:18Z) | `{asof, latest{22 metrics}, windows{metric:{1d,7d,14d,30d:{change,z}}}, risk{score, band, active, components[8]}, sources{metric:url}, meta{metrics{label, unit, group, dir}}, gpu_decline_6m, gpu_decline_12m, gpu_decline_detail, h100_contract_1y[[YYYY-MM, lo, hi]], flags[], h100[[YYYY-MM, $/hr]], nhist, first}` | **GPU, memory and compute price pulse.** h100_hr 1.99 (RunPod), vast_h100_hr 2.123, h200_hr 3.59, h100_index_sa 3.35 (SemiAnalysis), h100_ondemand_gd 3.65, h100_sdh100rt 2.84; DDR/NAND spot; OpenRouter token prices; AWS spot. `h100` monthly index 2023-10 (7.80) → 2026-10 (3.316). `h100_contract_1y` 2023-01..2026-08 (2026-04 null). `gpu_decline_12m` −19.8 (the sign convention is confusing: since_start shows +24.26 while the price fell). History is only 5 days (`nhist` 5, `first` 2026-10-16), so 7d/14d/30d windows are null. Flags show RunPod H200 +618% in 1 day (0.5 → 3.59), which is noise |

**Seeds for watch/:**
- `holder`/`party` (BDC universe, names, types, managers).
- `event`: aiwlive (Evidence and DcRecord with sourceName/sourceUrl), power feed (power/permit events, PowerPlant history), tells (holder-side events), alerts (mark events).
- `price`: bondq daily quotes, tag R/F (indicative exchange fixings, so arguably R), and pulse (GPU index).
- GpuModelDetail.index / GpuProviderPrice (partial).
- StreamEvent `pipeline` (status/runs).
- RiskList.earlierCalls / backtest (rt_hist, rt_up, nowcast_bt).
- Fund-side mark-lag scores for the "BDC shorts" buyer.

---

## 6. `parallax-credit-marks.html` (16,771,045 B; 16,765,650 chars)

**Page:**
- A single-page app titled **"First Cut"** (BDC marks and private-credit monitor plus an AI-infra section).
- Markup: 80 `<table>`, 177 `<tr>`, 3 `<style>`, 19 `<script>`, no base64 images.
- External: Google Fonts, `<script src="aiwc.js">`. **aiwc.js is not in the zip.**
- The inline app JS (477 KB) does `fetch('fundpos.json' | 'q3snap.json' | 'aidebt.json' | 'bevents.json' | 'collmeta.json' | 'niche.json' | 'power.json')` and `claude.use('db')` (collections `q3`, `q3replay`, live), plus a download of `parallax-q3-2026-calls.json` (not present).
- The JS also holds an inline fallback bond-quote seed (EX-CORZ-067 etc., 65 EX IDs, ts 2026-10-16).

**Embedded JSON blocks (`<script type="application/json" id=...>`).** Column names for the positional arrays come from the inline JS (`D.facilities.map(f=>({borrower:f[0],...}))`).

| id | Size | Structure | What it is | AI relevance |
|---|---|---|---|---|
| `data` | 15,061,293 | `{summary, funds[178], facilities[4867], borrowers[7777], scorecard[66], alerts[95], changes[1795], opps[845], bench, lenders, cbase, ew[2270], sig, sect, bsec{5957}, lh{4584}, fstat{178}, integ, badpik, restr, flows, secr[130], bnews{10}, fwd, susp{27}, _npf[846], _rfn[275], _unres[27], _recovered, stray, namodel, basket}` | The full BDC marks dataset as of **2026-06-30** (prior 2026-03-31). summary: 178 funds, 98 managers, 56,756 positions, FV $545.86B, 7,832 borrowers, 4,867 multi-lender facilities, nport_funds 1,964, nport_borrowers 2,354 | Holders and marks for AI borrowers (CoreWeave Compute Acquisition, DDTL V, Retained Vantage, Galaxy Helios, Firmus, QTS Project Ram, Together, Crusoe, Hyscaleix, Oracle) |
| `aichain` | 319,226 | `{asOf "2026-10-14", tenants[16], edges[85], issuers{26}}` | **The AI obligation chain.** See below | Core |
| `aiwatch` | 117,441 | `{proj[80], eq{29}, eqshow[29], eqrole{29}, eqasof "2026-10-15", bdc[12], bdcwhat{12}}` | AI lease/project watch and equity prices | Core |
| `aicov` | 19,941 | `{asOf "2026-10-14", params{11}, gpuPresets[3], dc[18], gpu[3], gpuNoData[6], sources[9], dcPresets[2]}` | Recovery and coverage model inputs | Core (recovery) |
| `pulse` | 8,399 | same as watch pulse (asof 2026-10-16T13:09Z) | GPU and memory pulse (older snapshot than `watch/out/db/pulse.json`) | GPU |
| `livenav` | 79,243 | `{asof_marks 2026-06-30, asof_market 2026-10-15, sample_days[16], market[16][date, ...4 factors], factors_now{BKLN_pct, BDC_TR_pct, IGV_pct}, oos{12}, per_band[5], funds[159]{i, name, type, dnav_pct, dnav_usd, carry_pct, market_pct, software_pct, series[16], drivers[[b, lien, mark, chg, ...]]}, n_positions 17066, par 3.998e11}` | Model (M): estimated NAV drift since 6/30 per BDC | Fund side |
| `realnav` | 13,128 | `{nav{fi:[nav_usd, nav_per_share, date]}, px{ticker:{p, d}}, pxasof 2026-10-15, x2{fi:[...4]}}` | BDC NAV (F, 2026-06-30) and listed-BDC share prices | **Holder.shareOfNav denominator** for BDC holders |
| `tenderstate` | 49,089 | `{checked_at 2026-10-16T20:49Z, ciks{78}, summary{42 cik:{fund, expired, filed, acc, requested_pct, fill_pct, prorated, cap_pct, est, history[]}}, pending{49}, seen[489]}` | Non-traded BDC tender and redemption results (SC TO-I) | Holder stress "forced seller" |
| `marklag` | 1,991 | `{f{fi:[score, n, first%, late2%, avglag, prem]}, m{mgr:[...5]}}` | Compact form of the watch marklag files | |
| `aka` | 48,918 | `{bkey:[aka, description, sponsor, url, flag]}`, 253 | Borrower descriptions and sponsors | Party enrichment (BDC borrowers; few AI) |
| `tells` | 10,746 | 23 rows | = tells_db.json without cik | |
| `banksdata` | 186,633 | `{U[190] urls, H[37] notes, asof 2026-06-30, links[860][13], eq{54}, stress{59}, unnamed[28], nonbank[41], fx}` | Bank lenders to BDCs (fund-level leverage facilities). `links` keys `f, fac, t, b, tk, ex, a, h, e, c, d, u, s` | Lender parties only; not AI DC loans |
| `repcov` | 1,402 | `{fi: float}` | Per-fund coverage % (unlabeled; looks like asset-coverage ratio) | |
| `q3cal` | 16,660 | 178 × `{f, d, st, b, ly, s, p}` | **Expected Q3 filing dates per BDC** (d = expected date, ly = last year's date, p = period 2026-09-30, st "due") | **InstrumentDetail.nextMarksPublic** |
| `readthru` | 63,730 | `{calibration{cross, same}, q2_replay{12}, q2_alerts[86], q2_fund_calls[78], q3{made_at_utc, sha256, funds[]}}` | Backtest of peer read-through (Q2 2026) | Backtest |
| `exitsdata` | 159,925 | `{exits[136], exit_summary, tcp{16}, whf{14}, blive{3540 bkey:[...3]}}` | Positions BDCs sold while other lenders kept theirs | |
| `note` | 6,277 | `{title, week_of 2026-10-15, dek, status, written_by, items[6]{tag, h, body...}}` | Weekly note ("First Cut weekly") | Template for the RiskList.note |

**`data` positional schemas (from the JS):**
- **funds** `[name, mgr, type, url, filed (YYYYMMDD), pos, fv, na, pt (pressure tier), pr (pressure reason), psrc]`. Type: Non-traded BDC (perpetual) 78, Private/institutional 54, Listed 46. Filed 20260707..20260928.
- **facilities** (27 cols) `[borrower, lien, spread (%), mat (ISO date), n, nm, par (USD), wm, lo, hi, gap, intra, intraMgr, naAny, naAll, pik, cmin, cmax, div, H[[f, par, m, chg, na, rawText, ?, ?]], bkey, mix, ..., facKey]`.
  - Sample: Retained Vantage Sub 9.5 2031-12-15, par 584,449,000, three holders fi 15/16/17 at 100.0.
- **borrowers** (16 cols) `[name, par, n, wm, lo, hi, chg, na, npv (N-PORT value), npn (N-PORT count), nppx, npdef, H[], N[[series, reg, type, inst, bal, val, px, def, arr, dt, pt, pr]], bkey, PR[[name, kind, tier, reason, amt]]]`. `N` is the N-PORT look-through (e.g. CoreWeave: 465 N-PORT holdings, $2.56B, avg 98.95). `_npf`/`_rfn` are lookup tables the JS expands into `N` and `lenders`.
- **alerts** (15 cols) `[date, issuer, ticker, form, sev, level, ev[], ex (excerpt), url, bdc, acc, hl, key, at]`.
  - 95 rows, 2025-07-11..2026-10-14.
  - Issuers: Cipher 14, CoreWeave 14, Core Scientific 9, Applied Digital 9, Hut 8 7, TeraWulf 6, IREN 5, Galaxy 4, NVIDIA 3, plus some BDCs.
  - Levels: CRITICAL 2, HIGH 20, MEDIUM 72, LOW 1.
  - **AI issuer 8-K/10-Q event alerts with SEC URLs.**
- **changes** `[f, borrower, bkey, cls, lien, ev, why, q0, q1, amt, m0, ok, u0, u1]` (Q1 to Q2 exits and adds).
- **opps** (36 cols) are mark-gap opportunities, with recovery ranges (`rlo, rb, rhi, rsrc`) and holders. **ew** are early-warning rows. **secr** are SEC staff comment letters.

**`aichain` detail:**
- **`tenants[16]`:** `{id, name, kind (AI lab/Hyperscaler/Neocloud/Chipmaker/Other/Undisclosed), own[] (own issuer sheets), direct{total $m, n, nd, keys["SHEET:cap"]}, via{total, n, keys}, mw, high, issuers[], viaIssuers[], viaEdges[[sheet, cap, viaTenant, ?]]}`.
  - IDs: openai, oracle, coreweave, microsoft, meta, anthropic, fluidstack, google, amazon, xai, nvidia, amd, nebius, janestreet, core42, undisclosed.
  - Sample: OpenAI direct $79,400m across ABILENE:0/1, JUPITER:0, SALINE:0, VANTAGE_ORCL:0; 4,650 MW.
- **`edges[85]`:** `{sheet, cp, tenant, role (tenant 44 / customer 30 / end user 5 / guarantor 5 / credit support 1), debt[cap idx], why, inferred (8 non-null), project, tenantText, mw (string), contract, backstop, risk (low 17 / medium 40 / high 28), src ("26,27,28" = keys into issuers[sheet].sources)}`.
  - Sample: ABILENE → oracle, tenant, debt [0, 1], "15-year lease", 1,200 MW, risk low.
- **`issuers{26}`:** ABILENE, APLD, BEIGNET, CIFR, CLOUDHQ, CORZ, CRUSOE, CRWV, DATABANK, FIRMUS, FLUIDSTACK, GLXY, HUT, IREN, JUPITER, LAMBDA, NBIS, NSCALE, QTS, SALINE, SOPAIPILLA, VANTAGE, VANTAGE_ORCL, WULF, XAI_VCI, ORCL. Each has:
  - Header: `{id, name, sector, ticker, score, call, asOf, latestFiling{form, period, filed, url}|null, totalDebt (string)}`.
  - **`caps[]`** (296 total; ORCL 63): `{i, instrument, entity, rank, amount ($m), basis, coupon, maturity, price, priceDate, priceSrc?, notes, committed, src, partOf}`. This is the capital structure.
  - **`holders[]`** (328): `{instrument, caps[], holder, type, par (string "$50.4M"), parM, mark, asOf, filed, furl, fund, kind, tier, reason}`.
  - **`cps[]`** (110 counterparty/project rows): `{project, tenant, mw, contract, backstop, risk, note, src}`.
  - **`sources{}`**: numbered and S/P/M/T/A-prefixed keys to URLs (386 numeric; 38 null). SEC vs press is distinguishable by host.

**`aiwatch.proj[80]`** `{issuer, project, site (null 8), mw (string or int), counterparty, term, rfs_prior{text, date, src}, rfs_latest{text, date, src}, changes[{date, what, quote, src}], financing[{instrument, amount_usd, lenders}], status (on track/slipping/at risk), status_why}`. This is lease and RFS-date evidence per project. `eq{ticker:{p, d, hi, off_hi, m1, m3, name}}` holds equity prices for 29 tickers (2026-10-15). `bdc[12]` is the list of bkeys linking aiwatch to fundpos (retained vantage, edgeconnex mcn, firmus supercloud, together computer, qts project ram, coreweave compute, coreweave, north america center, coreweave financing, crusoe, hyscaleix, lambda).

**`aicov`:**
- `params` (11 sliders, each with value, min, max, unit and a source citation): marketRent 125 $/kW-mo, capRate 8.5%, downtime 15 mo, liqPct 51%, partBuilt, workout 5%, buildCost 12.4 $m/MW, backstops, gpuDecline 30%/yr, gpuCost 10%, defaultDate 2028.5.
- `dc[18]` (id = `SHEET:cap`): `{name, tenant, debt $m, mw, built, price, priceDate, noiYr, reserve, costPerMW, attrib, backstop{full, note}, darkPerMW, noiMargin, inPlaceKw, ...notes}`.
- `gpu[3]` (XAI_VCI:11 etc.): `{lessee, cost, loan, start, maturity, price, priceDate, backstop, ...}`.
- `gpuNoData[6]` gives the reasons GPU collateral is n/d (CRWV DDTL 5.0 / 5.5, IREN, NBIS...).

**HTML quality:**
- Positional arrays with no inline header; the names come from the JS.
- The data block is 15 MB.
- **Two filers' non-accrual flags are unusable.** The JS sets `BADNA` when the non-accrual share is above 45%.
- `_unres` lists 27 bkeys that were not resolved ("10 9", "176 5 united states").
- `opps` contains "Unidentified borrower (filing text not parsed)".
- aichain caps lack instrument IDs and CUSIPs.
- `par` in holders is a string (`parM` is numeric).
- aichain/aicov `asOf` 2026-10-14 vs pulse 2026-10-16 vs data 2026-06-30: several as-of dates on one page.

**HTML seeds:**
- `party`: tenants, issuers, BDC funds, managers, bank lenders.
- `contract/lease`: aichain edges/cps, aiwatch proj (term, MW, RFS dates, backstop).
- `instrument`: aichain caps.
- `holder position`: data.facilities.H, borrowers.N, aichain holders.
- `obligation edge`: aichain edges, tenants direct/via.
- `event`: data.alerts, aiwatch changes, tells.
- `price`: caps price, eq, realnav.px.
- `document`: issuers.sources, latestFiling, filing URLs.
- `fact`: aicov params with citations.
- `campus/site`: only as free-text `project`/`site` strings, with no IDs or coordinates.

---

## 7. Mapping to types.ts and DB tables

| Target | Source in this area | Status |
|---|---|---|
| **Holder** (name, filing, filingUrl, filingDate, parMm, lastMark, gapPts, navHitMm, shareOfNav) | BDC: fundpos plus data.funds (url, filed) plus realnav.nav (NAV). N-PORT: agg_*, w2 rows (pctVal = share of NAV). aichain.issuers.holders | name, filing, url, date, par and mark are present. `gapPts` and `navHitMm` need a Marden price. `filing` enum ('N-PORT'\|'10-Q') is too narrow: 10-K, 6-K, BDC marks via other forms, dealer quotes |
| **InstrumentDetail.fundMarks / holders / nextMarksPublic** | fundpos per bkey and lien (low/high), N-PORT rows, `q3cal` (expected BDC filing dates), N-PORT filed dates | Derivable |
| **InstrumentDetail.priceHistory** | trace_rows.ser, spreads.ser, eod.json (bonds); bondq snapshots (3 days) | Bonds only; **no loan price history** |
| **InstrumentRow.mardenPrice / yieldPct / change1w / markGap** | yield, chg1w: TRACE/spreads (bonds) | **No Marden (M) price in this area**; loans have no observed weekly change |
| **InstrumentDetail.terms / marketCheck** | targets (coupon, maturity, amt_m, rank), out1/out2 (cpn, cpn_src, mat, mat_src), secs (coupon, maturity, ratings, 144A), aichain caps | Present, but as strings that need parsing |
| **SiteDetail.debtExposed.dealerQuote** | case_px / out* `kind:"dealer quote"` (JUPITER 89–91, BEIGNET 91) | Present for about 5 instruments |
| **CurveTier** | spreads.json (G-spread, mat), trace_rows y | Bonds; tier labels not present |
| **RiskRow.fundMark / gap / why** | fundpos, calls_r1 (p_cut5), aiwlive events, data.alerts | `odds` not present (calls_r1 p_cut5 is a BDC mark-cut probability, not repricing odds) |
| **RiskList.earlierCalls / backtest** | rt_hist, rt_up, nowcast_bt, q3snap.replay.status, readthru | BDC mark backtests, not instrument repricing |
| **Alert / StreamEvent mark, price, pipeline** | q3snap / out alerts, bondq, status / state.runs | Present |
| **Evidence / DcRecord / DcHistoryEvent** | aiwlive events (d, t, u, src), power feed (k, a, no, t, u), data.alerts, aiwatch changes | Present but noisy; src is sometimes mislabeled |
| **ObligationEdge / ObligationNode** | aichain.edges / tenants | `totalB` only as tenant totals; no `annual[]`, `start` or `termYears` as structured fields (free-text `contract`) |
| **LeaseRecord** | aiwatch.proj (term, mw, counterparty, rfs dates), aichain cps | Free text; no lessor/lessee IDs, `totalB`, `annualB` or commencement as fields |
| **GpuPricePoint / GpuProviderPrice / GpuModelDetail.index** | pulse.latest (RunPod, Vast, SemiAnalysis index, getdeploying, Silicon Data), pulse.h100 monthly, h100_contract_1y | No p25/p75/nProviders, region or term breakdown; history is 5 days except H100 monthly |
| **GpuCollateral / recovery** | aicov.gpu (cost, loan, start, maturity), params | Inputs only; no computed recovery |
| **Party** | ref_meta universe, data.funds (mgr, type), aichain tenants and issuers, banksdata lenders | Present; no ratings or CDS here (only TRACE bond ratings) |

## 8. Not in this area but needed by the V1 screens

1. **Marden price / spread / recovery / method version (M):** absent. Every mardenPrice, markGap, gapPts, navHitMm, stress `priceAfter` and RiskRow `odds` must be computed.
2. **Loan price history and weekly change:** loans have only quarterly BDC marks (6/30) and monthly N-PORT marks (May–Aug 2026). The bond side has daily TRACE from 2026-01-02 and 3 days of Frankfurt quotes.
3. **Site slugs and campus/building/plant IDs:** no file here carries `siteSlug`, campus ID, lat/lon or county. The case IDs (APLD_PF1, JUPITER, SALINE...) and aichain project strings would need a crosswalk to sites.
4. **Instrument ID on aichain caps and fundpos rows:** fundpos and data.facilities identify a loan by `bkey|lien|spread|maturity`, and caps by name. The only CUSIP → instrument link is `trace/match.json` (bonds). DDTL and loan holders need LoanX/`other` → instrument mapping, which does not exist.
5. **Structured contract terms:** lease start, outside date, annual rent, termination/FM terms are free text in aiwatch/aichain.
6. **Holder shareOfNav for BDCs:** needs realnav.nav joined by fi; it is not on the rows.
7. **Ratings, CDS, payer kpis:** not here (only bond ratings in TRACE secs).
8. **Explicit F/R/M tags:** must be derived from source kind.
9. **Watchlist / users:** none.
10. **External files referenced but missing:** `aiwc.js` and `parallax-q3-2026-calls.json`. `aidebt.json`, `bevents.json`, `collmeta.json`, `niche.json` and `power.json` are referenced by the HTML but sit outside this area.
