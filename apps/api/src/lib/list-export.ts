// Excel rows for the Completion monitor (spec 03) and Sites (spec 05): the columns each table shows,
// in its order (spec 00 "Exports": exactly the rows and columns shown, with a sources sheet). The
// completion row's projection is spread into its own columns (P10, P50, P90, outside date, cushion,
// Δ wk) instead of one JSON cell; Sourced values stay Sourced so the sources sheet lists them. "—" is
// written where the screen shows "—" (no lease date: no cushion, no weekly change, no driver).
import type { CompletionRow, SiteSummary, Sourced } from '../contracts/types.js';
import type { WithBuildingCount } from '../contracts/site-tabs.js';

const NA = '—';

type Row = Record<string, unknown>;

/** Timeline (P10, P50, P90, outside date) · Cushion · Δ wk: "—" each without a projection. */
function projectionColumns(p: CompletionRow['projection']): Row {
  if (!p) return { p10: NA, p50: NA, p90: NA, outsideDate: NA, cushionMonths: NA, deltaWeek: NA };
  const cushion: Sourced<number> | null = p.cushionMonths ?? null;
  return {
    p10: p.p10 ?? NA,
    p50: p.p50 ?? NA,
    p90: p.p90 ?? NA,
    outsideDate: p.outsideDate ?? NA,
    cushionMonths: cushion ?? NA,
    deltaWeek: cushion && cushion.value !== null ? (p.deltaWeek ?? null) : NA,
  };
}

/** Completion table: Site · Bldgs · Chain · Timeline (P10, P50, P90, outside date) · Cushion · Δ wk · Driver · Last evidence · Debt $B · Lenders. */
export function completionExportRow(row: CompletionRow & WithBuildingCount): Row {
  const p = row.projection;
  return {
    slug: row.slug,
    name: row.name,
    state: row.state,
    buildings: row.buildings ?? 'n/d',
    chain: row.chain,
    ...projectionColumns(p),
    driver: row.driver ?? (p ? 'n/d' : NA),
    lastEvidence: row.lastEvidence ?? null,
    debtB: row.debtB,
    leadLenders: row.leadLenders,
  };
}

/** Sites table: Site (county, state) · End user → tenant · Developer · GW · Bldgs · Chain · Status · Cushion · Rent start · Debt $B · Lead lenders. */
export function sitesExportRow(row: SiteSummary & WithBuildingCount): Row {
  return {
    slug: row.slug,
    name: row.name,
    county: row.county,
    state: row.state,
    endUser: row.endUser,
    tenant: row.tenant ?? NA,
    developer: row.developer,
    gw: row.gw,
    buildings: row.buildings ?? 'n/d',
    chain: row.chain,
    status: row.status,
    cushionMonths: row.cushionMonths ?? NA,
    rentStartP50: row.rentStartP50 ?? NA,
    debtB: row.debtB,
    leadLenders: row.leadLenders,
  };
}
