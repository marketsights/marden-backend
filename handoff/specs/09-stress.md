# 09 · Stress (`/stress`)

**Job:** pick a shock and see which debt loses coverage, what it is worth after, and who holds it.
**Reference:** screens/09-stress.html (Oracle cut to BB+) · **Endpoints:** `POST /api/stress/run` (body: scenario, params, severity), `POST /api/stress/saved`, `GET /api/stress/saved/:id`

## Scenarios in V1

| Scenario | Parameters | Propagation |
| --- | --- | --- |
| End user misses its funding plan (OpenAI) | % of contracts cut (default 30%) | End user → tenants on its chains → sites → instruments |
| Tenant downgrade (Oracle to BB+) | Target rating | Tenant link → collateral and coverage on each site → instrument prices; tenant's own notes |
| Power delay | Months (slider 0–24, default 6) | Every site still waiting on power: cushion falls by N; sites crossing zero hit rule M2 |

Severity: Base / Severe (Severe uses peak 2022 spreads).

## Layout

1. Header with Excel, Save scenario.
2. Scenario cards (selected = 2 px ink border) + severity control.
3. Results strip (5): site debt exposed; value lost on site debt ($, % of face); tenant's own notes loss; loans falling below 95; funds that file a hit.
4. **Flow chart:** shock → sites (band width = loan size, color = severity) + price-change bars per site. Title states which sites take most of the hit.
5. **Debt hit table**, worst first: instrument (size, tag), why it is hit (rule), price now → after, change, who holds it. Tenant's own notes as a shaded last row. A rent-coverage column (before → after) appears only for instruments whose rent is disclosed in a filing; none are today.

## Acceptance

- Results recompute within 2 seconds of changing a parameter.
- Saved scenarios appear in the "Saved stress result" widget and on Instrument detail.
