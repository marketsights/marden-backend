// Excel rows for Stress (spec 09 Layout 5; spec 00 "Exports": exactly the rows and columns shown, with a
// sources sheet). The debt-hit table: Instrument · Size · Why it is hit · Rule · Price now · Price after ·
// Change · Who holds it, plus Rent coverage (before, after) and Holder NAV hit only when the model sends
// them for some row (the screen shows those columns only then). Rows worst first, the tenant's own notes
// last, as on screen. Contagion: Instrument · Price now · Price after · Change · Who holds it.
// The prices after the shock and the change are the stress model's: written as Sourced values (tag M,
// method version) so the sources sheet names the model; "n/d" where the model could not price.

type Row = Record<string, unknown>;

const isObj = (v: unknown): v is Row => typeof v === 'object' && v !== null && !Array.isArray(v);
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const ND = 'n/d';
const NA = '—';

/** after − now in points (3 decimals, as the model's prices), null when either side is unpriced. */
function change(row: Row): number | null {
  const now = num(row.priceNow);
  const after = num(row.priceAfter);
  return now === null || after === null ? null : Math.round((after - now) * 1000) / 1000;
}

const isTenantNotes = (row: Row) => row.tenantNotes === true || (typeof row.instrumentId === 'string' && row.instrumentId.startsWith('notes:'));
const byChange = (a: Row, b: Row) => (change(a) ?? Number.POSITIVE_INFINITY) - (change(b) ?? Number.POSITIVE_INFINITY);

/** Worst first (largest fall), rows without a change after them, then the tenant's own notes. */
export function orderStressRows(rows: readonly Row[]): Row[] {
  return [...rows.filter((r) => !isTenantNotes(r)).sort(byChange), ...rows.filter(isTenantNotes).sort(byChange)];
}

interface RunMeta { asOf: string | null; methodVersion: string | null }

function runMeta(result: Row): RunMeta {
  const basis = isObj(result.basis) ? result.basis : {};
  const version = typeof result.methodVersion === 'string' ? result.methodVersion : basis.methodVersion;
  return { asOf: typeof result.asOf === 'string' ? result.asOf : null, methodVersion: typeof version === 'string' ? version : null };
}

/** A stress-model number as a Sourced value (M) when the run says when it ran; else the bare number. */
function model(value: number | null, meta: RunMeta): unknown {
  if (meta.asOf === null) return value ?? ND;
  return { value, tag: 'M', asOf: meta.asOf, sourceName: 'Marden stress model', ...(meta.methodVersion ? { methodVersion: meta.methodVersion } : {}) };
}

const text = (v: unknown, fallback: string) => (typeof v === 'string' && v.trim() !== '' ? v : fallback);

function debtHitRow(row: Row, meta: RunMeta, extras: { rent: boolean; nav: boolean }): Row {
  const out: Row = {
    instrumentId: row.instrumentId,
    instrument: row.name,
    sizeB: row.sizeB ?? ND,
    why: text(row.why, ND),
    rule: text(row.ruleCode, NA),
  };
  if (extras.rent) {
    const cover = isObj(row.rentCover) ? row.rentCover : null;
    out.rentCoverBefore = cover?.before ?? NA;
    out.rentCoverAfter = cover?.after ?? NA;
  }
  out.priceNow = num(row.priceNow) ?? ND;
  out.priceAfter = model(num(row.priceAfter), meta);
  out.change = model(change(row), meta);
  if (extras.nav) out.navHitMm = num(row.navHitMm) ?? NA;
  out.holders = text(row.holders, ND);
  return out;
}

function contagionRow(row: Row, meta: RunMeta): Row {
  return {
    instrumentId: row.instrumentId,
    instrument: row.name,
    priceNow: num(row.priceNow) ?? ND,
    priceAfter: model(num(row.priceAfter), meta),
    change: model(change(row), meta),
    holders: text(row.holders, ND),
  };
}

/** The table rows of a finished run as the screen shows them; null for anything else (queued, an error). */
export function stressExportRows(result: unknown): { asOf: string | null; rows: Row[] } | null {
  if (!isObj(result)) return null;
  const meta = runMeta(result);
  if (Array.isArray(result.rows)) {
    const rows = (result.rows as unknown[]).filter(isObj);
    const extras = { rent: rows.some((r) => isObj(r.rentCover)), nav: rows.some((r) => num(r.navHitMm) !== null) };
    return { asOf: meta.asOf, rows: orderStressRows(rows).map((r) => debtHitRow(r, meta, extras)) };
  }
  if (Array.isArray(result.instruments)) {
    const rows = (result.instruments as unknown[]).filter(isObj).sort(byChange);
    return { asOf: meta.asOf, rows: rows.map((r) => contagionRow(r, meta)) };
  }
  return null;
}
