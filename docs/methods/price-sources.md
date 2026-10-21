# Marden price-source plan

> **Superseded in part (2026-10-21).** After this plan was written, the business owner decided that Marden collects finra.org TRACE, Börse Frankfurt and Yahoo Finance daily despite the terms below, and accepts the risk. Where this plan says **Blocked**, **Needs licence** or **Do not use** for those three, read [licensing.md](licensing.md): the rows are collected, carry the exact `source_name`s flagged `unlicensed-commercial`, and `MARDEN_SHOW_UNLICENSED_PRICES` is the switch that hides them. The terms quoted here and the licensed routes in §6 still stand.

Written 2026-10-21. It builds on `public-corporates.md` and `abs-cmbs-loans-marks.md`, and I re-checked every source those notes marked "use now".
- All requests sent the User-Agent `Marden research cody@marden.ai`. No credentials were used or stored. I made no requests to finra.org data endpoints, Deutsche Börse, or any site that blocks automated access.
- Raw responses are in `/tmp/claude-0/-home-user/041c65f7-05f2-55e2-9ba6-73f6e02ba8df/scratchpad/pricing/verify_dl/`.
- Parsers are in `/tmp/claude-0/-home-user/041c65f7-05f2-55e2-9ba6-73f6e02ba8df/scratchpad/pricing/verify_scripts/` (`h2t.py`, `efts.py`, `nport_px.py`).

**Tags** (`types.ts` F/R/M):
- **F (Filed):** a regulatory filing or regulatory tape, i.e. SEC filings and FINRA TRACE under licence.
- **R (Reported):** a third party's number, such as an evaluated price, exchange quote, or press-reported dealer quote.
- **M (Marden model):** our own derivation from F or R inputs.

## 1. Bottom line

1. **The SEC sources pass.** N-PORT fund marks, BDC 10-Q schedules of investments, and 8-K new-issue terms all check out.
   - Terms: the SEC sets no limit on commercial use. Automated access is allowed with a declared User-Agent at 10 requests/second or less.
   - Access: I re-tested 7 of our identifiers and computed prices from N-PORT for 6 of them. I also re-tested 3 BDC lines and 1 8-K.
   - TRACE: none of these sources comes from TRACE under a non-commercial licence.
2. **The sources pass, but much of the legacy price data that cites them is not from them.**
   - 163 `aidebt.json` prices are copies of the finra.org TRACE pull. 129 of those rows cite an N-PORT `primary_doc.xml` URL in `src`.
   - Another 68 prices are Börse Frankfurt website fixings. 66 of those rows also cite N-PORT.
   - All 231 are downgraded: **blocked** (TRACE from finra.org) or **needs licence** (Frankfurt). See §3.
3. **Issuer 10-Q fair values are downgraded to "fact only, not a price".** CoreWeave's 10-Q says carrying value "approximates fair value".
4. **No commercial-safe source gives a current price.** Today the newest usable observation is 7/31 or 8/31. For converts that is far off the market:
   - IREN 0% 2031 was 76.10 at 7/31 in N-PORT, against 57.94 in the blocked 10/16 TRACE pull.
   - CoreWeave 2.875% 2033 has no N-PORT mark yet.
   - A FINRA TRACE licence (Corporate Bond, Rule 144A and Securitized Products Data Sets) is the first purchase. A licensed equity feed for converts is the second.

## 2. Adversarial check of the "use now" sources

| Source | Terms clause I re-read today (quote, URL) | Automated-access rule | Re-test on our identifiers (all HTTP 200) | Derived from TRACE under a non-commercial licence? | Verdict |
|---|---|---|---|---|---|
| **SEC EDGAR N-PORT** (`primary_doc.xml`, daily index, DERA quarterly zip) | "Information presented on sec.gov is considered public information and may be copied or further distributed by users of the web site without the SEC's permission. Please consider appropriate citation to the SEC as the source." (https://www.sec.gov/about/privacy-information, last reviewed Nov 29, 2023) | "Current max request rate: 10 requests/second." "Please declare your user agent in request headers" (https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data). The privacy page adds: "The SEC does not allow "unclassified" bots or automated tools to crawl the site." Robots.txt disallows `/cgi-bin` and `/search/`, but not `/Archives/edgar/data/`, `/Archives/edgar/daily-index/` or `/files/`. | Implied mark = 100 × valUSD / balance:<br>- **21873SAK4** CoreWeave 9.625% 2032: 156 NPORT-P hits filed since 8/1. Thrivent Core High Yield 7/31: 718,000 PA, $642,632.49 → **89.5031**, Level 2.<br>- **74690FBC9** QTS ABS II 2026-4 A2: 34 hits. ActivePassive Core Bond ETF 8/31 (filed 10/20): 176,000 → **98.1710**.<br>- **21882DAB6** CoreWeave DDTL V: 201 hits. Calamos High Income Opps 7/31 → **100.0625**, asset category LON.<br>- **46270CAE9** IREN 0% 2031: Hartford Strategic Income 7/31 → **76.10**.<br>- **88080TAC8** TeraWulf 1% 2031: Calamos Conv. Equity Alt ETF 7/31 → **164.609**.<br>- **68389XBX2** Oracle 3.6% 2050: ActivePassive 8/31 → **57.9474**.<br>- **09175RAE0** Bitdeer 4% 2031: 7 hits.<br>- **21873SAL2** CoreWeave 2.875% 2033: **0 hits**, because it was issued after 7/31.<br>- Vanguard STIG 7/31 re-parse: 20469BAF4 **97.4538** and 74690FBC9 **98.483**. | No. These are the funds' own fair values, mostly Level 2 vendor evaluations, published by the SEC. Any vendor licence binds the fund, not EDGAR readers. Residual risk is low: cite "as filed on Form N-PORT by <fund>" and do not label marks as ICE or Bloomberg prices. | **Stands: USE NOW (F).** Lag is 50–60 days. Only the third month of each fiscal quarter is public. |
| **SEC BDC 10-Q/10-K schedules of investments** (iXBRL, and the SEC "BDC Data Sets" `soi.tsv`) | Same SEC clause as above. The BDC Data Sets page says: "The data has been automatically and directly taken from submissions created by the registrants and provided as filed with the Commission. The data sets will be updated monthly." (https://www.sec.gov/data-research/sec-markets-data/bdc-data-sets) | Same | Carlyle Secured Lending 10-Q at 6/30:<br>- CCAC II: par 1,832, cost 1,829, FV 1,851 → **101.04**.<br>- CCAC IV: 16,742 / 16,576 / 16,575 → **99.00**.<br>Palmer Square BDC at 6/30, DDTL V:<br>- Par 1,775,806, FV 1,883,231: raw **106.05**.<br>- Footnote 13: "Of the $5,000,000 commitment ... $3,224,194 was unfunded". Adjusted P = 100 × (FV + U) / (F + U) = **102.15** (M).<br>Bulk file: `2026_09_bdc.zip` returned 206 on a range request (417,557 bytes, last-modified 2026-10-08). | No. These are Level 3 board/valuation-agent marks. | **Stands: USE NOW (F raw, M adjusted).** Lag is quarter end plus 35–45 days. Q3 marks are due Nov 1–17. |
| **SEC 8-K new-issue terms** | Same SEC clause | Same | WULF 8-K Ex. 99.1: "priced a $3.2 billion offering ... of 7.750% senior secured notes due 2030 ... at par." | No | **Stands: USE NOW (F)**, for issue price, coupon and spread at issue only |
| **Issuer 10-Q fair-value footnote** (CoreWeave DDTLs) | SEC public | Same | Researcher quote, checked in the same filing: "carrying value of the variable-rate borrowings ... approximates fair value" | No | **Downgraded: fact only (F), not a price input.** Remove the px from EX-CRWV-093 and EX-CRWV-096. |
| **US Treasury par yield curve** (model input, not a price) | My grep of https://home.treasury.gov/subfooter/site-policies-and-notices found no copyright clause. Treasury data is a U.S. Government work with no copyright (17 U.S.C. §105). | No restriction found | `daily-treasury-rates.csv/2026/all?type=daily_treasury_yield_curve`: 200. Row 10/20/2026: 10Y 5.12, 30Y 5.42. | n/a | **USE NOW (F)** |
| **NY Fed SOFR** (model input for floaters) | "The New York Fed grants you a non-exclusive license, subject to the Terms, to use, copy, and distribute Content for your personal or business purposes. You may: Access the Content, manually or through an automated process or device, provided your access does not have the effect of disabling, damaging, or interfering with the function of the Website". Reference-rate data must carry this notice: "The [NAME OF DATA or CONTENT]* is subject to the Terms of Use posted at newyorkfed.org. The New York Fed is not responsible for publication of the [DATA NAME] by [NAME OF P[UBLISHER]] ..." (https://www.newyorkfed.org/privacy/termsofuse) | Allowed | `markets.newyorkfed.org/api/rates/secured/sofr/last/1.json`: 200. SOFR 3.88 on 2026-10-20. | n/a | **USE NOW (F)**, with the notice. Term SOFR and forward curves are CME products and are not covered. |

**Access caveats for the build:**
- EDGAR full-text search (`efts.sec.gov`) works, but it is undocumented and has no robots.txt (403). Use it only for ad-hoc lookups at 1 request/second or less.
- For discovery, use the daily index. `form.20261020.idx` returned 200 and listed 12 NPORT-P and 26 10-Q lines.
- Never call `/cgi-bin/browse-edgar`, which robots.txt disallows. Legacy `q3x/hold.json` cites it.

## 3. Downgrades and quarantine of legacy data (do before any build)

| Item | Count | Evidence | New status |
|---|---|---|---|
| `aidebt.json` px equal to finra.org TRACE (`trace/trace_rows.json`, same price and date) | 163: 134 corp, 17 proj, 11 util, 1 abs | 129 of them cite an N-PORT `primary_doc.xml` in `src`. Example: EX-APLD-010 is 102.375 on 2026-10-20 and cites a BlackRock N-PORT filing. N-PORT can't carry a 10/20 price. | **Blocked.** finra.org terms allow use "ONLY for your own non-commercial personal or professional use". They forbid "develop or create a database" and use "in conjunction with any machine learning ... predictive analytics" (https://www.finra.org/terms-of-use). |
| `aidebt.json` px equal to Börse Frankfurt fixings (`watch/*/bondq.json`, src "Börse Frankfurt price fixings (indicative; thin trading)") | 68: 30 corp, 38 util | 66 cite an N-PORT URL. Example: NP-EQIX-29390XAE4 is 97.81 on 10/16. | **Needs licence (R).** The free licence applies only if the user "does not commercialize the FWB-Data". |
| `trace/*`: eod.json, trace_rows.json, spreads.json (G-spreads use TRACE yields), secs*.json (security master), and the 16 "TRACE last sale" rows in `aiwc/pxsrc/case_px.json` | All | Same backend | **Blocked.** The CUSIP master built from `secs.json` is tainted too, so rebuild it from SEC documents. |
| Legacy collectors still running | `trace/refresh_state.json` last ran 2026-10-20T10:55. The `bondq` Frankfurt watcher last ran 2026-10-20T17:20Z. | | **Switch both off now.** |
| Legacy fund marks that don't match their cited filing | EX-COMPASS-060 is 98.37; the filing shows 97.4538. **New:** EX-QTS-287 (QTSII 2026-4 A2) is 98.84; Vanguard shows 98.483. Researchers also flagged EX-QTS-290, EX-SWITCH-329, EX-QTS-298, EX-COMPASS-059 and EX-CRWV-091. | Vanguard file re-parsed today | **Quarantine.** Recompute every fund-mark px from the filing it cites. |
| fundpos "vantage" / "Vantage Bidco" | | Vantage Specialty Chemicals | **Remove** from the data-center mapping |
| fundpos raw DDTL V 105.6–106.0 | | Unfunded-commitment effect, confirmed in Palmer Square footnote 13 | Keep raw as F. Use the adjusted 102.15 as M. |
| `q3x/hold.json` NAIC Sch. D positions (117, `url: null`), "via state and insurer sites" | 117 | The NAIC content site is behind a Cloudflare challenge and its terms could not be read. The Ohio DOI directory `legacy.insurance.ohio.gov/FRAnnuals/` returned 200 with no terms page and no robots.txt (404). | **Hold for legal review.** Do not automate. |
| Equity prices from the Yahoo chart API (`y_CRWV.json` etc.) | | Unofficial endpoint; terms not checked this session | **Do not use.** Replace with a licensed feed (§6). |
| Press-reported dealer quotes (EX-JUPITER-190, FT via Reuters on kfgo.com) | 1 | A fact reported in the press | **R, manual entry only**, with a citation. Never scrape news sites. |

**Correction to the ABS note:** the legacy finra.org pull holds TRACE prints with productType SP for two data-center ABS classes: 54143VAA4 (CLDCP 2026-1A A21) and 268332AB9 (ECORE 2025-1A B). So 144A data-center ABS **are** disseminated, in the Securitized Products feed. That data is blocked for use; it counts only as evidence of coverage.

## 4. Sources by instrument class (in order of use)

Every observation is stored as `(instrument_id, cusip/isin, asof, price, kind, tag, source_name, url, n_obs, dispersion, lic)`. The model uses these roles:
- **Anchor:** sets the spread level on its as-of date.
- **Roll:** moves an anchor forward to today.
- **Check:** validation only, with no weight.
- Confidence falls as the anchor ages and as cross-fund dispersion widens.

### A. Oracle and Meta senior notes (registered, Corporate Bond Data Set)
1. **FINRA TRACE, Corporate Bond Data Set (F).** Needs licence. Primary anchor, daily: the snapshot price or the volume-weighted average of institutional-size prints. Converted to a G-spread over the UST curve.
2. **SEC N-PORT fund marks (F).** Use now. Month-end anchor: the median across funds, with the min–max range as dispersion. Oracle 3.6% 2050 had 121 filings since August. This is the only anchor until TRACE is licensed.
3. **UST par curve (F).** Use now. Rolls the anchor forward at constant spread. Output is flagged "spread stale since <asof>".
4. **Evaluated price, ICE/BVAL/LSEG (R).** Needs licence. Optional check.
- Not used: Börse Frankfurt quotes (R; needs licence; zero turnover), legacy finra.org data.

### B. CoreWeave senior notes (144A and RegS)
1. **TRACE Rule 144A Data Set (F).** Needs licence. Primary anchor. The RegS CUSIPs (U2069E…) also had prints in the legacy pull.
2. **N-PORT (F).** Use now. Anchor; 21873SAK4 is held widely. Foreign-currency notes (the EUR 8.5% 2032) need the filing's `exchangeRt`.
3. **UST curve (F).** Roll.
4. **CoreWeave 10-Q per-note fair value, if disclosed by series (F).** Check only. Not verified this session.

### C. Converts: CoreWeave 2.875% 2033, IREN 0% 2031, TeraWulf 1% 2031, Bitdeer 4% 2031
1. **TRACE Rule 144A Data Set (F).** Needs licence. Primary anchor.
2. **N-PORT (F).** Use now. A stale anchor: equity moves dominate. IREN went from 76.10 (7/31) to about 58 (blocked reference only). CoreWeave 2033 has no N-PORT mark until the 9/30 filings (around late November).
3. **Licensed equity last price (R).** Needs licence. Roll: price ≈ anchor + delta × equity move, using a simple convertible model (M).
4. **8-K issue terms and indenture (F).** Use now. Issue price at t0, conversion ratio and terms. CoreWeave 2033's issue at 100 on 2026-09-22 comes from the legacy pretrace row; re-verify it from the 8-K.

### D. 144A secured project notes: Cipher, WULF Compute, APLD, Flash Compute
1. **TRACE Rule 144A Data Set (F).** Needs licence. Primary anchor.
2. **N-PORT (F).** Use now. Anchor. WULF 982911AA7 had 1,445 filings and Flash 33853QAA9 had 658 (researcher counts).
3. **8-K issue terms (F).** Use now. Anchor at t0, e.g. WULF 7.75% 2030 at par.
4. **UST curve (F).** Roll.
5. **NAIC Sch. D Part 3 purchase prices (F).** Hold for legal review.

### E. Data-center ABS: Vantage VDC, QTS ABS, Switch, Compass, DataBank, Cloud Capital, plus Aligned, Stack, Sabey, Flexential, EdgeConneX, CyrusOne, TierPoint
1. **N-PORT (F).** Use now. Primary anchor today, with 8–14 funds per class at 7/31. Store the cross-fund median and range.
2. **TRACE Securitized Products Data Set, plus Rule 144A if FINRA says 144A ABS sit there (F).** Needs licence. Anchor when traded. Legacy evidence shows SP prints exist.
3. **Evaluated ABS prices, ICE/PricingDirect/BVAL (R).** Needs licence. Fills the gaps between N-PORT months.
4. **UST curve or SOFR (F).** Roll at constant spread or discount margin.
5. **ABS-15G and KBRA/Moody's presales.** Collateral facts only. No prices, and automated collection is blocked.

### F. Data-center CMBS: BX VLT, SWCH, CONE
1. **N-PORT (F).** Use now. The only anchor, and thin: VLT10 A has one fund, VLT4 five, SWCH three, CONE 2024-DFW1 four. No N-PORT marks were found for CONE 2026-DFW3 or ACD6.
2. **Evaluated CMBS prices, Trepp/ICE/BVAL (R).** Needs licence. Needed because TRACE never disseminates CMBS (Rule 6750(d)(4)).
3. **SOFR (F).** Roll for floating SASB deals.

### G. Private loans and DDTLs: CoreWeave DDTL 1.0–5.0 and CCAC, QTS Project Ram, Jupiter
1. **N-PORT, including interval funds (F).** Use now. Primary anchor. DDTL V is in 201 filings, about 100.06 at 7/31.
2. **BDC SOI (F raw, M adjusted).** Use now. Quarterly anchor. Use the unfunded-adjusted price when a footnote gives the commitment. On DDTL V, BDC marks (adjusted about 102.15) run about 2 points above registered-fund marks (about 100.06), so treat each as its own cohort.
3. **Credit agreement and 8-K terms (F).** Use now. Spread and maturity for discount-margin conversion, e.g. DDTL 3.0 at "SOFR +4%".
4. **SOFR (F).** Roll at constant discount margin.
5. **Press-reported dealer quotes (R).** Manual entry, low weight.
6. **Loan pricing service, LSEG LPC or S&P/Markit (R).** Needs licence. Optional.
- Not used: the issuer 10-Q "approximates fair value" figure.

## 5. What to build now

All SEC collectors share one rate limiter: at most 5 requests/second total, with header `User-Agent: Marden research cody@marden.ai` and `Accept-Encoding: gzip`. They never call `/cgi-bin` or `/search/`.

| Collector | Endpoint | Cadence | Output / tag |
|---|---|---|---|
| `sec_nport_marks` | Discovery: `https://www.sec.gov/Archives/edgar/daily-index/{YYYY}/QTR{q}/form.{YYYYMMDD}.idx` (NPORT-P, NPORT-P/A). Fetch: `https://www.sec.gov/Archives/edgar/data/{cik}/{adsh-no-dashes}/primary_doc.xml`. Backfill: `https://www.sec.gov/files/dera/data/form-n-port-data-sets/{YYYY}q{n}_nport.zip`. | Daily at 07:00 ET. Quarterly zip around the 7th–10th after quarter end. | Per position: cusip, isin, period, series, adsh, url, balance, units, valUSD, curCd, exchangeRt, fairValLevel, assetCat, px. Per CUSIP-period: n_funds, median, min, max. `kind=fund mark`, **F**. Keep `units=PA` only, and flag dispersion above 1 point. Expect filings for 8/31 to about Oct 30 and for 9/30 (the largest batch) to about Nov 29. |
| `sec_bdc_soi` | `https://www.sec.gov/files/datastandardsinnovation/data/business-development-company-bdc-data-sets/{YYYY}_{MM}_bdc.zip` (`soi.tsv`). In filing season, also 10-Q/10-K iXBRL from the daily index for BDC CIKs (the 178 in fundpos). | Monthly on the 9th. Daily Nov 1–20, Feb 15–Mar 10, May 1–20, Aug 1–20. | Borrower, facility, par, cost, FV, unfunded, spread, maturity. Raw px **F**; adjusted px **M**. Exclude the Vantage Specialty Chemicals rows. |
| `sec_issue_terms` | Daily index, 8-K entries for the issuer CIKs already in `aidebt.json` `src`. Fetch Ex. 99.x and the indenture exhibits. | Daily | Issue price, coupon, spread, maturity, conversion terms. `kind=issue price`, **F**. Human confirms before publishing. |
| `ust_par_curve` | `https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{YYYY}/all?type=daily_treasury_yield_curve&field_tdr_date_value={YYYY}&page&_format=csv` | Daily at 18:30 ET | Curve, **F** |
| `nyfed_sofr` | `https://markets.newyorkfed.org/api/rates/secured/sofr/last/1.json` | Daily at 08:30 ET | SOFR, **F**, published with the required NY Fed notice |
| `edgar_lookup` (ad hoc) | `https://efts.sec.gov/LATEST/search-index?q="{CUSIP}"&forms=NPORT-P` | On demand, 1 request/second or less | Finds which funds hold a new CUSIP |
| `legacy_quarantine` (one-off) | Local | Once, before the next build | Tag the 163 rows `lic=blocked-finra-website` and the 68 rows `lic=needs-licence-fwb`. Strip their px from product outputs. Recompute every "fund mark" px from its cited filing. Clear px on EX-CRWV-093 and EX-CRWV-096. Turn off the `trace` refresh and the `bondq` watcher. |
| `cusip_master` | SEC indentures, 8-Ks and N-PORT identifiers, not finra.org `secs.json` | On new issue | Instrument ↔ CUSIP/ISIN map. Pending the CGS decision in §6. |
| `trace_ingest` (stub, enable after licence) | FINRA TRACE snapshot or feed / EOD file under the vendor agreement | Daily, or real time | `kind=TRACE`, **F**, `lic=finra-7730` |

## 6. What needs the user (licences, keys, costs, decisions)

1. **FINRA TRACE market-data licence (Rule 7730).** Highest priority. Sign the FINRA vendor/subscriber agreement.
   - **Data Sets:** Corporate Bond (Oracle, Meta), Rule 144A (CoreWeave notes and converts, IREN, TeraWulf, Bitdeer, project notes), and Securitized Products (data-center ABS).
   - **Snapshot** (one price per security per day): $250/mo per set, so **$750/mo** for 3 sets ($500 without SP).
   - **Real-time feed:** $1,500/mo per set, so **$4,500/mo** ($3,000 without SP). It includes the end-of-day file, which otherwise costs $750/mo per set.
   - **Historic backfill:** $2,000 set-up plus $2,000/yr per set, so **$8,000 in year one** for 3 sets. It is delayed at least 6 months, or 18 months for SP and 144A SP.
   - **Display:** showing prints to fund users adds **$60/mo per display application per set**, or $7,500/mo flat per set.
   - **Ask FINRA in writing:**
     - (a) whether model prices sold to funds count as "repackaging ... such as indices or other derivative products";
     - (b) which Data Set carries 144A ABS.
   - **Alternative:** buy TRACE through a vendor that already redistributes it (ICE, LSEG, Bloomberg, MarketAxess BondTicker). Price is on quote.
2. **Licensed equity prices** for convert roll-forward (CRWV, IREN, WULF, BTDR), with external redistribution of derived values. Options include Nasdaq Basic, Cboe, or the Massive business plan. Cost was not checked this session; get quotes.
3. **Evaluated pricing for ABS, CMBS and loans:** ICE/PricingDirect/BVAL, Trepp for CMBS, LSEG LPC or S&P for loans. Costs are not public; request quotes. This is needed mainly for CMBS, because TRACE never disseminates it.
4. **CUSIP licence.** Third-party flow-down terms say use "outside clearing and settlement" needs a CGS licence with usage-based fees, and CGS has sent licence-demand letters. CGS's own policy was not found; ask CGS or counsel.
5. **Legal sign-offs:**
   - (a) republishing N-PORT and BDC marks with "as filed by <fund>" attribution;
   - (b) NAIC Sch. D via state DOI sites;
   - (c) manual use of press-reported dealer quotes;
   - (d) what to do with the legacy finra.org and Frankfurt data. finra.org terms bar even building a database or using it in predictive models, so I recommend deleting it from Marden stores and keeping it out of any calibration.
6. **Optional, recommend skipping:** a Börse Frankfurt licence. Pre-Trade non-display is €360–1,650/mo and distribution is €1,115/mo. It covers quotes only, with zero turnover.
7. **Confirm the User-Agent contact** `cody@marden.ai` is monitored. The SEC asks for a reachable contact.

## Sources (fetched 2026-10-21)
- [SEC privacy and dissemination](https://www.sec.gov/about/privacy-information) · [Accessing EDGAR data](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data) · [SEC developer resources](https://www.sec.gov/about/developer-resources) · [SEC BDC Data Sets](https://www.sec.gov/data-research/sec-markets-data/bdc-data-sets) · [SEC N-PORT Data Sets](https://www.sec.gov/data-research/sec-markets-data/form-n-port-data-sets)
- [NY Fed Terms of Use](https://www.newyorkfed.org/privacy/termsofuse) · [Treasury site policies](https://home.treasury.gov/subfooter/site-policies-and-notices)
- [FINRA terms of use](https://www.finra.org/terms-of-use) and [Rule 7730](https://www.finra.org/rules-guidance/rulebooks/finra-rules/7730), from the researchers' saved copies
- CUSIP licensing: [Octus CUSIP terms](https://octus.com/terms-cusip/) · [MSRB subscription agreement](https://www.msrb.org/sites/default/files/msrb1/pdfs/Subscription-Service-Agreement.pdf) · [WatersTechnology on CUSIP licensing](https://www.waterstechnology.com/data-management/7952899/where-have-four-years-of-cusip-legal-drama-gone?ref=search)
