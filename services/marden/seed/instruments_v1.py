"""The V1 instrument universe (specs/00a "V1 coverage"): 50 AI-linked instruments of $1B or more, US, 2024-2026.

Picked from the case-linked rows (aiwcredit.rowinfo.c), the spec-named examples in DATA_DICTIONARY 1.1, the
legacy watch list and price targets, with commitments checked against the facility registry (aiwc/q3x/fac.json).
Reasoning per row is written to docs/seed-data/v1-instruments.md. Exceptions to the $1B rule are spec-named.
"""

from __future__ import annotations

from typing import NamedTuple


class V1Pick(NamedTuple):
    id: str
    segment: str
    reason: str


CONSTRUCTION = "Construction and project loans"
GPU_LEASE = "Chip and GPU lease vehicles"
GPU_DDTL = "GPU delayed-draw facilities"
NEOCLOUD = "Neocloud and developer bonds"
TENANT = "Tenant and end-user credits (reference)"

V1_PICKS: tuple[V1Pick, ...] = (
    # ---- construction and project loans (spec target 15; 17 picked: every US case-linked project debt >= $1B)
    V1Pick("EX-JUPITER-190", CONSTRUCTION, "Jupiter (spec-named); $18.0B Red Chiles construction loans, 2025"),
    V1Pick("EX-VANTAGE_ORCL-366", CONSTRUCTION, "Frontier + Lighthouse (spec-named); $38.0B construction debt, 2025"),
    V1Pick("EX-ABILENE-000", CONSTRUCTION, "Abilene (spec-named); Phase 1 construction loan $2.3B"),
    V1Pick("EX-ABILENE-001", CONSTRUCTION, "Abilene (spec-named); Phase 2 construction loans $7.1B, 2025"),
    V1Pick("EX-SALINE-308", CONSTRUCTION, "The Barn (spec-named); Saline project bonds $14.0B"),
    V1Pick("EX-APLD-010", CONSTRUCTION, "Polaris Forge 1 (case APLD_PF1); 9.25% secured notes $2.35B"),
    V1Pick("EX-APLD-011", CONSTRUCTION, "Polaris Forge 2 (case APLD_PF2); 6.75% secured notes $2.15B"),
    V1Pick("EX-APLD-012", CONSTRUCTION, "Polaris Forge 1 B4 (case APLD_PF1B4); 7.00% secured notes $1.59B"),
    V1Pick("EX-CIFR-027", CONSTRUCTION, "Barber Lake (case CIFR_BARBER); 7.125% secured notes $1.73B"),
    V1Pick("EX-CIFR-028", CONSTRUCTION, "Black Pearl (case CIFR_BLACKPEARL); 6.125% secured notes $2.0B, 2026"),
    V1Pick("EX-CORZ-065", CONSTRUCTION, "Core Scientific / CoreWeave sites (case CORZ_CW); 7.75% notes $3.3B"),
    V1Pick("EX-GLXY-162", CONSTRUCTION, "Helios 1 (case GLXY_H1); $1.4B committed term loan, 2025"),
    V1Pick("EX-GLXY-163", CONSTRUCTION, "Helios 2 (case GLXY_H2); 9.875% secured notes $3.51B, 2026"),
    V1Pick("EX-HUT-171", CONSTRUCTION, "River Bend (case HUT_RB); 6.192% secured notes $3.25B, 2026"),
    V1Pick("EX-HUT-172", CONSTRUCTION, "Beacon Point (case HUT_BP); 6.129% secured notes $4.25B, 2026"),
    V1Pick("EX-WULF-369", CONSTRUCTION, "Lake Mariner (case WULF_LM); 7.75% secured notes $3.2B"),
    V1Pick("EX-FLUIDSTACK-156", CONSTRUCTION, "Abernathy (case FLASH_ABERNATHY); Flash Compute 7.25% $1.3B, 2025"),
    # ---- chip and GPU lease vehicles (target 8; 7 picked, Anthropic Big Sky / North Star have no instrument)
    V1Pick("EX-BEIGNET-022", GPU_LEASE, "Meta Hyperion / Beignet (spec-named); 6.581% notes $27.3B, 2025"),
    V1Pick("EX-SOPAIPILLA-309", GPU_LEASE, "Meta El Paso / Sopaipilla (case); 7.534% notes $12.5B, 2026"),
    V1Pick("EX-XAI_VCI-388", GPU_LEASE, "Colossus 2 GPU SPV (case XAI_VCI); VCI AH1 first-lien loan, 2025"),
    V1Pick("EX-XAI_VCI-389", GPU_LEASE, "Colossus 2 GPU SPV (case XAI_VCI); VCI AH2 notes $3.5B committed, 2026"),
    V1Pick("EX-XAI_VCI-390", GPU_LEASE, "Colossus 2 GPU SPV (case XAI_VCI); VCI AH3 first-lien loan, 2026"),
    V1Pick("EX-XAI_VCI-383", GPU_LEASE, "Valor GPU sale-leasebacks behind the VCI loans; $13.3B lease liability"),
    V1Pick("EX-NSCALE-212", GPU_LEASE, "Nscale (spec-named) Texas/North Carolina SPVs; $2.54B Dell GPU leases, 2026"),
    # ---- GPU delayed-draw facilities (target 12)
    V1Pick("EX-CRWV-091", GPU_DDTL, "CoreWeave DDTL 1.0 (spec-named); $2.3B commitment"),
    V1Pick("EX-CRWV-092", GPU_DDTL, "CoreWeave DDTL 2.0 (spec-named); $7.6B commitment, 2024"),
    V1Pick("EX-CRWV-093", GPU_DDTL, "CoreWeave DDTL 2.1 (spec-named); $3.0B, 2025"),
    V1Pick("EX-CRWV-094", GPU_DDTL, "CoreWeave DDTL 3.0 (spec-named, case CRWV_DDTL3); $2.6B commitment, 2025"),
    V1Pick("EX-CRWV-095", GPU_DDTL, "CoreWeave DDTL 4.0 (spec-named, case CRWV_DDTL4); $8.5B commitment, 2026"),
    V1Pick("EX-CRWV-096", GPU_DDTL, "CoreWeave DDTL 5.0 (spec-named); $3.1B commitment, 2026"),
    V1Pick("EX-CRWV-097", GPU_DDTL, "CoreWeave DDTL 5.5; $2.6B commitment, 2026"),
    V1Pick("EX-IREN-184", GPU_DDTL, "IREN Childress (spec-named, case IREN_CHILDRESS); $1.545B DDTL, 2026"),
    V1Pick("EX-IREN-185", GPU_DDTL, "IREN Childress GPU notes (case); $2.1B USPP programme, 2026"),
    V1Pick("EX-NBIS-204", GPU_DDTL, "Nebius (spec-named) US GPU SPV term loan; $775M, below $1B, kept as named"),
    V1Pick("EX-NSCALE-210", GPU_DDTL, "Nscale (spec-named) Ward County TX GPU facility; $1.85B, 2026"),
    V1Pick("EX-NSCALE-211", GPU_DDTL, "Nscale (spec-named) North Carolina GPU facility; $1.2B, 2026"),
    # ---- neocloud and developer bonds (target 8)
    V1Pick("EX-CRWV-102", NEOCLOUD, "CoreWeave 2031s (spec-named); 9.00% notes $1.75B, 2025"),
    V1Pick("EX-CRWV-103", NEOCLOUD, "CoreWeave 2031s (spec-named); 9.75% notes $2.75B, 2026"),
    V1Pick("EX-CRWV-104", NEOCLOUD, "CoreWeave 2032s (spec-named); 9.625% notes $1.25B, 2026"),
    V1Pick("EX-CRWV-105", NEOCLOUD, "CoreWeave 2032s (spec-named); 8.50% EUR notes $2.28B, 2026"),
    V1Pick("EX-CRWV-106", NEOCLOUD, "CoreWeave 2031 convert; 1.75% $2.59B, 2025"),
    V1Pick("EX-CRWV-107", NEOCLOUD, "CoreWeave 2032 convert; 1.75% $4.0B, 2026"),
    V1Pick("EX-VANTAGE-361", NEOCLOUD, "Vantage sub loan (spec-named); $662M HoldCo sub loan, kept as named"),
    V1Pick("MAN-EDGED-75SeniorSecuredGreenNote", NEOCLOUD, "Edged Compute developer notes; 7.5% $1.3B, 2026"),
    # ---- tenant and end-user credits, reference only (target 7; OpenAI, Anthropic, Microsoft have none)
    V1Pick("EX-ORCL-239", TENANT, "Oracle (spec-named) Sep-2025 issue; 5.20% 2035 $4.0B"),
    V1Pick("EX-ORCL-261", TENANT, "Oracle Sep-2025 issue; 5.95% 2055 $3.5B"),
    V1Pick("EX-ORCL-240", TENANT, "Oracle Feb-2026 issue; 5.70% 2036 $5.0B"),
    V1Pick("EX-ORCL-262", TENANT, "Oracle Feb-2026 issue; 6.70% 2056 $5.0B"),
    V1Pick("EX-ORCL-228", TENANT, "Oracle Feb-2026 issue; 4.95% 2031 $3.5B"),
    V1Pick("EX-CRWV-108", TENANT, "CoreWeave (spec-named tenant credit); 2.875% 2033 convert $4.2B, Sep 2026"),
)

V1_IDS: frozenset[str] = frozenset(p.id for p in V1_PICKS)

assert len(V1_IDS) == len(V1_PICKS) == 50, "the V1 universe is 50 distinct instruments"
