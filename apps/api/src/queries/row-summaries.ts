// KPI strips for Data centers and On-site power, computed over the same filtered rows as the table,
// so "counts on the KPI strip equal the filtered table totals" (spec 14, 15). Sums skip unknown (n/d)
// values; MW by status lists only statuses with at least one known value.
import type { DataCenterSummaryResponse } from '../contracts/app.js';
import type { PowerPlantSummary } from '../contracts/types.js';
import type { Db } from '../db.js';
import { asOfDay } from '../lib/params.js';
import { latestRowsSql, numAt, txt } from '../lib/row-query.js';
import { DC_ROWS, PLANT_ROWS } from '../lib/row-views.js';
import { filteredRows, type RowScope } from './rows.js';

const COVERAGE_FIELDS = ['developer', 'tenant', 'status', 'facilityMw', 'itMw', 'powerSource', 'utility', 'iso', 'onlineYear'];
/**
 * Financed debt over the filtered campuses: every financed site on each campus (`financedSites`: Galaxy Helios I
 * and II, Polaris Forge 1 and its building 4), not only the primary `financed` link, counted once per site: a
 * site whose debt secures several campuses (Core Scientific's notes on Denton, Dalton, Marble and Muskogee; one
 * CMBS over two QTS campuses) adds its debt once. n/d debts are skipped, and a row without the list (built before
 * it existed) counts its primary link.
 */
const FINANCED_SITES = `(CASE WHEN jsonb_typeof(r.doc -> 'financedSites') = 'array' THEN r.doc -> 'financedSites'
  WHEN jsonb_typeof(r.doc -> 'financed') = 'object' THEN jsonb_build_array(r.doc -> 'financed') ELSE '[]'::jsonb END)`;
const FINANCED_DEBT = `(SELECT sum(d) FROM (
  SELECT DISTINCT ON (s ->> 'siteSlug') (s #>> '{debtB,value}')::float8 AS d
    FROM filtered r, jsonb_array_elements(${FINANCED_SITES}) s
   WHERE jsonb_typeof(s #> '{debtB,value}') = 'number' ORDER BY s ->> 'siteSlug') per_site)`;

/** Campuses with at least one building record (the footer's "buildings 11 of 16"). */
const HAS_BUILDINGS = `coalesce(${numAt('buildings,total')}, 0) > 0`;

const sumBy = (groupExpr: string, valueExpr: string, labelKey: string): string => `
  (SELECT coalesce(jsonb_agg(jsonb_build_object('${labelKey}', g, 'mw', mw) ORDER BY g), '[]'::jsonb)
   FROM (SELECT ${groupExpr} AS g, sum(${valueExpr}) AS mw FROM filtered r
         WHERE ${groupExpr} IS NOT NULL GROUP BY 1 HAVING count(${valueExpr}) > 0) s)`;

export type Summary<T> = { available: number } & T;

export async function dataCenterSummary(db: Db, scope: RowScope): Promise<Summary<DataCenterSummaryResponse>> {
  const { params, cte } = filteredRows(DC_ROWS, scope);
  const dayPh = params.add(asOfDay(scope.asOf));
  const isNew = `left(${txt('firstSeen')}, 10) > (${dayPh}::date - 7)::text`;
  const fill = (field: string, known: string) =>
    `jsonb_build_object('field', '${field}', 'pct', round(100.0 * count(*) FILTER (WHERE ${known}) / nullif(count(*), 0), 1),
      'known', count(*) FILTER (WHERE ${known}))`;
  const coverage = [
    ...COVERAGE_FIELDS.map((f) => fill(f, `jsonb_typeof(r.doc #> '{${f},value}') <> 'null'`)),
    fill('buildings', HAS_BUILDINGS),
  ].join(', ');
  const res = await db.query<Summary<DataCenterSummaryResponse> & { financedCount: number; financedDebt: number | null }>(
    `${cte} SELECT
      (SELECT count(*) FROM latest)::int AS available,
      (SELECT count(*) FROM filtered)::int AS campuses,
      (SELECT coalesce(sum(${numAt('buildings,total')}), 0) FROM filtered r)::int AS buildings,
      (SELECT coalesce(sum(${numAt('buildings,built')}), 0) FROM filtered r)::int AS "buildingsBuilt",
      ${sumBy(txt('status,value'), numAt('facilityMw,value'), 'status')} AS "mwByStatus",
      jsonb_build_object(
        'count', (SELECT count(*) FROM filtered r WHERE ${isNew}),
        'newest', (SELECT jsonb_build_object('id', r.key, 'name', r.doc ->> 'name') FROM filtered r
                   WHERE ${isNew} ORDER BY ${txt('firstSeen')} DESC, r.key LIMIT 1)) AS "newThisWeek",
      (SELECT count(*) FROM filtered r WHERE jsonb_typeof(r.doc -> 'financed') = 'object')::int AS "financedCount",
      ${FINANCED_DEBT}::float8 AS "financedDebt",
      (SELECT jsonb_build_array(${coverage}) FROM filtered r) AS coverage`,
    params.values,
  );
  const row = res.rows[0];
  if (!row) throw new Error('summary query returned no row');
  const { financedCount, financedDebt, ...rest } = row;
  const newest = rest.newThisWeek.newest ?? undefined;
  return {
    ...rest,
    newThisWeek: newest ? { count: rest.newThisWeek.count, newest } : { count: rest.newThisWeek.count },
    financed: { count: financedCount, debtB: financedCount === 0 ? 0 : (financedDebt as number) },
    coverage: rest.campuses === 0 ? [] : rest.coverage,
  };
}

const unitMw = `(CASE WHEN jsonb_typeof(u -> 'totalMw') = 'number' THEN (u ->> 'totalMw')::float8 END)`;

export async function powerPlantSummary(db: Db, scope: RowScope): Promise<Summary<PowerPlantSummary>> {
  const { params, cte } = filteredRows(PLANT_ROWS, scope);
  const detailView = params.add('plant_detail');
  const asOfPh = params.add(scope.asOf);
  const dayPh = params.add(asOfDay(scope.asOf));
  const recent = (expr: string): string => `left(${expr}, 10) > (${dayPh}::date - 30)::text`;
  const res = await db.query<Summary<PowerPlantSummary>>(
    `${cte}, details AS (
        SELECT d.key, d.doc FROM (${latestRowsSql(detailView, asOfPh)}) d WHERE d.key IN (SELECT key FROM filtered)),
      units AS (
        SELECT u ->> 'oem' AS oem, u ->> 'status' AS status, ${unitMw} AS mw
        FROM details d, jsonb_array_elements(CASE WHEN jsonb_typeof(d.doc -> 'unitRows') = 'array'
                                                  THEN d.doc -> 'unitRows' ELSE '[]'::jsonb END) u)
    SELECT
      (SELECT count(*) FROM latest)::int AS available,
      (SELECT count(*) FROM filtered)::int AS plants,
      (SELECT coalesce(sum(${numAt('units,count')}), 0) FROM filtered r)::int AS units,
      ${sumBy(txt('status,value'), numAt('permittedMw,value'), 'status')} AS "mwByStatus",
      ${sumBy(txt('technology'), numAt('permittedMw,value'), 'tech')} AS "mwByTech",
      (SELECT coalesce(jsonb_agg(jsonb_build_object('oem', oem, 'byStatus', by_status) ORDER BY oem), '[]'::jsonb)
       FROM (SELECT oem, jsonb_agg(jsonb_build_object('status', status, 'mw', mw) ORDER BY status) AS by_status
             FROM (SELECT oem, status, sum(mw) AS mw FROM units WHERE oem IS NOT NULL AND status IS NOT NULL
                   GROUP BY 1, 2 HAVING count(mw) > 0) s GROUP BY oem) o) AS "mwByOem",
      (SELECT count(*) FROM filtered r LEFT JOIN details d ON d.key = r.key
       WHERE ${recent(txt('airPermit,decided'))} OR ${recent(`(d.doc #>> '{permit,filed}')`)}
          OR ${recent(`(d.doc #>> '{permit,decided}')`)})::int AS "permitsLast30d"`,
    params.values,
  );
  const row = res.rows[0];
  if (!row) throw new Error('summary query returned no row');
  return row;
}
