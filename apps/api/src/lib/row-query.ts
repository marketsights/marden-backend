// Builds SQL WHERE / ORDER BY clauses for row read models (dc_row, plant_row) from query params.
// Filter expressions are constants in code; only values travel as bind parameters.
import { badRequest } from '../errors.js';
import { dayEnd } from './business-day.js';
import { factSource } from './licensing.js';
import { first, flag, isoDate, likePattern, list, num, type Query } from './params.js';

export class SqlParams {
  readonly values: unknown[] = [];
  add(value: unknown): string {
    this.values.push(value);
    return `$${this.values.length}`;
  }
}

/** Lazily bound context values a predicate may need. */
export interface FilterCtx {
  org(): string;   // placeholder for the caller's org id
  day(): string;   // placeholder for the as-of day (YYYY-MM-DD)
  /**
   * SQL source of facts as known on the as-of day: fact_current, or fact_asof(day) for ?asOf reads, without
   * the rows of sources the unlicensed-price switch hides (lib/licensing.ts factSource).
   */
  facts(): string;
  /** SQL condition: a row learned at `learnedAt` was known on the as-of day (TRUE without ?asOf). */
  known(learnedAt: string): string;
}

export type FilterKind = 'list' | 'number' | 'date' | 'flag' | 'text';
export interface FilterDef {
  param: string;
  kind: FilterKind;
  pred: (placeholder: string, ctx: FilterCtx) => string;
}

export interface RowViewDef {
  view: string;
  idTable: { table: string; column: string };
  filters: FilterDef[];
  sorts: Readonly<Record<string, string>>;
  defaultSort: string; // '-lastChange' = descending
  pageSize: number;
  /**
   * A second read model per key joined onto each served row (its latest doc as of the same date, or
   * null when not built): plant rows carry their plant_debt roll-up (spec 20 table columns).
   */
  companion?: { view: string; merge: (doc: never, companion: never | null) => unknown };
}

// ---- jsonb expression helpers (alias r = one latest read_model row) ----
const path = (p: string): string => `'{${p}}'`;
export const txt = (p: string): string => `(r.doc #>> ${path(p)})`;
export const low = (p: string): string => `lower(r.doc #>> ${path(p)})`;
export const numAt = (p: string): string =>
  `(CASE WHEN jsonb_typeof(r.doc #> ${path(p)}) = 'number' THEN (r.doc #>> ${path(p)})::float8 END)`;
export const arrAt = (p: string): string =>
  `(CASE WHEN jsonb_typeof(r.doc #> ${path(p)}) = 'array' THEN r.doc #> ${path(p)} ELSE '[]'::jsonb END)`;

/** Case-insensitive equality against any of the requested values. */
export const inList = (p: string): FilterDef['pred'] => (ph) => `${low(p)} = ANY(${ph}::text[])`;

/** SQL for the latest row per key of one view, as of a date. */
export const latestRowsSql = (viewPh: string, asOfPh: string): string => `
  SELECT DISTINCT ON (key) key, as_of, doc FROM read_model
  WHERE view = ${viewPh} AND (${asOfPh}::date IS NULL OR as_of <= ${asOfPh}::date)
  ORDER BY key DESC, as_of DESC`;

function bindFilter(def: FilterDef, q: Query, params: SqlParams, ctx: FilterCtx): string | null {
  switch (def.kind) {
    case 'list': {
      const values = list(q, def.param).map((v) => v.toLowerCase());
      return values.length ? def.pred(params.add(values), ctx) : null;
    }
    case 'number': {
      const n = num(q, def.param);
      return n === undefined ? null : def.pred(params.add(n), ctx);
    }
    case 'date': {
      const d = isoDate(q, def.param);
      return d === undefined ? null : def.pred(params.add(d), ctx);
    }
    case 'flag':
      return flag(q, def.param) ? def.pred('', ctx) : null;
    case 'text': {
      const s = first(q, def.param);
      if (s === undefined) return null;
      if (s.length > 100) throw badRequest(`${def.param} is too long`);
      return def.pred(params.add(likePattern(s)), ctx);
    }
  }
}

/** hidden: source names left out of fact reads (FastifyInstance.hiddenSources). */
export interface WhereScope { orgId: string; day: string; asOf: string | null; hidden: readonly string[] }

export function buildWhere(def: RowViewDef, q: Query, params: SqlParams, scope: WhereScope): string {
  let orgPh: string | null = null;
  let dayPh: string | null = null;
  let hiddenPh: string | null = null;
  const day = (): string => (dayPh ??= params.add(scope.day));
  const hidden = (): string | null => (scope.hidden.length ? (hiddenPh ??= params.add(scope.hidden)) : null);
  const ctx: FilterCtx = {
    org: () => (orgPh ??= params.add(scope.orgId)),
    day,
    facts: () => factSource(scope.asOf === null ? null : day(), hidden()),
    known: (learnedAt) => (scope.asOf === null ? 'TRUE' : `${learnedAt} < ${dayEnd(day())}`),
  };
  const conds = def.filters.map((f) => bindFilter(f, q, params, ctx)).filter((c): c is string => c !== null);
  return conds.length ? conds.map((c) => `(${c})`).join(' AND ') : 'TRUE';
}

export function buildOrder(def: RowViewDef, q: Query): string {
  const raw = first(q, 'sort') ?? def.defaultSort;
  const desc = raw.startsWith('-');
  const name = desc ? raw.slice(1) : raw;
  const expr = def.sorts[name];
  if (!expr) throw badRequest(`sort must be one of ${Object.keys(def.sorts).map((s) => `${s}, -${s}`).join(', ')}`);
  return `${expr} ${desc ? 'DESC' : 'ASC'} NULLS LAST, r.key ASC`;
}
