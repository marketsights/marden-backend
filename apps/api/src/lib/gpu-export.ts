// GPU index and collateral (spec 19): the Excel exports behind the tables on /gpus and /gpus/:model.
// Each export is one table as shown, from the same read model the screen reads; values are copied
// as served (Sourced values kept, so the sources sheet lists each price page and model version).
import type { GpuIndexPage, GpuValueCurve } from '../contracts/app.js';
import type { GpuDeploymentPage, GpuModelDetail, GpuPricePoint } from '../contracts/types.js';
import { badRequest } from '../errors.js';
import { first, list, type Query } from './params.js';

type Row = Record<string, unknown>;

const lowerSet = (values: string[]): Set<string> => new Set(values.map((v) => v.toLowerCase()));
const matches = (wanted: Set<string>, value: string | null | undefined) => wanted.size === 0 || (value != null && wanted.has(value.toLowerCase()));

/** Index terms (spec 19): on-demand and reserved buckets. */
export const GPU_TERMS = ['on-demand', '1y', '3y'] as const;

export function gpuTerm(q: Query): string {
  const term = first(q, 'term') ?? 'on-demand';
  if (!(GPU_TERMS as readonly string[]).includes(term)) throw badRequest(`term must be one of ${GPU_TERMS.join(', ')}`);
  return term;
}

/** Today's provider prices (?model=&term=), as in the provider table. */
export function gpuProviderRows(doc: Pick<GpuIndexPage, 'providers'>, q: Query): Row[] {
  const model = lowerSet(list(q, 'model'));
  const term = lowerSet(list(q, 'term'));
  return doc.providers
    .filter((p) => matches(model, p.model) && matches(term, p.term))
    .map((p) => ({ provider: p.provider, model: p.model, term: p.term, region: p.region, usdHr: p.usdHr, capturedAt: p.capturedAt, pageUrl: p.pageUrl }));
}

/** Latest index point per model for one term. */
export function latestIndex(index: readonly GpuPricePoint[], term: string): Map<string, GpuPricePoint> {
  const out = new Map<string, GpuPricePoint>();
  for (const p of index) {
    if (p.term !== term) continue;
    const held = out.get(p.model);
    if (!held || p.date > held.date) out.set(p.model, p);
  }
  return out;
}

const curveColumns = (curve: GpuValueCurve | undefined): Row =>
  curve
    ? { launch: curve.launch, newPrice: curve.newPrice, ...Object.fromEntries(curve.points.map((p) => [`valueAge${p.age}y`, p.usd])) }
    : { launch: null, newPrice: null };

const indexColumns = (point: GpuPricePoint | undefined): Row =>
  point
    ? { indexDate: point.date, medianUsdHr: point.medianUsdHr, p25UsdHr: point.p25, p75UsdHr: point.p75, providers: point.nProviders }
    : { indexDate: null, medianUsdHr: null, p25UsdHr: null, p75UsdHr: null, providers: null };

function modelRow(model: string, term: string, curve: GpuValueCurve | undefined, point: GpuPricePoint | undefined): Row {
  const { launch, newPrice, ...ages } = curveColumns(curve);
  return { model, launch, newPrice, term, ...indexColumns(point), ...ages };
}

/** One row per model (?term=): today's index price and the value per unit by age, as in the models table. */
export function gpuModelRows(doc: GpuIndexPage, q: Query): Row[] {
  const term = gpuTerm(q);
  const latest = latestIndex(doc.index, term);
  const models = [...new Set([...doc.valueByAge.map((c) => c.model), ...latest.keys()])].sort();
  return models.map((model) => modelRow(model, term, doc.valueByAge.find((c) => c.model === model), latest.get(model)));
}

/** Delivered vs energized, one row per facility (spec 19). */
export function gpuDeploymentRows(doc: GpuDeploymentPage): Row[] {
  return doc.facilities.map((d) => ({
    id: d.id, name: d.name, scope: d.scope, model: d.model, status: d.status,
    unitsFinanced: d.unitsFinanced, unitsDelivered: d.unitsDelivered, unitsEnergized: d.unitsEnergized,
    idleUnits: d.idleUnits, idleShare: d.idleShare, monthsIdle: d.monthsIdle,
    idleInterestM: d.idleInterestM, valueLostWhileIdleM: d.valueLostWhileIdleM,
    ltvToday: d.ltvToday, ltvIfOnSchedule: d.ltvIfOnSchedule,
    kwPerUnit: d.assumptions.kwPerUnit, pue: d.assumptions.pue, couponPct: d.assumptions.couponPct,
    methodVersion: d.assumptions.methodVersion, missing: d.missing.join('; '), evidence: d.evidence,
  }));
}

export const GPU_MODEL_PARTS = ['providers', 'resale', 'facilities'] as const;

/** One table of the model page (?part=providers|resale|facilities). */
export function gpuModelPartRows(doc: GpuModelDetail, q: Query): Row[] {
  const part = first(q, 'part') ?? 'providers';
  if (part === 'providers') return gpuProviderRows(doc, q);
  if (part === 'resale') return doc.resale.map((r) => ({ model: doc.model, date: r.date, usd: r.usd, source: r.source, url: r.url }));
  if (part === 'facilities') return doc.facilities.map((f) => ({ model: doc.model, instrumentId: f.instrumentId, name: f.name, units: f.units }));
  throw badRequest(`part must be one of ${GPU_MODEL_PARTS.join(', ')}`);
}
