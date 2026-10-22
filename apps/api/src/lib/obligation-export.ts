// Excel rows for the obligation map's table (spec 18; spec 00 "Exports": exactly the rows and
// columns shown, with a sources sheet). One row per edge in the screen's columns and order: Payer ·
// Payee · Type · Status · Total · Annual <as-of year> · Start · Term · Source. Ends are written by
// name, as on screen. Sourced values stay Sourced so the sources sheet lists each one, and every
// source passage goes in the Source column (the sources sheet keeps each document link). The
// annual cell is n/d when the edge has no annual schedule and "—" when the year is outside it,
// as the screen shows them.
import type { ObligationEdge, ObligationGraph } from '../contracts/types.js';

const NA = '—';

/** The year the screen reads annual amounts for: the document's as-of year. */
export const obligationYear = (graph: Pick<ObligationGraph, 'asOf'>): number => Number(graph.asOf.slice(0, 4));

export function obligationExportRow(edge: ObligationEdge, names: ReadonlyMap<string, string>, year: number): Record<string, unknown> {
  const annual = edge.annual.length === 0 ? 'n/d' : (edge.annual.find((a) => a.year === year)?.amountB ?? NA);
  return {
    id: edge.id,
    payer: names.get(edge.from) ?? edge.from,
    payee: names.get(edge.to) ?? edge.to,
    type: edge.type,
    status: edge.status,
    totalB: edge.totalB,
    [`annual${year}B`]: annual,
    start: edge.start,
    termYears: edge.termYears,
    sources: edge.evidence,
  };
}

/** The filtered graph's edges as the table shows them, in the document's order (largest total first). */
export function obligationExportRows(graph: ObligationGraph): Record<string, unknown>[] {
  const names = new Map(graph.nodes.map((n) => [n.id, n.name]));
  const year = obligationYear(graph);
  return graph.edges.map((edge) => obligationExportRow(edge, names, year));
}
