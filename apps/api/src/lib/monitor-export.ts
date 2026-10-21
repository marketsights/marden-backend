// Excel rows for the Risk list (spec 10) and Track record (spec 12): the columns each table shows, in its
// order (spec 00 "Exports": exactly the rows and columns shown, with a sources sheet). Sourced values stay
// Sourced and `why` stays a list of Evidence records, so the sources sheet lists each one; "vs last wk"
// is written as the screen shows it instead of a JSON cell. "—" where the screen shows "—" (no gap for a
// "traded" / "private" / "no fund holds it" row), "n/d" where it shows n/d.
import type { RiskRow } from '../contracts/types.js';
import type { TrackRecord } from '../contracts/app.js';

const NA = '—';
const ND = 'n/d';

type Row = Record<string, unknown>;
type TrackCase = TrackRecord['cases'][number];
type ModelRow = TrackRecord['models'][number];

/** vs last week as on screen: ▲ n (up the list) · ▼ n · — · New. */
export function moveText(move: RiskRow['move']): string {
  if (move.kind === 'new') return 'New';
  if (move.kind === 'same' || !move.by) return NA;
  return `${move.kind === 'up' ? '▲' : '▼'} ${move.by}`;
}

/** Risk list: # · Instrument (name, size) and why it is here · Price · 1w · Fund mark · gap · Odds · vs last wk. */
export function riskListExportRow(row: RiskRow): Row {
  const words = typeof row.fundMark === 'string';
  return {
    rank: row.rank,
    instrumentId: row.instrumentId,
    name: row.name,
    size: row.size || ND,
    why: row.why,
    price: row.price ?? NA,
    change1w: row.change1w ?? NA,
    fundMark: row.fundMark ?? ND,
    gap: words ? NA : (row.gap ?? ND),
    odds: row.odds,
    vsLastWeek: moveText(row.move),
  };
}

/** Lead-time chart, one row per past repricing: case · first flag (and its basis) · move · lead days. */
export function trackCaseExportRow(c: TrackCase): Row {
  return {
    name: c.name,
    instrumentId: c.instrumentId ?? NA,
    flagged: c.backtestRunning ? NA : (c.flagged ?? 'not flagged before the move'),
    flagBasis: c.flagBasis ?? NA,
    moved: c.backtestRunning ? NA : (c.moved ?? ND),
    leadDays: c.backtestRunning ? NA : (c.leadDays ?? NA),
    status: c.backtestRunning ? 'backtest running' : 'published',
  };
}

/** Models table: Output (version) · Built from · Runs (cadence, last run). */
export function modelExportRow(m: ModelRow): Row {
  return {
    output: m.output,
    version: m.version,
    builtFrom: m.builtFrom,
    cadence: m.cadence,
    lastRun: m.lastRun ?? 'not run yet',
    model: m.model,
    method: m.methodUrl ?? ND,
  };
}
