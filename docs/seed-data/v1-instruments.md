# V1 instruments (50)

Written by `python -m marden seed instruments --write-docs` on 2026-10-21. The list itself
lives in `services/marden/seed/instruments_v1.py`; `instrument.in_v1` is true for exactly these ids.

## How they were picked

specs/00a-product-spec.md sets the V1 universe: the 50 largest AI-linked instruments of $1B or more, US
only, 2024-2026 vintages, with stabilized colocation, CMBS and non-US deals left for V2. No locked list
exists in the archive (DATA_DICTIONARY 3.9), so the picks come from:

1. the case-linked rows (`aiwcredit.rowinfo.c`, a case's own debt), keeping US cases with debt of $1B+
   and dropping the pool/ABS cases (QTS, DATABANK, CLOUDHQ) and SOUTHGATE (Australia), as site_map.json does;
2. the spec-named examples resolved in DATA_DICTIONARY 1.1 (Jupiter, Frontier/Lighthouse, Abilene, The
   Barn, Hyperion, CoreWeave DDTL 1.0-5.0, IREN, Nebius, Nscale, CoreWeave 2031s/2032s, the Vantage sub
   loan, Oracle and CoreWeave as tenant credits);
3. sizes checked against the facility registry (`aiwc/q3x/fac.json`), because aidebt `amt_m` is the drawn
   amount on DDTLs (DDTL 4.0: 2,837 drawn vs 8,500 committed) and blank on the xAI VCI loans;
4. vintages from fac `date` (Oracle tenant credits are the Sep-2025 and Feb-2026 issues, not older notes).

Exceptions, kept because the spec names them: the Nebius US GPU term loan ($775M) and the Vantage HoldCo
sub loan ($662M). Gaps: Anthropic Big Sky / North Star and OpenAI, Anthropic and Microsoft reference
credits have no instrument in the archive (Big Sky exists only as fac `ANTH_COMPUTE_SPV_35B`), so the
GPU-lease segment has 7 rows (target 8) and tenant credits 6 (target 7); construction loans carry 17
(target 15) because every US case-linked project debt of $1B+ is in.

| Segment | Count | Spec target |
|---|---|---|
| Construction and project loans | 17 | 15 |
| Chip and GPU lease vehicles | 7 | 8 |
| GPU delayed-draw facilities | 12 | 12 |
| Neocloud and developer bonds | 8 | 8 |
| Tenant and end-user credits (reference) | 6 | 7 |

## The list

| # | Id | Instrument | Issuer | Kind | Site | Size $B | Basis | Maturity | Seeded price | Why |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `EX-JUPITER-190` | Construction loans | Red Chiles A–D, LLC (Project Jupiter SPVs; STACK / BorderPlex; Blue Owl Net Lease Trust) | construction_loan | jupiter | 18.00 |  | 2029-11-09 | dealer quote (press) | Jupiter (spec-named); $18.0B Red Chiles construction loans, 2025 |
| 2 | `EX-VANTAGE_ORCL-366` | Construction debt (TX + WI) | Vantage Frontier / Port Washington SPVs | construction_loan | frontier | 38.00 |  | 2030-04-15 | none | Frontier + Lighthouse (spec-named); $38.0B construction debt, 2025 |
| 3 | `EX-ABILENE-000` | Phase 1 construction loan | Abilene JV (Crusoe / Blue Owl / Primary Digital) | construction_loan | abilene | 2.30 |  | n/d | none | Abilene (spec-named); Phase 1 construction loan $2.3B |
| 4 | `EX-ABILENE-001` | Phase 2 construction loan | Abilene JV (Crusoe / Blue Owl / Primary Digital) | construction_loan | abilene | 7.10 |  | n/d | none | Abilene (spec-named); Phase 2 construction loans $7.1B, 2025 |
| 5 | `EX-SALINE-308` | Project bonds | Related Digital Saline project SPV | bond | the-barn | 14.00 |  | 2045-10 | fund marks | The Barn (spec-named); Saline project bonds $14.0B |
| 6 | `EX-APLD-010` | 9.25% Sr Secured Notes 2030 | APLD ComputeCo LLC | bond | polaris-forge-1 | 2.35 |  | 2030-12 | fund marks | Polaris Forge 1 (case APLD_PF1); 9.25% secured notes $2.35B |
| 7 | `EX-APLD-011` | 6.75% Sr Secured Notes 2031 | APLD ComputeCo 2 LLC | bond | polaris-forge-2 | 2.15 |  | 2031-03 | fund marks | Polaris Forge 2 (case APLD_PF2); 6.75% secured notes $2.15B |
| 8 | `EX-APLD-012` | 7.00% Sr Secured Notes 2031 | APLD ComputeCo 3 LLC | bond | polaris-forge-1-building-4 | 1.59 |  | 2031-06 | fund marks | Polaris Forge 1 B4 (case APLD_PF1B4); 7.00% secured notes $1.59B |
| 9 | `EX-CIFR-027` | 7.125% Sr Secured Notes 2030 | Cipher Compute LLC | bond | barber-lake | 1.73 |  | 2030-11 | fund marks | Barber Lake (case CIFR_BARBER); 7.125% secured notes $1.73B |
| 10 | `EX-CIFR-028` | 6.125% Sr Secured Notes 2031 | Black Pearl Compute LLC | bond | black-pearl | 2.00 |  | 2031-02 | fund marks | Black Pearl (case CIFR_BLACKPEARL); 6.125% secured notes $2.0B, 2026 |
| 11 | `EX-CORZ-065` | 7.75% Sr Sec Notes 2031 | Core Scientific Finance I LLC | bond | core-scientific-coreweave | 3.30 |  | 2031-05 | fund marks | Core Scientific / CoreWeave sites (case CORZ_CW); 7.75% notes $3.3B |
| 12 | `EX-GLXY-162` | Senior secured term loan (DB lead) | Galaxy Helios I LLC | term_loan | helios-1 | 1.27 |  | 2028-08 | fund marks | Helios 1 (case GLXY_H1); $1.4B committed term loan, 2025 |
| 13 | `EX-GLXY-163` | 9.875% Sr Sec Notes 2031 | Galaxy Helios Data Centers II LLC | bond | helios-2 | 3.51 |  | 2031-08 | fund marks | Helios 2 (case GLXY_H2); 9.875% secured notes $3.51B, 2026 |
| 14 | `EX-HUT-171` | 6.192% Sr Sec Notes due 2042 (River Bend, 44813DAA4) | Hut 8 DC LLC | bond | river-bend | 3.25 |  | 2042-11 | fund marks | River Bend (case HUT_RB); 6.192% secured notes $3.25B, 2026 |
| 15 | `EX-HUT-172` | 6.129% Sr Sec Notes due 2042 (Beacon Point, 07367AAA7) | Beacon Point DC LLC | bond | beacon-point | 4.25 |  | 2042-11 | fund marks | Beacon Point (case HUT_BP); 6.129% secured notes $4.25B, 2026 |
| 16 | `EX-WULF-369` | 7.75% Sr Secured Notes 2030 | WULF Compute LLC | bond | lake-mariner | 3.20 |  | 2030-10 | fund marks | Lake Mariner (case WULF_LM); 7.75% secured notes $3.2B |
| 17 | `EX-FLUIDSTACK-156` | Flash Compute 7.25% Sr Sec Notes 2030 | Flash Compute LLC (Abernathy JV) | bond | abernathy | 1.30 |  | 2030 | fund marks | Abernathy (case FLASH_ABERNATHY); Flash Compute 7.25% $1.3B, 2025 |
| 18 | `EX-BEIGNET-022` | 6.581% Sr Secured Notes due 2049 | Beignet Investor LLC | bond | hyperion | 27.29 |  | 2049-05 | dealer quote (press) | Meta Hyperion / Beignet (spec-named); 6.581% notes $27.3B, 2025 |
| 19 | `EX-SOPAIPILLA-309` | 7.534% Sr Secured Notes due 2048 | Sopaipilla Investor LLC | bond | meta-el-paso | 12.55 |  | 2048-11 | fund marks | Meta El Paso / Sopaipilla (case); 7.534% notes $12.5B, 2026 |
| 20 | `EX-XAI_VCI-388` | VCI AH1 1L Term Loan (LX276526) | VCI Asset Holdings 1 LLC | term_loan | colossus-2 | n/d |  | 2030-11 | fund marks | Colossus 2 GPU SPV (case XAI_VCI); VCI AH1 first-lien loan, 2025 |
| 21 | `EX-XAI_VCI-389` | VCI AH2 1L Term Loan (LX284984) | VCI Asset Holdings 2 LLC | term_loan | colossus-2 | n/d |  | 2031-02 | fund marks | Colossus 2 GPU SPV (case XAI_VCI); VCI AH2 notes $3.5B committed, 2026 |
| 22 | `EX-XAI_VCI-390` | VCI AH3 1L Term Loan (LX289647) | VCI Asset Holdings 3 LLC | term_loan | colossus-2 | n/d |  | 2031-04 | fund marks | Colossus 2 GPU SPV (case XAI_VCI); VCI AH3 first-lien loan, 2026 |
| 23 | `EX-XAI_VCI-383` | Valor failed sale-leasebacks (3 GPU leases) | CTC Property LLC | lease |  | 13.33 |  | n/d | none | Valor GPU sale-leasebacks behind the VCI loans; $13.3B lease liability |
| 24 | `EX-NSCALE-212` | Dell Financial Services leases | Ward County and NC SPVs | lease |  | 2.54 |  | n/d | none | Nscale (spec-named) Texas/North Carolina SPVs; $2.54B Dell GPU leases, 2026 |
| 25 | `EX-CRWV-091` | DDTL 1.0 | CoreWeave Compute Acquisition Co. II, LLC | ddtl |  | 1.30 | drawn | 2028-03 | fund marks | CoreWeave DDTL 1.0 (spec-named); $2.3B commitment |
| 26 | `EX-CRWV-092` | DDTL 2.0 | CoreWeave Compute Acquisition Co. IV, LLC | ddtl |  | 3.19 | drawn | 2030-08 | fund marks | CoreWeave DDTL 2.0 (spec-named); $7.6B commitment, 2024 |
| 27 | `EX-CRWV-093` | DDTL 2.1 | CoreWeave Compute Acquisition Co. IV, LLC | ddtl |  | 3.00 | drawn | 2031-03 | none | CoreWeave DDTL 2.1 (spec-named); $3.0B, 2025 |
| 28 | `EX-CRWV-094` | DDTL 3.0 | CoreWeave Compute Acquisition Co. VII, LLC (co-borrower CCAC V) | ddtl | coreweave-ddtl-3 | 2.21 | drawn | 2030-08 | none | CoreWeave DDTL 3.0 (spec-named, case CRWV_DDTL3); $2.6B commitment, 2025 |
| 29 | `EX-CRWV-095` | DDTL 4.0 | CoreWeave Compute Acquisition Co. VIII, LLC | ddtl | coreweave-ddtl-4 | 2.84 | drawn | 2032-03 | fund marks | CoreWeave DDTL 4.0 (spec-named, case CRWV_DDTL4); $8.5B commitment, 2026 |
| 30 | `EX-CRWV-096` | DDTL 5.0 | CoreWeave Financing DDTL V, LLC | ddtl |  | 1.10 | drawn | 2031-11 | fund marks | CoreWeave DDTL 5.0 (spec-named); $3.1B commitment, 2026 |
| 31 | `EX-CRWV-097` | DDTL 5.5 | CoreWeave Financing DDTL V-V, LLC | ddtl |  | 1.20 | drawn | 2031-09 | none | CoreWeave DDTL 5.5; $2.6B commitment, 2026 |
| 32 | `EX-IREN-184` | DDTL (Credit Agreement) | IE US Hardware 3 LLC | ddtl | childress | 0.41 | drawn | 2031-12 | fund marks | IREN Childress (spec-named, case IREN_CHILDRESS); $1.545B DDTL, 2026 |
| 33 | `EX-IREN-185` | 5.96% USPP Sr. Secured Notes | IE US Hardware 3 LLC | bond | childress | 0.53 |  | 2031-12 | none | IREN Childress GPU notes (case); $2.1B USPP programme, 2026 |
| 34 | `EX-NBIS-204` | Senior secured term loan | Nebius Compute II LLC / Nebius Compute II Oy | term_loan |  | 0.78 |  | 2030-10 | none | Nebius (spec-named) US GPU SPV term loan; $775M, below $1B, kept as named |
| 35 | `EX-NSCALE-210` | Ward County GPU DDTL | Nscale Ward County Borrower SPV, LLC | ddtl |  | n/d |  | 2033-02 | none | Nscale (spec-named) Ward County TX GPU facility; $1.85B, 2026 |
| 36 | `EX-NSCALE-211` | North Carolina GPU DDTL | Nscale NC Borrower SPV, LLC | ddtl |  | n/d |  | 2031-12 | none | Nscale (spec-named) North Carolina GPU facility; $1.2B, 2026 |
| 37 | `EX-CRWV-102` | 9.00% Sr Notes 2031 | CoreWeave, Inc. | bond |  | 1.75 |  | 2031-02 | none | CoreWeave 2031s (spec-named); 9.00% notes $1.75B, 2025 |
| 38 | `EX-CRWV-103` | 9.75% Sr Notes 2031 | CoreWeave, Inc. | bond |  | 2.75 |  | 2031-10 | dealer quote (press) | CoreWeave 2031s (spec-named); 9.75% notes $2.75B, 2026 |
| 39 | `EX-CRWV-104` | 9.625% Sr Notes 2032 | CoreWeave, Inc. | bond |  | 1.25 |  | 2032-07 | none | CoreWeave 2032s (spec-named); 9.625% notes $1.25B, 2026 |
| 40 | `EX-CRWV-105` | 8.50% EUR Sr Notes 2032 | CoreWeave, Inc. | bond |  | 2.28 |  | 2032-07 | none | CoreWeave 2032s (spec-named); 8.50% EUR notes $2.28B, 2026 |
| 41 | `EX-CRWV-106` | 1.75% Convertible Notes 2031 | CoreWeave, Inc. | convert |  | 2.59 |  | 2031-12 | none | CoreWeave 2031 convert; 1.75% $2.59B, 2025 |
| 42 | `EX-CRWV-107` | 1.75% Convertible Notes 2032 | CoreWeave, Inc. | convert |  | 4.00 |  | 2032-10 | fund marks | CoreWeave 2032 convert; 1.75% $4.0B, 2026 |
| 43 | `EX-VANTAGE-361` | Retained Vantage HoldCo sr sub loan | Retained Vantage DC Intermediate Holdco, LP + Assets, LP | term_loan |  | 0.66 |  | 2031-12 | fund marks | Vantage sub loan (spec-named); $662M HoldCo sub loan, kept as named |
| 44 | `MAN-EDGED-75SeniorSecuredGreenNote` | 7.5% Senior Secured Green Notes due 2031 | Edged Compute LLC (sub of EDC Venture, LLC) | bond |  | 1.30 |  | 2031-04 | fund marks | Edged Compute developer notes; 7.5% $1.3B, 2026 |
| 45 | `EX-ORCL-239` | 5.20% due Sep-2035 Sr Notes | Oracle Corporation | bond |  | 4.00 |  | 2035-09 | none | Oracle (spec-named) Sep-2025 issue; 5.20% 2035 $4.0B |
| 46 | `EX-ORCL-261` | 5.95% due Sep-2055 Sr Notes | Oracle Corporation | bond |  | 3.50 |  | 2055-09 | none | Oracle Sep-2025 issue; 5.95% 2055 $3.5B |
| 47 | `EX-ORCL-240` | 5.70% due Feb-2036 Sr Notes | Oracle Corporation | bond |  | 5.00 |  | 2036-02 | none | Oracle Feb-2026 issue; 5.70% 2036 $5.0B |
| 48 | `EX-ORCL-262` | 6.70% due Feb-2056 Sr Notes | Oracle Corporation | bond |  | 5.00 |  | 2056-02 | none | Oracle Feb-2026 issue; 6.70% 2056 $5.0B |
| 49 | `EX-ORCL-228` | 4.95% due Feb-2031 Sr Notes | Oracle Corporation | bond |  | 3.50 |  | 2031-02 | none | Oracle Feb-2026 issue; 4.95% 2031 $3.5B |
| 50 | `EX-CRWV-108` | 2.875% Convertible Notes 2033 | CoreWeave, Inc. | convert |  | 4.20 |  | 2033-04 | none | CoreWeave (spec-named tenant credit); 2.875% 2033 convert $4.2B, Sep 2026 |
