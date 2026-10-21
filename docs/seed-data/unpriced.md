# Instruments without a usable seeded price

Written by `python -m marden seed holdings --write-docs` on 2026-10-21, after the
instruments loader (press dealer quotes, fund marks recomputed from each instrument's cited filing) and the
holdings loader (N-PORT and BDC 10-Q marks of every linked holding line, cross-fund medians, the
unfunded-adjusted DDTL mark). 639 of 707 instruments have no `price_obs` row. TRACE,
Börse Frankfurt and Yahoo values are never loaded (docs/methods/price-sources.md), so most bonds stay
unpriced until a licensed TRACE feed or newer N-PORT filings arrive.

| Why there is no seeded price | Instruments |
|---|---|
| legacy px cites a fund filing, but no line for it is in the archive | 234 |
| no legacy price in the archive and no linked fund holding | 194 |
| legacy px was a copy of the finra.org TRACE pull (blocked) | 136 |
| legacy px was a Börse Frankfurt fixing (needs licence) | 64 |
| legacy px source is not a fund filing or press dealer quote | 8 |
| held in seeded filings, but no linked line gives a usable mark (non-USD, unfunded, no par) | 2 |
| legacy px was the issuer's 'approximates fair value' statement (fact only) | 1 |

## V1 instruments without a seeded price (18)

| Id | Instrument | Issuer | Kind | Linked holdings | Why |
|---|---|---|---|---|---|
| `EX-ABILENE-000` | Phase 1 construction loan | Abilene JV (Crusoe / Blue Owl / Primary Digital) | construction_loan | 0 | no legacy price in the archive and no linked fund holding |
| `EX-ABILENE-001` | Phase 2 construction loan | Abilene JV (Crusoe / Blue Owl / Primary Digital) | construction_loan | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CRWV-093` | DDTL 2.1 | CoreWeave Compute Acquisition Co. IV, LLC | ddtl | 0 | legacy px was the issuer's 'approximates fair value' statement (fact only) |
| `EX-CRWV-094` | DDTL 3.0 | CoreWeave Compute Acquisition Co. VII, LLC (co-borrower CCAC V) | ddtl | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CRWV-105` | 8.50% EUR Sr Notes 2032 | CoreWeave, Inc. | bond | 14 | held in seeded filings, but no linked line gives a usable mark (non-USD, unfunded, no par) |
| `EX-CRWV-108` | 2.875% Convertible Notes 2033 | CoreWeave, Inc. | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-IREN-185` | 5.96% USPP Sr. Secured Notes | IE US Hardware 3 LLC | bond | 0 | no legacy price in the archive and no linked fund holding |
| `EX-NBIS-204` | Senior secured term loan | Nebius Compute II LLC / Nebius Compute II Oy | term_loan | 0 | no legacy price in the archive and no linked fund holding |
| `EX-NSCALE-210` | Ward County GPU DDTL | Nscale Ward County Borrower SPV, LLC | ddtl | 0 | no legacy price in the archive and no linked fund holding |
| `EX-NSCALE-211` | North Carolina GPU DDTL | Nscale NC Borrower SPV, LLC | ddtl | 0 | no legacy price in the archive and no linked fund holding |
| `EX-NSCALE-212` | Dell Financial Services leases | Ward County and NC SPVs | lease | 0 | no legacy price in the archive and no linked fund holding |
| `EX-ORCL-228` | 4.95% due Feb-2031 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-239` | 5.20% due Sep-2035 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-240` | 5.70% due Feb-2036 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-261` | 5.95% due Sep-2055 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-262` | 6.70% due Feb-2056 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-VANTAGE_ORCL-366` | Construction debt (TX + WI) | Vantage Frontier / Port Washington SPVs | construction_loan | 0 | no legacy price in the archive and no linked fund holding |
| `EX-XAI_VCI-383` | Valor failed sale-leasebacks (3 GPU leases) | CTC Property LLC | lease | 0 | no legacy price in the archive and no linked fund holding |

## All other instruments without a seeded price (621)

| Id | Instrument | Issuer | Kind | Linked holdings | Why |
|---|---|---|---|---|---|
| `8K-ONCOR-4.2-2035` | 4.20% Senior Secured Notes due 2035 | Oncor Electric Delivery Company LLC | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `8K-ONCOR-4.5-2031` | 4.50% Senior Secured Notes due 2031 | Oncor Electric Delivery Company LLC | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `8K-ONCOR-4.65-2029` | 4.65% Senior Secured Notes due 2029 | Oncor Electric Delivery Company LLC | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `8K-ONCOR-5.35-2035` | 5.35% Senior Secured Notes due 2035 | Oncor Electric Delivery Company LLC | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `8K-ONCOR-5.55-2054` | 5.55% Senior Secured Notes due 2054 | Oncor Electric Delivery Company LLC | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `8K-ONCOR-5.65-2036` | 5.65% Senior Secured Notes due 2036 | Oncor Electric Delivery Company LLC | bond | 0 | no legacy price in the archive and no linked fund holding |
| `8K-ONCOR-5.8-2055` | 5.80% Senior Secured Notes due 2055 | Oncor Electric Delivery Company LLC | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `8K-ONCOR-5.9-2056` | 5.90% Senior Secured Notes due 2056 | Oncor Electric Delivery Company LLC | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `8K-ONCOR-6.35-2066` | 6.35% Senior Secured Notes due 2066 | Oncor Electric Delivery Company LLC | bond | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-ALPHAGEN-AlphaGeneration` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | Alpha Generation (PJM gas IPP; AI-load beneficiary - inferred) | other | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-APLD-AppliedDigital` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | Applied Digital (name match in BDC filing) | other | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-ARK-ArkDataCenters` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | Ark Data Centers LLC (1L unitranche) | other | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-CEG-Calpine` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | Calpine (CEG sub) term loans | other | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-COGENTRIX-Cogentrix` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | Cogentrix Energy (PJM gas IPP; inferred) | other | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-CRUSOE-CrusoeEnergy` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | Crusoe Energy Systems LLC | other | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-CRWV-CoreWeave` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | CoreWeave (DDTL SPVs / parent) - verify loan vs equity per filer | other | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-CYRUSONE-CyrusOne` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | CyrusOne (warehouse / loans) | other | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-DARTPOINTS-DartPoints` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | DartPoints (edge data centers) | other | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-DCBLOX-DCBlox` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | DC BLOX (data-center developer) | other | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-EDGECONNEX-EdgeConneX` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | EdgeConneX (HoldCo sub loan) | other | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-FIRMUS-Firmus` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | Firmus Technologies (Blackstone 1L TL/DDTL) | other | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-FLEXENTIAL-Flexential` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | Flexential (Topco revolver / loans) | other | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-HUNTERSTOWN-Hunterstown` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | Hunterstown Generation (PJM gas; inferred) | other | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-INVENERGY-Invenergy` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | Invenergy (power developer; inferred) | other | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-NRG-LightningPower` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | Lightning Power LLC (NRG) term loan | other | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-NSCALE-Nscale` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | Nscale (Blue Owl facilities) | other | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-TENSORWAVE-TensorWave` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | TensorWave (AMD GPU neocloud) | other | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-TLN-TalenEnergy` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | Talen Energy Supply (TLB) | other | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-TOGETHER-TogetherComputer` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | Together Computer (equipment financing) | other | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-USSIGNAL-USSignal` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | US Signal (data center / fiber) | other | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-VANTAGE-VantageDataCente` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | Vantage Data Centers (HoldCo sub loan / DDTL) | other | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-VST-Vistra` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | Vistra (term loans; verify vs Vistra corporate-services name collision | other | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-XAI_VCI-VCIAssetHoldings` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | VCI Asset Holdings 1/2/3 LLC (xAI GPU SPV loans) | other | 0 | no legacy price in the archive and no linked fund holding |
| `BDC-XAI_VCI-ValorCompute` | BDC / private-credit loan(s) - link to product BDC marks by borrower name | Valor Compute Infrastructure (xAI GPU leases) | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-ALIGNED-002` | ADC 2021-1 A2 | Aligned Data Centers Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-ALIGNED-003` | ADC 2021-1 B | Aligned Data Centers Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-ALIGNED-004` | ADC 2022-1 A2 | Aligned Data Centers Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-ALIGNED-005` | ADC 2023-1 A2 | Aligned Data Centers Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-ALIGNED-006` | ADC 2023-2 A2 | Aligned Data Centers Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-ALIGNED-007` | ADC 2026-1 A2-I | Aligned Data Centers Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-ALIGNED-008` | ADC 2026-1 B | Aligned Data Centers Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-ALIGNED-009` | International LP loan | Aligned Data Centers International LP | term_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-APLD-014` | 2026 Revolver | APLD Intermediate HoldCo LLC | facility | 0 | no legacy price in the archive and no linked fund holding |
| `EX-APLD-015` | Texas Capital Note | Applied Digital Corporation | bond | 0 | no legacy price in the archive and no linked fund holding |
| `EX-APLD-016` | Other term loans and notes | Various subsidiaries | bond | 0 | no legacy price in the archive and no linked fund holding |
| `EX-APLD-017` | MAM Preferred Units | APLD HPC TopCo 2 LLC | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-APLD-018` | Series E-1 Preferred | Applied Digital Corporation | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-ARK-019` | 1L term loan | Ark Data Centers, LLC | term_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-ARK-020` | 1L delayed-draw term loan | Ark Data Centers, LLC | ddtl | 0 | no legacy price in the archive and no linked fund holding |
| `EX-ARK-021` | 1L revolver | Ark Data Centers, LLC | facility | 0 | no legacy price in the archive and no linked fund holding |
| `EX-BEIGNET-023` | Residual value guarantee (Hyperion) | Meta Platforms, Inc. | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-BEIGNET-024` | Hyperion operating leases (initial term) | Pelican Leap LLC (Meta) | lease | 0 | no legacy price in the archive and no linked fund holding |
| `EX-BEIGNET-025` | Meta senior unsecured notes (5 issues) | Meta Platforms, Inc. | bond | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CIFR-026` | MS revolver | Cipher Digital Inc. | facility | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CIFR-030` | 1.75% Convertible Notes 2030 | Cipher Digital Inc. | convert | 0 | legacy px source is not a fund filing or press dealer quote |
| `EX-CIFR-032` | 0% Convertible Notes 2032 (launched) | Cipher Digital Inc. | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-CIFR-033` | Parent completion guarantees | Cipher Digital Inc. | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CLOUDHQ-035` | Cloud Capital 2024-1A A3 | Cloud Capital Holdco LP (Bourzou/Jamshid issuers) | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CLOUDHQ-036` | Cloud Capital 2024-2A A2 | Bourzou Issuer / Jamshid | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CLOUDHQ-038` | CloudHQ VA B1 quick-draw | CloudHQ VA construction SPVs | construction_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CLOUDHQ-039` | CloudHQ VA B1 unfunded tranche | CloudHQ VA construction SPVs | other | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CLOUDHQ-040` | CloudHQ VA B2 quick-draw | CloudHQ VA construction SPVs | construction_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CLOUDHQ-041` | CloudHQ VA B3 quick-draw | CloudHQ VA construction SPVs | construction_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-COLOGIX-042` | CLGIX 2022-1CAN A2 (CAD) | Cologix Canada ABS issuer | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-COLOGIX-043` | CLGIX 2022-1CAN B (CAD) | Cologix Canada ABS issuer | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-COLOGIX-044` | CLGIX 2022-1CAN C (CAD) | Cologix Canada ABS issuer | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-COLOGIX-045` | Cologix US 2026-1 | Cologix US ABS issuer | abs | 0 | no legacy price in the archive and no linked fund holding |
| `EX-COLOGIX-046` | Cologix Canada 2026-1 | Cologix Canada ABS issuer | abs | 0 | no legacy price in the archive and no linked fund holding |
| `EX-COMPASS-047` | II 2024-1 A1 | Compass Datacenters Issuer II, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-COMPASS-048` | II 2024-1 A2 | Compass Datacenters Issuer II, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-COMPASS-049` | II 2024-1 B | Compass Datacenters Issuer II, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-COMPASS-050` | II 2024-2 A1 | Compass Datacenters Issuer II, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-COMPASS-051` | II 2024-2 B2 | Compass Datacenters Issuer II, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-COMPASS-052` | II 2025-1 A1 | Compass Datacenters Issuer II, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-COMPASS-053` | II 2025-1 A2 | Compass Datacenters Issuer II, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-COMPASS-054` | II 2025-1 B1 | Compass Datacenters Issuer II, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-COMPASS-055` | II 2025-2 A1 | Compass Datacenters Issuer II, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-COMPASS-056` | III 2025-1 A2 | Compass Datacenters Issuer III, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-COMPASS-057` | III 2025-1 A3 | Compass Datacenters Issuer III, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-COMPASS-058` | III 2025-2 A2 | Compass Datacenters Issuer III, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-COMPASS-059` | III 2025-3 A2 | Compass Datacenters Issuer III, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-COMPASS-060` | III 2026-1 A21 | Compass Datacenters Issuer III, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-COMPASS-061` | III 2026-1 A22 | Compass Datacenters Issuer III, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-COMPASS-062` | III 2026-1 A23 | Compass Datacenters Issuer III, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-COMPASS-063` | III 2026-2 A2 (class 1) | Compass Datacenters Issuer III, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-COMPASS-064` | III 2026-2 A2 (class 2) | Compass Datacenters Issuer III, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CORZ-068` | Revolving Credit Facility | Core Scientific, Inc. | facility | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CORZ-069` | L/C Facility | Core Scientific, Inc. | facility | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CORZ-070` | Finance leases | Parent / subs | lease | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CORZ-071` | Operating lease liabilities | Parent / subs | lease | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CORZ-072` | Dalton Bonds (JDA taxable revenue bonds) | Dalton-Whitfield JDA | muni | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CORZ-073` | Completion Guarantee to Finance I | Core Scientific, Inc. | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CRUSOE-074` | 1L TL Tranche II | Crusoe Energy Systems LLC | term_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CRUSOE-075` | 1L TL Tranche III | Crusoe Energy Systems LLC | term_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CRUSOE-076` | Sr secured loan 1 (MCIP) | Crusoe Energy Systems LLC | term_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CRUSOE-077` | Sr secured loan 2 (MCIP) | Crusoe Energy Systems LLC | term_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CRUSOE-078` | Loan participation 'Lien 2' | Crusoe Energy Systems LLC | term_loan | 0 | legacy px source is not a fund filing or press dealer quote |
| `EX-CRUSOE-079` | Loan participation 'Lien 3' | Crusoe Energy Systems LLC | term_loan | 0 | legacy px source is not a fund filing or press dealer quote |
| `EX-CRUSOE-081` | Loans (3 lines) | Crusoe Energy Systems LLC | term_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CRUSOE-082` | Loans (3 lines) | Crusoe Energy Systems LLC | term_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CRUSOE-083` | Loan participation | Crusoe Energy Systems LLC | term_loan | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CRUSOE-084` | Credit facility (Brookfield) | Crusoe (entity n/d) | facility | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CRUSOE-085` | GPU facilities (Upper90 + BCI, King Street, Liberty Mutual, ORIX) | GPU SPVs (names n/d) | facility | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CRUSOE-086` | GPU facility, Iceland (Victory Park) | GPU SPV (name n/d) | facility | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CRUSOE-087` | GPU loan with AMD rent-back (Goldman) | GPU SPV (name n/d) | term_loan | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CRUSOE-088` | DDTL (Goldman) | GPU SPV (name n/d) | ddtl | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CRUSOE-089` | Credit / bridge (Morgan Stanley-led) | n/d | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CRUSOE-090` | Abilene JV construction loans | Abilene JV (Crusoe / Blue Owl / Primary Digital) | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CRWV-098` | Revolving credit facility | CoreWeave, Inc. | facility | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CRWV-099` | OEM / software financing (recourse) | CoreWeave, Inc. / subsidiaries | term_loan | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CRWV-100` | OEM financing (non-recourse) | Subsidiaries (obligors n/d) | term_loan | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CRWV-109` | Magnetar loan (MagAI deposit) | CoreWeave, Inc. | term_loan | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CRWV-110` | Operating leases | CoreWeave, Inc. / subsidiaries | lease | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CRWV-111` | Leases signed, not commenced | CoreWeave, Inc. / subsidiaries | lease | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CRWV-112` | Data-center JV funding commitments | Unconsolidated JV VIEs | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-CYRUSONE-113` | CYRUS 2023-1 A2 | CyrusOne Data Centers Issuer I LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CYRUSONE-114` | CYRUS 2023-1 B | CyrusOne Data Centers Issuer I LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CYRUSONE-115` | CYRUS 2023-2 A2 | CyrusOne Data Centers Issuer I LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CYRUSONE-116` | CYRUS 2024-1 A2 | CyrusOne Data Centers Issuer I LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CYRUSONE-117` | CYRUS 2024-2 A2 | CyrusOne Data Centers Issuer I LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CYRUSONE-118` | CYRUS 2024-3 A2 | CyrusOne Data Centers Issuer I LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CYRUSONE-119` | CYRUS 2025-1 A2 | CyrusOne Data Centers Issuer I LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CYRUSONE-120` | CONE 2024-DFW1 A | CONE Trust 2024-DFW1 (SASB CMBS) | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CYRUSONE-121` | CONE 2024-DFW1 E | CONE Trust 2024-DFW1 (SASB CMBS) | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CYRUSONE-122` | CONE 2026-DFW3 A | CONE CMT 2026-DFW3 (SASB CMBS) | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CYRUSONE-123` | CONE 2026-ACD6 A | CONE CMT 2026-ACD6 (SASB CMBS) | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CYRUSONE-124` | Term loan B | CyrusOne Holdco LLC | term_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CYRUSONE-125` | Revolving warehouse | CyrusOne Revolving Warehouse | facility | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-CYRUSONE-126` | KDCONE Dev Corp loan | KDCONE Dev Corp | term_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-DATABANK-127` | COLO 2021-2 A2 | DataBank Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-DATABANK-128` | COLO 2021-2 B | DataBank Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-DATABANK-129` | COLO 2023-1 A2 | DataBank Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-DATABANK-130` | COLO 2024-1 A2 | DataBank Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-DATABANK-131` | COLO 2026-1 A2 | DataBank Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-DATABANK-132` | COLO 2026-1 B | DataBank Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-DATABANK-133` | HYPER 2025-1 A2 | DataBank Issuer II LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-DATABANK-134` | HYPER 2025-1 B | DataBank Issuer II LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-DATABANK-135` | Oracle quick-draw construction loan | Oracle build-to-suit SPV | construction_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-EDGECONNEX-137` | EDGE 2022-1 A2 | EdgeConneX Data Centers Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-EDGECONNEX-138` | EDGE 2024-1 A2 | EdgeConneX Data Centers Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-EDGECONNEX-139` | EDGEU 2026-1 A2 (EUR) | EdgeConneX Holdings IV-D B.V. / Data Centers Europe B.V. | abs | 0 | no legacy price in the archive and no linked fund holding |
| `EX-FIRMUS-141` | 1L Term Loan (BCRED) | Firmus Supercloud Pty Ltd | term_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-FIRMUS-142` | DDTL (BCRED) | Firmus Supercloud Pty Ltd | ddtl | 0 | no legacy price in the archive and no linked fund holding |
| `EX-FIRMUS-143` | 1L Term Loan (BXSL) | Firmus Supercloud Pty Ltd | term_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-FIRMUS-144` | DDTL (BXSL) | Firmus Supercloud Pty Ltd | ddtl | 0 | no legacy price in the archive and no linked fund holding |
| `EX-FIRMUS-145` | Total senior secured package | Firmus Supercloud Pty Ltd | bond | 0 | no legacy price in the archive and no linked fund holding |
| `EX-FIRMUS-146` | Term Loan (FSCO) | Firmus Metal Singapore Pte Ltd | term_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-FIRMUS-147` | Term Loan (FSCI) | Firmus Metal Singapore Pte Ltd | term_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-FLEXENTIAL-148` | 2021 Class C | Flexential Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-FLEXENTIAL-149` | 2025-1 A2 | Flexential Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-FLEXENTIAL-150` | 2025-1 C | Flexential Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-FLEXENTIAL-151` | 2025-2 A2 | Flexential Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-FLEXENTIAL-152` | 2026-x A2 | Flexential Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-FLEXENTIAL-153` | 2026-3A A2 | Flexential Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-FLEXENTIAL-154` | Topco revolver | Flexential Topco Corporation | facility | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-FLUIDSTACK-155` | Macquarie GPU senior facility | Fluidstack GPU-financing entity (Iceland deployment) | facility | 0 | no legacy price in the archive and no linked fund holding |
| `EX-FLUIDSTACK-157` | Deferred purchase price to WULF (Abernathy 50.1%) | Fluidstack-led purchaser group | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-FLUIDSTACK-158` | Lease: WULF Lake Mariner CB-3/4/5 | Fluidstack USA I Inc | lease | 0 | no legacy price in the archive and no linked fund holding |
| `EX-FLUIDSTACK-159` | Lease: CIFR Barber Lake | Fluidstack USA II Inc | lease | 0 | no legacy price in the archive and no linked fund holding |
| `EX-FLUIDSTACK-160` | Lease: Abernathy | Fluidstack USA III Inc | lease | 0 | no legacy price in the archive and no linked fund holding |
| `EX-FLUIDSTACK-161` | Lease: Hut 8 River Bend | Fluidstack USA IV Inc | lease | 0 | no legacy price in the archive and no linked fund holding |
| `EX-GLXY-164` | 3.00% Exchangeable Sr Notes 2026 | Galaxy Digital Holdings LP | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-GLXY-165` | 2.50% Exchangeable Sr Notes 2029 | Galaxy Digital Holdings LP | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-GLXY-167` | Loans payable (trading) | Trading subs | term_loan | 0 | no legacy price in the archive and no linked fund holding |
| `EX-GLXY-168` | LUNA NYAG settlement | GDH Ltd successor | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-GLXY-169` | Helios II Completion Guarantee | Galaxy Digital Holdings LP | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-GLXY-170` | Helios I Sponsor Guaranties | Galaxy Digital Holdings LP | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-HUT-173` | JPM senior secured revolver | Hut 8 Corp. | facility | 0 | no legacy price in the archive and no linked fund holding |
| `EX-HUT-174` | FalconX BTC-collateralized term loan | Hut 8 Mining Corp. | term_loan | 0 | no legacy price in the archive and no linked fund holding |
| `EX-HUT-175` | Two Prime BTC-collateralized revolver | Wholly owned sub (n/d) | facility | 0 | no legacy price in the archive and no linked fund holding |
| `EX-HUT-176` | TZRC Secured Promissory Note | Hut 8 Corp. (assumed) | bond | 0 | no legacy price in the archive and no linked fund holding |
| `EX-HUT-177` | SAFE (other financial liability) | Consolidated subsidiary | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-IREN-178` | 3.25% Conv. Sr. Notes 2030 | IREN Limited | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-IREN-179` | 3.50% Conv. Sr. Notes 2029 | IREN Limited | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-IREN-180` | 0.00% Conv. Sr. Notes 2031 | IREN Limited | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-IREN-181` | 0.25% Conv. Sr. Notes 2032 | IREN Limited | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-IREN-182` | 1.00% Conv. Sr. Notes Jun-2033 | IREN Limited | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-IREN-186` | Mackenzie MFSA + Notes | IE Mackenzie Compute Ltd | bond | 0 | no legacy price in the archive and no linked fund holding |
| `EX-IREN-187` | GPU finance leases | IE CA Leasing Ltd. et al. | lease | 0 | no legacy price in the archive and no linked fund holding |
| `EX-IREN-188` | Other debt | n/d | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-IREN-189` | Remarketing-Right and MSA parent guarantees | IREN Limited | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-LAMBDA-191` | Senior secured credit facility (multi-tranche) | Lambda, Inc. | facility | 0 | no legacy price in the archive and no linked fund holding |
| `EX-LAMBDA-192` | Senior secured TLB | GPU SPV (unnamed) | term_loan | 0 | legacy px source is not a fund filing or press dealer quote |
| `EX-LAMBDA-193` | Senior secured DDTL (fixed) | Lambda, Inc. | ddtl | 0 | no legacy price in the archive and no linked fund holding |
| `EX-LAMBDA-194` | Short-term private debt (Nvidia chips for Microsoft) | n/d | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-LAMBDA-195` | Nvidia GPU leaseback (asset, not debt) | n/a | lease | 0 | no legacy price in the archive and no linked fund holding |
| `EX-NBIS-196` | 2.00% Conv. Sr. Notes | Nebius Group N.V. | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-NBIS-197` | 3.00% Conv. Sr. Notes | Nebius Group N.V. | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-NBIS-198` | 1.00% Conv. Sr. Notes | Nebius Group N.V. | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-NBIS-199` | 2.75% Conv. Sr. Notes | Nebius Group N.V. | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-NBIS-200` | 1.25% Conv. Sr. Notes | Nebius Group N.V. | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-NBIS-201` | 2.625% Conv. Sr. Notes | Nebius Group N.V. | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-NBIS-202` | 0.50% Conv. Sr. Notes due 2030 (Aug-26) | Nebius Group N.V. | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-NBIS-203` | 4.50% Conv. Sr. Notes due 2034 (Aug-26) | Nebius Group N.V. | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-NSCALE-205` | GPU Financing Facility (DDTL) | Nscale Services UK Ltd | ddtl | 0 | no legacy price in the archive and no linked fund holding |
| `EX-NSCALE-206` | Macquarie Senior Facility | Glomfjord GPU sub | facility | 0 | no legacy price in the archive and no linked fund holding |
| `EX-NSCALE-207` | Revolving Credit Facility | Nscale Global Holdings Ltd | facility | 0 | no legacy price in the archive and no linked fund holding |
| `EX-NSCALE-208` | Kvandal South DC TL + VAT facility | Nscale Norway DC DA | facility | 0 | no legacy price in the archive and no linked fund holding |
| `EX-NSCALE-209` | Macquarie Iceland DDTL | Nscale Services Iceland III Ehf | ddtl | 0 | no legacy price in the archive and no linked fund holding |
| `EX-NSCALE-213` | Convertible Loan Notes + Nvidia sale | Nscale Ltd | convert | 0 | no legacy price in the archive and no linked fund holding |
| `EX-NSCALE-214` | Operating lease liabilities | Nscale group | lease | 0 | no legacy price in the archive and no linked fund holding |
| `EX-NSCALE-215` | Deferred revenue (customer prepayments) | Nscale group | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-ORCL-216` | 2.80% due Apr-2027 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-217` | 3.25% due Nov-2027 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-218` | 2.30% due Mar-2028 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-219` | 4.50% due May-2028 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-220` | 4.80% due Aug-2028 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-221` | 4.55% due Feb-2029 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-222` | 4.20% due Sep-2029 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-223` | 6.15% due Nov-2029 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-224` | 2.95% due Apr-2030 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-225` | 4.65% due May-2030 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-226` | 3.25% due May-2030 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-227` | 4.45% due Sep-2030 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-229` | 2.875% due Mar-2031 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-230` | 5.25% due Feb-2032 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-231` | 4.80% due Sep-2032 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-232` | 6.25% due Nov-2032 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-233` | 4.90% due Feb-2033 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-234` | 5.35% due May-2033 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-235` | 4.30% due Jul-2034 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-236` | 4.70% due Sep-2034 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-237` | 3.90% due May-2035 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-238` | 5.50% due Aug-2035 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-241` | 3.85% due Jul-2036 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-242` | 3.80% due Nov-2037 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-243` | 6.50% due Apr-2038 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-244` | 6.125% due Jul-2039 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-245` | 3.60% due Apr-2040 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-246` | 5.375% due Jul-2040 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-247` | 3.65% due Mar-2041 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-248` | 4.50% due Jul-2044 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-249` | 4.125% due May-2045 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-250` | 5.875% due Sep-2045 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-251` | 6.55% due Feb-2046 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-252` | 4.00% due Jul-2046 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-253` | 4.00% due Nov-2047 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-255` | 3.95% due Mar-2051 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-256` | 6.90% due Nov-2052 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-257` | 5.55% due Feb-2053 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-258` | 5.375% due Sep-2054 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-259` | 4.375% due May-2055 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-260` | 6.00% due Aug-2055 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-263` | 3.85% due Apr-2060 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-264` | 4.10% due Mar-2061 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-265` | 5.50% due Sep-2064 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-266` | 6.125% due Aug-2065 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-267` | 6.10% due Sep-2065 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-268` | 6.85% due Feb-2066 Sr Notes | Oracle Corporation | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-ORCL-269` | FRN due Aug-2028 (SOFR+0.76%) | Oracle Corporation | other | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-ORCL-270` | FRN due Feb-2029 (SOFR+1.11%) | Oracle Corporation | other | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-ORCL-271` | Term Loan Credit Agreement 2 | Oracle Corporation | term_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-ORCL-272` | $10B 5-yr Revolver | Oracle Corporation | facility | 0 | no legacy price in the archive and no linked fund holding |
| `EX-ORCL-273` | Commercial paper | Oracle Corporation | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-ORCL-274` | Finance leases | Oracle + subsidiaries | lease | 0 | no legacy price in the archive and no linked fund holding |
| `EX-ORCL-275` | Operating leases (commenced) | Oracle + subsidiaries | lease | 0 | no legacy price in the archive and no linked fund holding |
| `EX-ORCL-276` | Leases not yet commenced | Oracle + subsidiaries | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-ORCL-277` | Guarantee of lessor borrowing | Oracle Corporation | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-ORCL-278` | 6.50% Series D Mandatory Convertible Preferred | Oracle Corporation | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-QTS-280` | QTS ABS I 2025-1 B | QTS Issuer ABS I, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-QTS-286` | QTS ABS II 2026-3 A2 | QTS Issuer ABS II, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-QTS-289` | QTS ABS II 2026-5 B | QTS Issuer ABS II, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-QTS-291` | QTS ABS II 2026-6 B | QTS Issuer ABS II, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-QTS-293` | BX 2024-VLT4 A | BX Trust 2024-VLT4 (CMBS) | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-QTS-294` | BX 2024-VLT5 A | BX 2024-VLT5 (CMBS) | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-QTS-295` | BX 2025-VLT6 A | BX Trust 2025-VLT6 (CMBS) | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-QTS-296` | BX 2025-VLT7 A | BX Trust 2025-VLT7 (CMBS) | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-QTS-297` | BX 2026-VLT9 A | BX 2026-VLT9 (portfolio CMBS) | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-QTS-298` | BX 2026-VLT10 A | BX 2026-VLT10 (1 loan / 1 property) | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-QTS-299` | BX 2026-VLT10 C | BX 2026-VLT10 (1 loan / 1 property) | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-QTS-300` | BX 2026-VLT10 D | BX 2026-VLT10 (1 loan / 1 property) | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-QTS-301` | BX 2026-VLT10 E | BX 2026-VLT10 (1 loan / 1 property) | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-SABEY-303` | SDCP 2022-1 A2 | Sabey DC Asset Holdings (SDCP issuer) | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-SABEY-304` | SDCP 2024-1 A2 | Sabey DC Asset Holdings (SDCP issuer) | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-SABEY-305` | SDCP 2025-1 A2 | Sabey DC Asset Holdings (SDCP issuer) | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-SABEY-306` | SDCP 2025-2 A2 | Sabey DC Asset Holdings (SDCP issuer) | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-SABEY-307` | SDCP 2026-1 A2 | Sabey DC Asset Holdings (SDCP issuer) | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-SOPAIPILLA-310` | Residual value guarantee (El Paso) | Meta Platforms, Inc. | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-SOPAIPILLA-311` | El Paso lease (initial term) | Meta Platforms, Inc. | lease | 0 | no legacy price in the archive and no linked fund holding |
| `EX-SOPAIPILLA-312` | Meta senior unsecured notes (5 issues) | Meta Platforms, Inc. | bond | 0 | no legacy price in the archive and no linked fund holding |
| `EX-STACK-313` | SIDC 2026-1 A-2 | Stack Infrastructure Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-STACK-314` | SIDC 2025-1 A2 | Stack Infrastructure Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-STACK-315` | SIDC 2023-2 A2 | Stack Infrastructure Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-STACK-316` | SIDC 2023-3 A2 | Stack Infrastructure Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-STACK-317` | SIDC 2024-1 A2 | Stack Infrastructure Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-STACK-318` | CMBS (NVA11A, Gainesville VA) | CMBS Facility SPV | cmbs | 0 | no legacy price in the archive and no linked fund holding |
| `EX-STACK-319` | Secured VFN | Stack Infrastructure Issuer, LLC | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-STACK-320` | Unsecured RCF | BODIT SPV LLC | facility | 0 | no legacy price in the archive and no linked fund holding |
| `EX-SWITCH-322` | Switch 2024-1 A2 | Switch ABS Issuer, LLC + Co-Issuer | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-SWITCH-323` | Switch 2024-1 B | Switch ABS Issuer, LLC + Co-Issuer | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-SWITCH-324` | Switch 2024-2 A2 | Switch ABS Issuer, LLC + Co-Issuer | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-SWITCH-325` | Switch 2024-2 B | Switch ABS Issuer, LLC + Co-Issuer | abs | 0 | no legacy price in the archive and no linked fund holding |
| `EX-SWITCH-326` | Switch 2024-2 C | Switch ABS Issuer, LLC + Co-Issuer | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-SWITCH-327` | Switch 2025-1 A2 | Switch ABS Issuer, LLC + Co-Issuer | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-SWITCH-328` | Switch 2025-1 B | Switch ABS Issuer, LLC + Co-Issuer | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-SWITCH-329` | Switch 2025-2 A21 | Switch ABS Issuer, LLC + Co-Issuer | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-SWITCH-330` | Switch 2025-2 A22 | Switch ABS Issuer, LLC + Co-Issuer | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-SWITCH-331` | Switch 2025-2 B | Switch ABS Issuer, LLC + Co-Issuer | abs | 0 | no legacy price in the archive and no linked fund holding |
| `EX-SWITCH-332` | Switch 2026-1 A21 | Switch ABS Issuer, LLC + Co-Issuer | abs | 0 | no legacy price in the archive and no linked fund holding |
| `EX-SWITCH-333` | Switch 2026-1 A22 | Switch ABS Issuer, LLC + Co-Issuer | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-SWITCH-334` | SWCH 2025-DATA A | SWCH Commercial Mortgage Trust 2025-DATA | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-SWITCH-335` | SWCH 2025-DATA B | SWCH Commercial Mortgage Trust 2025-DATA | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-SWITCH-336` | SWCH 2025-DATA C | SWCH Commercial Mortgage Trust 2025-DATA | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-SWITCH-337` | SWCH 2025-DATA D | SWCH Commercial Mortgage Trust 2025-DATA | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-SWITCH-338` | SWCH 2025-DATA E | SWCH Commercial Mortgage Trust 2025-DATA | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-SWITCH-339` | SWCH 2025-DATA F | SWCH Commercial Mortgage Trust 2025-DATA | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-TOGETHER-342` | Equipment financing (EPT) | Together Computer, Inc. | term_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-VANTAGE-343` | VDC 2020-2 A2 | Vantage Data Centers Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-VANTAGE-344` | VDC 2021-1 A2 | Vantage Data Centers Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-VANTAGE-345` | VDC 2024-1 A2 | Vantage Data Centers Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-VANTAGE-346` | VDC 2025-1 A2 | Vantage Data Centers Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-VANTAGE-347` | VDC 2025-2 A2 | Vantage Data Centers Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-VANTAGE-348` | VDC 2025-3 B | Vantage Data Centers Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-VANTAGE-349` | VDCR 2023-1 A2A | Retained Vantage Data Centers Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-VANTAGE-350` | VDCR 2023-1 B | Retained Vantage Data Centers Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-VANTAGE-351` | VDCR 2023-1 CAD tranche (XS2680929788) | Retained Vantage Data Centers Issuer, LLC | abs | 0 | no legacy price in the archive and no linked fund holding |
| `EX-VANTAGE-352` | VDCR 2023-2 A2 | Retained Vantage Data Centers Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-VANTAGE-353` | VDCR 2024-1 A2 | Retained Vantage Data Centers Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-VANTAGE-354` | VDCR 2024-1 B | Retained Vantage Data Centers Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-VANTAGE-355` | VDCR 2025-1 A2A | Retained Vantage Data Centers Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-VANTAGE-356` | VDCR 2025-1 second class | Retained Vantage Data Centers Issuer, LLC | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-VANTAGE-357` | VDCUK 2024-1 A2 (GBP) | Vantage DC Jersey Borrower SPV Ltd | abs | 0 | no legacy price in the archive and no linked fund holding |
| `EX-VANTAGE-358` | VDCUK 2024-1 B (GBP) | Vantage DC Jersey Borrower SPV Ltd | abs | 0 | no legacy price in the archive and no linked fund holding |
| `EX-VANTAGE-359` | VDCDE 2025-1 A2 (EUR) | Vantage DC Germany Borrower Lux | abs | 1 | held in seeded filings, but no linked line gives a usable mark (non-USD, unfunded, no par) |
| `EX-VANTAGE-360` | VDCM 2025-AZ D (CMBS) | VDCM 2025-AZ (Arizona) | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-VANTAGE-362` | 1L DDTL | Vantage Data Centers, LLC | ddtl | 0 | no legacy price in the archive and no linked fund holding |
| `EX-VANTAGE-363` | VDC Europe private ABS | Vantage Data Centers Europe S.a r.l. | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-VANTAGE-364` | $2B revolving warehouse | n/d | facility | 0 | no legacy price in the archive and no linked fund holding |
| `EX-VANTAGE-365` | Frontier / Port Washington project debt; 'Ares $2.4B loan' | Frontier (TX) / Lighthouse (WI) project cos | term_loan | 0 | no legacy price in the archive and no linked fund holding |
| `EX-VANTAGE_ORCL-368` | VANTAGE ORACLE WI loan (PGIM-held portion of the $38B) | Vantage Port Washington (WI) | construction_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-WULF-370` | Revolver (Morgan Stanley) | TeraWulf Inc. | facility | 0 | no legacy price in the archive and no linked fund holding |
| `EX-WULF-371` | 2.75% Convertible Sr Notes 2030 | TeraWulf Inc. | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-WULF-372` | 1.00% Convertible Sr Notes 2031 | TeraWulf Inc. | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-WULF-373` | 0% Convertible Sr Notes 2032 | TeraWulf Inc. | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `EX-WULF-374` | Muskie deferred + contingent consideration | TeraWulf (Muskie entities) | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-WULF-375` | Reimbursement agreement | TeraWulf Inc. | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-WULF-376` | Justified seller 6.8% put | Justified Data Campus Developer | other | 0 | no legacy price in the archive and no linked fund holding |
| `EX-XAI_VCI-377` | 5.350% Sr Notes due 2031 | Space Exploration Technologies Corp. | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `EX-XAI_VCI-378` | 5.650% Sr Notes due 2033 | Space Exploration Technologies Corp. | bond | 0 | legacy px source is not a fund filing or press dealer quote |
| `EX-XAI_VCI-379` | 5.875% Sr Notes due 2036 | Space Exploration Technologies Corp. | bond | 0 | legacy px source is not a fund filing or press dealer quote |
| `EX-XAI_VCI-380` | 6.600% Sr Notes due 2046 | Space Exploration Technologies Corp. | bond | 0 | legacy px source is not a fund filing or press dealer quote |
| `EX-XAI_VCI-381` | 6.650% Sr Notes due 2056 | Space Exploration Technologies Corp. | bond | 0 | legacy px source is not a fund filing or press dealer quote |
| `EX-XAI_VCI-382` | $5.0B Revolving Credit Facility | Space Exploration Technologies Corp. | facility | 0 | no legacy price in the archive and no linked fund holding |
| `EX-XAI_VCI-384` | Other equipment financings | Various subsidiaries | term_loan | 0 | no legacy price in the archive and no linked fund holding |
| `EX-XAI_VCI-385` | X 3.875% 2027 / 5.000% 2030 notes | X Corp. subsidiary | bond | 0 | no legacy price in the archive and no linked fund holding |
| `EX-XAI_VCI-386` | xAI securities-backed revolving line | xAI subsidiary | facility | 0 | no legacy price in the archive and no linked fund holding |
| `EX-XAI_VCI-387` | Finance lease liabilities | SpaceX consolidated | lease | 0 | no legacy price in the archive and no linked fund holding |
| `FWP-AEP-5.2-2036` | 5.20% Senior notes/bonds (per FWP) | AEP Texas Inc. | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `FWP-AEP-5.2-2036-614` | 5.20% Series Q, due 2036 | Southwestern Electric Power Co (AEP) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-AEP-5.25-2036` | 5.25% Senior notes/bonds (per FWP) | AEP Transmission Company, LLC | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `FWP-AEP-5.3-2033` | 5.30% Series P, due 2033 | Southwestern Electric Power Co (AEP) | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `FWP-AEP-5.375-2035` | 5.375% Senior notes/bonds (per FWP) | AEP Transmission Company, LLC | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `FWP-AEP-5.4-2034` | 5.40% Series O, due 2034 | Public Service Co of Oklahoma (AEP) | bond | 0 | no legacy price in the archive and no linked fund holding |
| `FWP-AEP-5.45-2036` | 5.45% Series N, due 2036 | Public Service Co of Oklahoma (AEP) | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `FWP-AEP-5.6-2056` | 5.60% Senior notes/bonds (per FWP) | Indiana Michigan Power Co (AEP) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-AEP-5.7-2034` | 5.70% Series O, due 2034 | AEP Texas Inc. | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-AEP-5.8-2056` | 5.800% Junior Subordinated Debentures Series C due 2056 | American Electric Power Co Inc | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-AEP-5.85-2055` | 5.85% Series P, due 2055 | AEP Texas Inc. | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `FWP-AEP-5.9-2056` | 5.90% Series R, due 2056 | Southwestern Electric Power Co (AEP) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-AEP-6-2037` | 6.00% Senior notes/bonds (per FWP) | Southwestern Electric Power Co (AEP) | bond | 0 | no legacy price in the archive and no linked fund holding |
| `FWP-AEP-6.05-2056` | 6.050% Junior Subordinated Debentures Series D due 2056 | American Electric Power Co Inc | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-AEP-6.3-2056` | 6.30% Series P, due 2056 | Public Service Co of Oklahoma (AEP) | bond | 0 | no legacy price in the archive and no linked fund holding |
| `FWP-CNP-4.8-2030` | 4.80% Senior notes/bonds (per FWP) | CenterPoint Energy Houston Electric LLC | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-CNP-4.85-2036` | 4.85% Senior notes/bonds (per FWP) | CenterPoint Energy Houston Electric LLC | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-CNP-4.95-2035` | 4.95% Senior notes/bonds (per FWP) | CenterPoint Energy Houston Electric LLC | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-CNP-5.4-2031` | 5.40% Senior notes/bonds (per FWP) | CenterPoint Energy Houston Electric LLC | bond | 0 | no legacy price in the archive and no linked fund holding |
| `FWP-CNP-5.95-2056` | 5.950% Junior Subordinated Notes, Series D, due 2056 | CenterPoint Energy, Inc. | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `FWP-CNP-6.4-2058` | 6.400% Junior Subordinated Notes, Series E, due 2058 | CenterPoint Energy, Inc. | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-DOMINION-4.6-2028` | 4.60% 2025 Series C 4.60% Senior Notes due 2028 | Dominion Energy, Inc. | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `FWP-DOMINION-5-2030` | 5.00% 2025 Series A 5.00% Senior Notes due 2030 | Dominion Energy, Inc. | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `FWP-DOMINION-5.35-2036` | 5.35% 2026 Series A 5.35% Senior Notes due 2036 | Dominion Energy, Inc. | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `FWP-DOMINION-5.45-2035` | 5.45% 2025 Series B 5.45% Senior Notes due 2035 | Dominion Energy, Inc. | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `FWP-DOMINION_VEPCO-4.9-2035` | 4.90% 2025 Series C 4.90% Senior Notes due 2035 | Virginia Electric & Power Co (Dominion) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-DOMINION_VEPCO-4.95-2036` | 4.95% aggregate principal amount of 2026 Series A 4.95% Senior Notes | Virginia Electric & Power Co (Dominion) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-DOMINION_VEPCO-5.15-2035` | 5.15% 2025 Series A 5.15% Senior Notes due 2035 | Virginia Electric & Power Co (Dominion) | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `FWP-DOMINION_VEPCO-5.6-2055` | 5.60% 2025 Series D 5.60% Senior Notes due 2055 | Virginia Electric & Power Co (Dominion) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-DOMINION_VEPCO-5.65-2036` | 5.65% 2026 Series C 5.65% Senior Notes due 2036 | Virginia Electric & Power Co (Dominion) | bond | 0 | no legacy price in the archive and no linked fund holding |
| `FWP-DOMINION_VEPCO-5.65-2055` | 5.65% 2025 Series B 5.65% Senior Notes due 2055 | Virginia Electric & Power Co (Dominion) | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `FWP-DOMINION_VEPCO-5.7-2056` | 5.70% 2026 Series B 5.70% Senior Notes due 2056 | Virginia Electric & Power Co (Dominion) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-DOMINION_VEPCO-6.3-2056` | 6.30% 2026 Series D 6.30% Senior Notes due 2056 | Virginia Electric & Power Co (Dominion) | bond | 0 | no legacy price in the archive and no linked fund holding |
| `FWP-DTE-4.25-2027` | 4.250% 2025 Series D 4.250% General and Refunding Mortgage Bonds | DTE Electric Co | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-DTE-4.85-2036` | 4.850% 2026 Series A 4.850% General and Refunding Mortgage Bonds | DTE Electric Co | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-DTE-4.875-2028` | 4.875% aggregate principal amount of the Issuer’s 2023 Series C 4.875% Senior No | DTE Energy Co | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-DTE-5.2-2030` | 5.200% 2025 Series A 5.200% Senior Notes due 2030 | DTE Energy Co | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-DTE-5.25-2035` | 5.250% aggregate principal amount of the Issuer’s 2025 Series B 5.250% General a | DTE Electric Co | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-DTE-5.55-2056` | 5.550% 2026 Series B 5.550% General and Refunding Mortgage Bonds | DTE Electric Co | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-DTE-5.85-2055` | 5.850% aggregate principal amount of the Issuer’s 2025 Series C 5.850% General a | DTE Electric Co | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-ENTERGY_AR-4.95-2036` | 4.95% First Mortgage Bonds, 4.95% Series | Entergy Arkansas, LLC | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `FWP-ENTERGY_AR-5.45-2034` | 5.45% First Mortgage Bonds, 5.45% Series | Entergy Arkansas, LLC | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-ENTERGY_AR-5.75-2056` | 5.75% First Mortgage Bonds, 5.75% Series | Entergy Arkansas, LLC | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `FWP-ENTERGY_LA-4.9-2036` | 4.90% Collateral Trust Mortgage Bonds 4.90% Series | Entergy Louisiana, LLC | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-ENTERGY_LA-5.5-2036` | 5.50% Collateral Trust Mortgage Bonds 5.50% Series | Entergy Louisiana, LLC | bond | 0 | no legacy price in the archive and no linked fund holding |
| `FWP-ENTERGY_LA-5.65-2056` | 5.65% Collateral Trust Mortgage Bonds 5.65% Series | Entergy Louisiana, LLC | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-ENTERGY_LA-5.8-2055` | 5.80% Collateral Trust Mortgage Bonds, 5.80% Series | Entergy Louisiana, LLC | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-ENTERGY_LA-6.2-2056` | 6.20% Collateral Trust Mortgage Bonds 6.20% Series | Entergy Louisiana, LLC | bond | 0 | no legacy price in the archive and no linked fund holding |
| `FWP-ENTERGY_MS-5.05-2036` | 5.05% First Mortgage Bonds, 5.05% Series | Entergy Mississippi, LLC | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-ENTERGY_MS-5.8-2055` | 5.80% First Mortgage Bonds, 5.80% Series | Entergy Mississippi, LLC | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-ENTERGY_TX-5.2-2036` | 5.20% First Mortgage Bonds, 5.20% Series | Entergy Texas, Inc. | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-ENTERGY_TX-5.25-2035` | 5.25% First Mortgage Bonds, 5.25% Series | Entergy Texas, Inc. | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `FWP-EQIX-4.75-2035` | 4.750% of 4.750% Senior Notes | Equinix, Inc. (and finance subs) | bond | 0 | no legacy price in the archive and no linked fund holding |
| `FWP-GEORGIA_POWER-4.6-2029` | 4.60% Series 2026B 4.60% Senior Notes | Georgia Power Co | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `FWP-GEORGIA_POWER-4.85-2031` | 4.85% of Series 2025B 4.85% Senior Notes | Georgia Power Co | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-GEORGIA_POWER-5.2-2035` | 5.20% Series 2025C 5.20% Senior Notes | Georgia Power Co | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-SOUTHERN-6-2058` | 6.00% Notes | Southern Co | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `FWP-SOUTHERN-6.375-2055` | 6.375% Notes | Southern Co | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `FWP-SOUTHERN-6.5-2085` | 6.50% principal amount of the Series 2025A 6.50% Junior Subordinated Notes | Southern Co | bond | 0 | no legacy price in the archive and no linked fund holding |
| `FWP-WEC-3.95-2029` | 3.95% 3.95% Debentures due March 1, 2029 | Wisconsin Electric Power Co (We Energies) | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `FWP-WEC-4.15-2030` | 4.15% 4.15% Debentures due October 15, 2030 | Wisconsin Electric Power Co (We Energies) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-WEC-4.65-2031` | 4.65% 4.65% Debentures due June 15, 2031 | Wisconsin Electric Power Co (We Energies) | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `FWP-WEC-4.75-2028` | 4.75% 4.75% Debentures due January 15, 2028 | WEC Energy Group | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-WEC-5.1-2036` | 5.10% 5.10% Debentures due June 15, 2036 | Wisconsin Electric Power Co (We Energies) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-WEC-5.625-2056` | 5.625% Notes | WEC Energy Group | bond | 0 | no legacy price in the archive and no linked fund holding |
| `FWP-WEC-5.65-2056` | 5.65% 5.65% Debentures due March 15, 2056 | Wisconsin Electric Power Co (We Energies) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-XCEL-4.15-2029` | 4.15% Notes | Public Service Co of Colorado (Xcel) | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `FWP-XCEL-4.75-2028` | 4.75% Notes | Xcel Energy Inc | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `FWP-XCEL-4.85-2036` | 4.85% Notes | Northern States Power Co (MN) (Xcel) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-XCEL-5.05-2035` | 5.05% Notes | Northern States Power Co (MN) (Xcel) | bond | 0 | no legacy price in the archive and no linked fund holding |
| `FWP-XCEL-5.05-2036` | 5.05% Notes | Public Service Co of Colorado (Xcel) | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `FWP-XCEL-5.3-2035` | 5.30% Notes | Southwestern Public Service Co (Xcel) | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `FWP-XCEL-5.3-2036` | 5.300% Notes | Southwestern Public Service Co (Xcel) | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `FWP-XCEL-5.35-2034` | 5.35% (Reopening of 5.35% First Mortgage Bonds, Series No. 41 | Public Service Co of Colorado (Xcel) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-XCEL-5.55-2056` | 5.55% Notes | Northern States Power Co (MN) (Xcel) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-XCEL-5.6-2035` | 5.60% Notes | Xcel Energy Inc | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `FWP-XCEL-5.65-2054` | 5.65% (Reopening of 5.65% First Mortgage Bonds, Series | Northern States Power Co (WI) (Xcel) | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `FWP-XCEL-5.65-2055` | 5.65% Notes | Northern States Power Co (MN) (Xcel) | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `FWP-XCEL-5.85-2055` | 5.85% (Reopening of 5.85% First Mortgage Bonds, Series No. 43 | Public Service Co of Colorado (Xcel) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `FWP-XCEL-5.875-2056` | 5.875% Notes | Southwestern Public Service Co (Xcel) | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `FWP-XCEL-6.25-2085` | 6.25% Notes | Xcel Energy Inc | bond | 0 | no legacy price in the archive and no linked fund holding |
| `MAN-APLD-DevCodevelopmentloanfaci` | DevCo development loan facility (Macquarie Equipment Capital) | Applied Digital DevCo (APLD) | facility | 0 | no legacy price in the archive and no linked fund holding |
| `MAN-CEG-CCFCFirstLienSeniorSecur` | CCFC First Lien Senior Secured Term Loan | Calpine (CEG sub) - CCFC | term_loan | 0 | no legacy price in the archive and no linked fund holding |
| `MAN-CEG-DOELoanProgramsOfficeloa` | DOE Loan Programs Office loan (Crane Clean Energy Center restart) | Constellation Energy Generation LLC | term_loan | 0 | no legacy price in the archive and no linked fund holding |
| `MAN-CEG-GPCFirstLienSeniorSecure` | GPC First Lien Senior Secured Term Loan | Calpine (CEG sub) - GPC | term_loan | 0 | no legacy price in the archive and no linked fund holding |
| `MAN-CLOUDHQ-LOHRASPII-2026-1` | Series 2026-1 Notes (single class; data center ABS) | Lohrasp Enterprise II LLC (CloudHQ; manager Cloud Capital Advisors LLC | abs | 0 | no legacy price in the archive and no linked fund holding |
| `MAN-DLR-Constructionloan` | Construction loan | Digital Realty Trust, L.P. | construction_loan | 0 | no legacy price in the archive and no linked fund holding |
| `MAN-EDGECONNEX-Proposedconstructiondebt` | Proposed construction debt (possibly 144A private placement) | EdgeConneX (EQT) - Cedar Creek campus | construction_loan | 0 | no legacy price in the archive and no linked fund holding |
| `MAN-EQIX-AMER2loanxScalehyperscal` | AMER 2 loan (xScale hyperscale JV) | Equinix xScale AMER 2 | abs | 0 | no legacy price in the archive and no linked fund holding |
| `MAN-EQIX-EMEA2jointventurecreditf` | EMEA 2 joint-venture credit facility (xScale hyperscale) | Equinix xScale EMEA 2 JV | abs | 0 | no legacy price in the archive and no linked fund holding |
| `MAN-FIRMUS-Blackstoneleddebtfinanci` | Blackstone-led debt financing facility (Project Southgate) | Firmus Technologies / Sustainable Data Centres (Project Southgate) | facility | 0 | no legacy price in the archive and no linked fund holding |
| `MAN-HUT-BeaconPointPhase2project` | Beacon Point Phase 2 project financing - not yet arranged | Beacon Point DC Phase 2 (Hut 8) | term_loan | 0 | no legacy price in the archive and no linked fund holding |
| `MAN-MARA-SeniorSecuredNotesdue203` | Senior Secured Notes due 2032 (Long Ridge) | MARA Holdings - Long Ridge | bond | 0 | no legacy price in the archive and no linked fund holding |
| `MAN-MARA-Termloanfacility765` | Term loan facility (7.65%) | MARA Holdings | term_loan | 0 | no legacy price in the archive and no linked fund holding |
| `MAN-NRG-725LightningSeniorSecure` | 7.25% Lightning Senior Secured Notes due 2032 | Lightning Power (NRG sub; ex-LS Power assets) | bond | 0 | no legacy price in the archive and no linked fund holding |
| `MAN-NRG-LightningTermLoandue2031` | Lightning Term Loan due 2031 | Lightning Power (NRG sub) | term_loan | 0 | no legacy price in the archive and no linked fund holding |
| `MAN-QTS-BankguaranteeLCbackstopf` | Bank guarantee / LC backstop for utility fees (in talks) | QTS (Blackstone) - utility-fee guarantee facility | other | 0 | no legacy price in the archive and no linked fund holding |
| `MAN-QTS-Seniorsecuredtermloansin` | Senior secured term loans (incl. $3.25B incremental; MIS total up to ~$7.6B) | QTS Thunder Managing Issuer LLC (MIS) | term_loan | 0 | no legacy price in the archive and no linked fund holding |
| `MAN-SMCI-700SeriesAMandatoryConve` | 7.00% Series A Mandatory Convertible Preferred Stock (75,000,000 depositary shar | Super Micro Computer, Inc. | other | 0 | no legacy price in the archive and no linked fund holding |
| `MAN-SPCX-Proposed40BApolloleddebt` | Proposed ~$40B Apollo-led debt for Nvidia GPU purchases (~$10B bank loans + ~$30 | SpaceX (xAI) - proposed GPU financing | term_loan | 0 | no legacy price in the archive and no linked fund holding |
| `MAN-VST-SeniorSecuredTermLoanB3` | Senior Secured Term Loan B-3 | Vistra Operations Co LLC | term_loan | 0 | no legacy price in the archive and no linked fund holding |
| `MAN-VST-SeriesA7000FixedtoFixedR` | Series A 7.000% Fixed-to-Fixed Reset Junior Subordinated Notes due 2057 (first r | Vistra Operations Company LLC (guarantor Vistra Corp.) | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `MAN-VST-SeriesB7250FixedtoFixedR` | Series B 7.250% Fixed-to-Fixed Reset Junior Subordinated Notes due 2057 | Vistra Operations Company LLC (guarantor Vistra Corp.) | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `MAN-WULF-Justifiedprojectfinancin` | Justified project financing - not yet closed (Anthropic lease) | TeraWulf - Justified Data Campus (Kentucky) | lease | 0 | no legacy price in the archive and no linked fund holding |
| `MAN-YONDR-6875SeniorSecuredNotesdu` | 6.875% Senior Secured Notes due 2031 | Yondr JK 1 LLC (indirect sub of DB Holding 12 S.a r.l.) | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-BE-093712AK3` | Convertible senior notes: BE 3 06/01/28 | Bloom Energy Corp | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-BE-093712AM9` | Convertible senior notes: BE 3 06/01/29 | Bloom Energy Corp | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-BE-093712AN7` | Convertible senior notes: BE 0 11/15/30 | Bloom Energy Corp | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-BTDR-09175RAB6` | Convertible senior notes: Bitdeer Technologies Group | Bitdeer Technologies Group | convert | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-BTDR-09175RAD2` | Convertible senior notes: BTDR 4.875 07/01/31 | Bitdeer Technologies Group | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-BTDR-09175RAE0` | Convertible senior notes: Bitdeer Technologies Group | Bitdeer Technologies Group | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-BTDR-09175RAG5` | Convertible senior notes: Bitdeer Technologies Group | Bitdeer Technologies Group | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-CEG-210385AB6` | Senior notes: Constellation Energy Generation LLC | Constellation Energy Generation LLC | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-CEG-210385AC4` | Senior notes: Constellation Energy Generation LLC | Constellation Energy Generation LLC | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-CEG-210385AD2` | Senior notes: Constellation Energy Generation LLC | Constellation Energy Generation LLC | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-CEG-210385AE0` | Senior notes: Constellation Energy Generation LLC | Constellation Energy Generation LLC | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-CEG-210385AF7` | Senior notes: Constellation Energy Generation LLC | Constellation Energy Generation LLC | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-CEG-210385AG5` | Senior notes: Constellation Energy Generation LLC | Constellation Energy Generation LLC | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-CEG-210385AJ9` | Senior notes: CONSTL ENRGY GEN | Constellation Energy Generation LLC | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-CEG-210385AM2` | Senior notes: Constellation Energy Generation LLC | Constellation Energy Generation LLC | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-CEG-210385AN0` | Senior notes: Constellation Energy Generation LLC | Constellation Energy Generation LLC | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-CEG-210385AP5` | Senior notes: Constellation Energy Generation LLC | Constellation Energy Generation LLC | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-CEG-210385AQ3` | Senior notes: Constellation Energy Generation LLC | Constellation Energy Generation LLC | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-CEG-210385AR1` | Senior notes: CEG 5 7/8 01/15/66 | Constellation Energy Generation LLC | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-CEG-210385AS9` | Senior notes: Constellation Energy Generation LLC | Constellation Energy Generation LLC | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-CEG-210385AT7` | Senior notes: Constellation Energy Generation LLC | Constellation Energy Generation LLC | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-CEG-210385AU4` | Senior notes: Constellation Energy Generation LLC | Constellation Energy Generation LLC | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-CEG-30161MAG8` | Senior notes: Constellation Energy Generation LLC | Constellation Energy Generation LLC | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-CEG-30161MAJ2` | Senior notes: Constellation Energy Generation LLC | Constellation Energy Generation LLC | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-CEG-30161MAN3` | Senior notes: Constellation Energy Generation LLC | Constellation Energy Generation LLC | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-CENTERSQUARE-15201EAA2` | ABS/CMBS notes: CNSQ 2024-1A A2 | Centersquare Issuer LLC | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-CENTERSQUARE-15201EAB0` | ABS/CMBS notes: CENTERSQUARE ISSUER LLC CNSQ 2024-1A B | Centersquare Issuer LLC | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-CENTERSQUARE-15201EAD6` | ABS/CMBS notes: CENTERSQUARE ISSUER LLC CNSQ 2025-1A A2 | Centersquare Issuer LLC | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-CENTERSQUARE-15201EAF1` | ABS/CMBS notes: Centersquare Issuer LLC | Centersquare Issuer LLC | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-CENTERSQUARE-15201EAK0` | ABS/CMBS notes: Centersquare Issuer LLC | Centersquare Issuer LLC | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-CENTERSQUARE-15201EAN4` | ABS/CMBS notes: CNSQ 2025-5A A2 | Centersquare Issuer LLC | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-CENTERSQUARE-15201EAR5` | ABS/CMBS notes: Centersquare Issuer LLC | Centersquare Issuer LLC | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-CLSK-18452BAC4` | Convertible senior notes: Cleanspark, Inc. | CleanSpark, Inc. | convert | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-CLSK-18452BAD2` | Convertible senior notes: Cleanspark Inc | CleanSpark, Inc. | convert | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-DLR-25389JAR7` | Senior notes: Digital Realty Trust LP | Digital Realty Trust, L.P. | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-DLR-25389JAT3` | Senior notes: Digital Realty Trust LP | Digital Realty Trust, L.P. | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-DLR-25389JAU0` | Senior notes: Digital Realty Trust LP | Digital Realty Trust, L.P. | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-DLR-25389JAV8` | Senior notes: Digital Realty Trust LP | Digital Realty Trust, L.P. | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-DLR-25389JAX4` | Senior notes: Digital Realty Trust LP | Digital Realty Trust, L.P. | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-DLR-46855538` | Senior notes: DLR 1 01/15/32 | Digital Realty Trust, L.P. | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-EQIX-19456892` | Senior notes: Equinix Asia Financing Corp. Pte. Ltd. | Equinix, Inc. (and finance subs) | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-EQIX-23135826` | Senior notes: EQUINIX EU 2 FIN | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-EQIX-29390XAA2` | Senior notes: Equinix Europe 2 Financing Corp LLC | Equinix, Inc. (and finance subs) | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-EQIX-29390XAB0` | Senior notes: EQUINIX EUROPE 2 FINANCING CORP LLC | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-EQIX-29390XAC8` | Senior notes: EQUINIX EU 2 FIN | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-EQIX-29390XAD6` | Senior notes: EQUINIX EUROPE 2 FINANCING CORP LLC | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-EQIX-29390XAE4` | Senior notes: EQUINIX EUROPE 2 FINANCING CORP LLC | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-EQIX-29390XAG9` | Senior notes: Equinix Europe 2 Financing Corp LLC | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-EQIX-29390XAH7` | Senior notes: Equinix Europe 2 Financing Corp LLC | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-EQIX-29390XAK0` | Senior notes: Equinix Europe 2 Financing Corp LLC | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-EQIX-29444UBD7` | Senior notes: Equinix Inc | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-EQIX-29444UBE5` | Senior notes: Equinix Inc | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-EQIX-29444UBG0` | Senior notes: Equinix Inc | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-EQIX-29444UBH8` | Senior notes: Equinix Inc | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-EQIX-29444UBJ4` | Senior notes: Equinix Inc | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-EQIX-29444UBL9` | Senior notes: Equinix Inc | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-EQIX-29444UBM7` | Senior notes: Equinix Inc | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-EQIX-29444UBP0` | Senior notes: EQUINIX INC | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-EQIX-29444UBR6` | Senior notes: Equinix Inc | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-EQIX-29444UBS4` | Senior notes: Equinix Inc | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-EQIX-29444UBT2` | Senior notes: Equinix Inc | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-EQIX-29444UBU9` | Senior notes: Equinix, Inc. | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-EQIX-29444UBV7` | Senior notes: Equinix Inc | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-EQIX-29444UBX3` | Senior notes: Equinix Inc | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-EQIX-29444UBY1` | Senior notes: Equinix Inc | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-EQIX-29447JAA6` | Senior notes: Equinix Canada Financing Ltd | Equinix, Inc. (and finance subs) | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-EQIX-29447JAB4` | Senior notes: Equinix Canada Financing Ltd | Equinix, Inc. (and finance subs) | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-EQIX-29447KAA3` | Senior notes: Equinix Asia Financing Corp Pte Ltd | Equinix, Inc. (and finance subs) | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-EQIX-29947852` | Senior notes: Equinix, Inc. | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-EQIX-31038551` | Senior notes: Equinix Europe 2 Financing Corp LLC | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-EQIX-53140214` | Senior notes: EQUINIX EUROPE | Equinix, Inc. (and finance subs) | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-EQIX-95919401` | Senior notes: Equinix Asia Financing Corp. Pte. Ltd. | Equinix, Inc. (and finance subs) | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-EQIX_XS-11751811` | ABS/CMBS notes: Equinix Xscale Secured Funding 2026-1 DAC | Equinix xScale Secured Funding 2026-1 DAC | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-GLOBAL_SWITCH-71147586` | Senior notes: Global Switch Holdings Ltd | Global Switch Finance B.V. / Global Switch Holdings | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-GLOBAL_SWITCH-76743158` | Senior notes: Global Switch Finance BV | Global Switch Finance B.V. / Global Switch Holdings | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-LANDMARK_DC-538908AC8` | ABS/CMBS notes: LMDV ISSUER CO LLC LMDV 2025-1A B | LMDV Issuer Co LLC (Landmark data center ABS) | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-LANDMARK_DC-538908AE4` | ABS/CMBS notes: LMDV ISSUER CO LLC LMDV 2025-1A C | LMDV Issuer Co LLC (Landmark data center ABS) | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-MARA-565788AB2` | Convertible senior notes: MARA Holdings Inc | MARA Holdings, Inc. | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-MARA-565788AD8` | Convertible senior notes: MARA Holdings Inc | MARA Holdings, Inc. | convert | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-MARA-565788AF3` | Convertible senior notes: MARA Holdings Inc | MARA Holdings, Inc. | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-MARA-565788AH9` | Convertible senior notes: MARA Holdings Inc | MARA Holdings, Inc. | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-MARA-565788AJ5` | Convertible senior notes: MARA Holdings Inc 0.0%, Due 08/01/2032 | MARA Holdings, Inc. | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-NRG-629377CE0` | Senior notes: NRG Energy Inc | NRG Energy, Inc. | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-NRG-629377CH3` | Senior notes: NRG Energy Inc | NRG Energy, Inc. | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-NRG-629377CL4` | Senior notes: NRG Energy Inc | NRG Energy, Inc. | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-NRG-629377CP5` | Senior notes: NRG Energy Inc | NRG Energy, Inc. | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-NRG-629377CQ3` | Senior notes: NRG Energy Inc | NRG Energy, Inc. | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-NRG-629377CR1` | Senior notes: NRG Energy Inc | NRG Energy, Inc. | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-NRG-629377CS9` | Senior notes: NRG Energy Inc | NRG Energy, Inc. | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-NRG-629377CT7` | Senior notes: NRG Energy Inc | NRG Energy, Inc. | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-NRG-629377CU4` | Senior notes: NRG Energy Inc | NRG Energy, Inc. | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-NRG-629377CW0` | Senior notes: NRG Energy Inc | NRG Energy, Inc. | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-NRG-629377CX8` | Senior notes: NRG Energy Inc | NRG Energy, Inc. | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-NRG-629377CY6` | Senior notes: NRG Energy, Inc. | NRG Energy, Inc. | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-NRG-629377DA7` | Senior notes: NRG Energy Inc | NRG Energy, Inc. | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-NRG-629377DB5` | Senior notes: NRG Energy Inc | NRG Energy, Inc. | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-NRG-629377DC3` | Senior notes: NRG Energy Inc | NRG Energy, Inc. | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-NRG-629377DD1` | Senior notes: NRG Energy Inc | NRG Energy, Inc. | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-NRG-629377DE9` | Senior notes: NRG Energy, Inc. | NRG Energy, Inc. | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-NRG-629377DF6` | Senior notes: NRG Energy Inc | NRG Energy, Inc. | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-NRG-629377DG4` | Senior notes: NRG Energy Inc | NRG Energy, Inc. | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-NRG-62937NBC0` | Term loan: NRG Energy Inc 2024 Term Loan | NRG Energy, Inc. | term_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-QTS-05612TAE5` | ABS/CMBS notes: BX 2024-VLT4 C | Bx Trust 2024-Vlt4 | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-QTS-05619PAE6` | ABS/CMBS notes: QTS Data Centers - BX 2025-VOLT C | Qts Data Centers - Bx 2025-Volt C | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-QTS-48279104` | Term loan: QTS GOOD NEWS FACILITY TL | Qts Good News Facility Tl | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-QTS-70636906` | ABS/CMBS notes: QTS GOOD NEWS FCLTY REVR | Qts Good News Fclty Revr | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-QTS-74758JAB3` | Term loan: QTS TL 1L USD | Qualitytech Lp | abs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-QTS-86642774` | ABS/CMBS notes: QTS IRVING DC3 LLC | Qts Irving Dc3 Llc | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-RIOT-767292AB1` | Convertible senior notes: Riot Platforms Inc | Riot Platforms, Inc. | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-SABEY-78520EAB2` | ABS/CMBS notes: SABEY DATA CENTER ISSUER LLC | Sabey Data Center Issuer LLC | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-SMCI-86800UAD6` | Convertible senior notes: Super Micro Computer Inc | Super Micro Computer, Inc. | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-SMCI-86800UAF1` | Convertible senior notes: Super Micro Computer Inc | Super Micro Computer, Inc. | convert | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-TIERPOINT-88651CAE0` | ABS/CMBS notes: TierPoint Issuer LLC, Series 2025-1A, Class A2 | TierPoint Issuer LLC | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-TIERPOINT-88651CAG5` | ABS/CMBS notes: TierPoint Issuer LLC, Series 2025-2A, Class A2 | TierPoint Issuer LLC | cmbs | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-TLN-87422LAV2` | Term loan: Talen Energy Supply LLC 2023 Term Loan B | Talen Energy Supply LLC | term_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-TLN-87422LAY6` | Term loan: Talen Energy Supply LLC 2024-1 Incremental Term Loan | Talen Energy Supply LLC | term_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-TLN-87422LBA7` | Term loan: TLN TL B 1L USD | Talen Energy Supply LLC | term_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-TLN-87422VAK4` | Senior notes: Talen Energy Supply LLC | Talen Energy Supply LLC | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-TLN-87422VAM0` | Senior notes: Talen Energy Supply LLC | Talen Energy Supply LLC | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-TLN-87422VAN8` | Senior notes: Talen Energy Supply LLC | Talen Energy Supply LLC | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `NP-TLN-87422VAP3` | Senior notes: Talen Energy Supply LLC | Talen Energy Supply LLC | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-TLN-87422VAQ1` | Senior notes: Talen Energy Supply LLC | Talen Energy Supply LLC | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-VST-56060649` | Term loan: Vistra Zero Opt Co, LLC 3/20/2031 | Vistra Operations Co LLC / Vistra Corp | term_loan | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-VST-92840MAB8` | Senior notes: VST 8 PERP | Vistra Operations Co LLC / Vistra Corp | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-VST-92840MAC6` | Senior notes: Vistra Corp | Vistra Operations Co LLC / Vistra Corp | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-VST-92840MAD4` | Senior notes: Vistra Corp | Vistra Operations Co LLC / Vistra Corp | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-VST-92840VAB8` | Senior notes: Vistra Operations Co. LLC | Vistra Operations Co LLC / Vistra Corp | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-VST-92840VAE2` | Senior notes: Vistra Operations Co LLC | Vistra Operations Co LLC / Vistra Corp | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-VST-92840VAF9` | Senior notes: Vistra Operations Co LLC | Vistra Operations Co LLC / Vistra Corp | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-VST-92840VAG7` | Senior notes: Vistra Operations Co LLC | Vistra Operations Co LLC / Vistra Corp | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-VST-92840VAH5` | Senior notes: VISTRA OPERATIONS CO LLC | Vistra Operations Co LLC / Vistra Corp | bond | 0 | legacy px was a Börse Frankfurt fixing (needs licence) |
| `NP-VST-92840VAP7` | Senior notes: Vistra Operations Co. LLC | Vistra Operations Co LLC / Vistra Corp | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-VST-92840VAQ5` | Senior notes: Vistra Operations Co LLC | Vistra Operations Co LLC / Vistra Corp | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-VST-92840VAR3` | Senior notes: Vistra Operations Co. LLC | Vistra Operations Co LLC / Vistra Corp | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-VST-92840VAS1` | Senior notes: Vistra Operations Co LLC | Vistra Operations Co LLC / Vistra Corp | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-VST-92840VAT9` | Senior notes: Vistra Operations Co LLC | Vistra Operations Co LLC / Vistra Corp | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-VST-92840VAU6` | Senior notes: VST 5.7 12/30/34 | Vistra Operations Co LLC / Vistra Corp | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-VST-92840VAV4` | Senior notes: Vistra Operations Co LLC | Vistra Operations Co LLC / Vistra Corp | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-VST-92840VAW2` | Senior notes: Vistra Operations Co. LLC | Vistra Operations Co LLC / Vistra Corp | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-VST-92840VAX0` | Senior notes: Vistra Operations Co LLC | Vistra Operations Co LLC / Vistra Corp | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-VST-92840VAY8` | Senior notes: Vistra Operations Co. LLC | Vistra Operations Co LLC / Vistra Corp | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-VST-92840VAZ5` | Senior notes: Vistra Operations Co LLC | Vistra Operations Co LLC / Vistra Corp | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-VST-92840VBA9` | Senior notes: Vistra Operations Co LLC | Vistra Operations Co LLC / Vistra Corp | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-VST-92840VBB7` | Senior notes: Vistra Operations Co LLC | Vistra Operations Co LLC / Vistra Corp | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-VST-92840VBC5` | Senior notes: Vistra Operations Co. LLC | Vistra Operations Co LLC / Vistra Corp | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `NP-VST-92840VBD3` | Senior notes: Vistra Operations Co. LLC | Vistra Operations Co LLC / Vistra Corp | bond | 0 | legacy px cites a fund filing, but no line for it is in the archive |
| `XB-CEG-GPCFirstLienSeniorSecuredTermLoanSubFaci` | G P C First Lien Senior Secured Term Loan Sub Facility (XBRL member) | Constellation Energy Generation LLC | facility | 0 | no legacy price in the archive and no linked fund holding |
| `XB-CEG-GreenfieldLPLoanFacilityMember` | Greenfield L. P. Loan Facility (XBRL member) | Constellation Energy Generation LLC | facility | 0 | no legacy price in the archive and no linked fund holding |
| `XB-CEG-PinOakCreekEnergyCenterCreditAgreementMe` | Pin Oak Creek Energy Center Credit Agreement (XBRL member) | Constellation Energy Generation LLC | other | 0 | no legacy price in the archive and no linked fund holding |
| `XB-DLR-FourPointTwoFiveZeroPercentNotesDueTwent` | Four Point Two Five Zero Percent Notes Due Twenty Thirty Seven (XBRL member) | Digital Realty Trust, L.P. | bond | 0 | no legacy price in the archive and no linked fund holding |
| `XB-DLR-OnePointSevenPercentageNotesDue2027Membe` | One Point Seven Percentage Notes Due2027 (XBRL member) | Digital Realty Trust, L.P. | bond | 0 | no legacy price in the archive and no linked fund holding |
| `XB-DLR-OnePointTwoFivePercentNotesDueTwentyTwen` | One Point Two Five Percent Notes Due Twenty Twenty Eight (XBRL member) | Digital Realty Trust, L.P. | bond | 0 | no legacy price in the archive and no linked fund holding |
| `XB-DLR-ThreePointEightSevenFivePercentageNotesD` | Three Point Eight Seven Five Percentage Notes Due2033 (XBRL member) | Digital Realty Trust, L.P. | bond | 0 | no legacy price in the archive and no linked fund holding |
| `XB-DLR-ThreePointSevenFiveZeroPercentNotesDueTw` | Three Point Seven Five Zero Percent Notes Due Twenty Thirty (XBRL member) | Digital Realty Trust, L.P. | bond | 0 | no legacy price in the archive and no linked fund holding |
| `XB-DLR-ThreePointSevenFiveZeroPercentNotesDueTw-549` | Three Point Seven Five Zero Percent Notes Due Twenty Thirty Three (XBRL member) | Digital Realty Trust, L.P. | bond | 0 | no legacy price in the archive and no linked fund holding |
| `XB-DLR-ThreePointThreeZeroZeroPercentNotesDueTw` | Three Point Three Zero Zero Percent Notes Due Twenty Twenty Nine (XBRL member) | Digital Realty Trust, L.P. | bond | 0 | no legacy price in the archive and no linked fund holding |
| `XB-DLR-ZeroPointFiveFiveZeroPercentDueTwoThousa` | Zero Point Five Five Zero Percent Due Two Thousand Twenty Nine (XBRL member) | Digital Realty Trust, L.P. | other | 0 | no legacy price in the archive and no linked fund holding |
| `XB-DLR-ZeroPointTwoZeroZeroPercentDueTwoThousan` | Zero Point Two Zero Zero Percent Due Two Thousand Twenty Six (XBRL member) | Digital Realty Trust, L.P. | other | 0 | no legacy price in the archive and no linked fund holding |
| `XB-NRG-A2450SeniorSecuredNotesDue2027Member` | A2450 Senior Secured Notes Due2027 (XBRL member) | NRG Energy, Inc. | bond | 0 | legacy px was a copy of the finra.org TRACE pull (blocked) |
| `XB-NRG-A3000CedarBayou5TEFLoanDue2045Member` | A3.000 Cedar Bayou5 T E F Loan Due2045 (XBRL member) | NRG Energy, Inc. | term_loan | 0 | no legacy price in the archive and no linked fund holding |
| `XB-NRG-ConvertibleSeniorNotesDue2048Member` | Convertible Senior Notes Due2048 (XBRL member) | NRG Energy, Inc. | convert | 0 | no legacy price in the archive and no linked fund holding |
| `XB-NRG-THWhartonTEFLoanDue2045Member` | T. H. Wharton T E F Loan Due2045 (XBRL member) | NRG Energy, Inc. | term_loan | 0 | no legacy price in the archive and no linked fund holding |
| `XB-NRG-TaxexemptBondsMember` | Taxexempt Bonds (XBRL member) | NRG Energy, Inc. | bond | 0 | no legacy price in the archive and no linked fund holding |
| `XB-TLN-PEDFA2009BBondsMember` | P E D F A2009 B Bonds (XBRL member) | Talen Energy Supply LLC | bond | 0 | no legacy price in the archive and no linked fund holding |
| `XB-TLN-PEDFA2009CBondsMember` | P E D F A2009 C Bonds (XBRL member) | Talen Energy Supply LLC | bond | 0 | no legacy price in the archive and no linked fund holding |
| `XB-VST-CoffeenTermLoanMember` | Coffeen Term Loan (XBRL member) | Vistra Operations Co LLC / Vistra Corp | term_loan | 0 | no legacy price in the archive and no linked fund holding |
| `XB-VST-ConstructionTermLoanFacilityCommitmentsM` | Construction Term Loan Facility Commitments (XBRL member) | Vistra Operations Co LLC / Vistra Corp | other | 0 | no legacy price in the archive and no linked fund holding |
| `XB-VST-RevenueBondsMember` | Revenue Bonds (XBRL member) | Vistra Operations Co LLC / Vistra Corp | muni | 0 | no legacy price in the archive and no linked fund holding |
| `XB-VST-TexasEnergyFundLoanMember` | Texas Energy Fund Loan (XBRL member) | Vistra Operations Co LLC / Vistra Corp | term_loan | 0 | no legacy price in the archive and no linked fund holding |
