// Sidebar search (spec 00, 16): sites, instruments (name, CUSIP, ISIN), campuses, buildings (label,
// parcel ID), plants, entities and permit numbers, ranked by trigram word similarity. Permit numbers
// are facts, read through fact_current (or fact_asof for ?asOf), never from `fact` directly.
import type { SearchResult } from '../contracts/app.js';
import type { Db } from '../db.js';

// $1 = query text, $2 = ILIKE pattern, $3 = as-of date or null, $4 = rows per kind.
const sim = (expr: string): string => `word_similarity($1, coalesce(${expr}, ''))`;
const hit = (expr: string): string => `(${expr} ILIKE $2 OR $1 <% coalesce(${expr}, ''))`;
const anyHit = (arr: string): string => `EXISTS (SELECT 1 FROM unnest(${arr}) x WHERE x ILIKE $2 OR $1 <% x)`;
const bestOf = (arr: string): string => `(SELECT coalesce(max(word_similarity($1, x)), 0) FROM unnest(${arr}) x)`;
const known = (alias: string): string => `($3::date IS NULL OR ${alias}.created_at < ($3::date + 1))`;
const score = (...parts: string[]): string => `greatest(${parts.join(', ')})::float8`;
const exact = (expr: string): string => `CASE WHEN ${expr} ILIKE $2 THEN 0.6 ELSE 0 END`;

const BRANCHES = [
  `SELECT 'site' AS kind, s.slug AS id, s.name AS label, nullif(concat_ws(', ', s.county, s.state), '') AS sub,
          '/sites/' || s.slug AS href, ${score(sim('s.name'), exact('s.name'), bestOf('s.tracking_names'))} AS score
   FROM site s WHERE (${hit('s.name')} OR ${anyHit('s.tracking_names')}) AND ${known('s')}`,
  `SELECT 'instrument', i.id, i.name, concat_ws(' · ', i.issuer_name, i.cusip), '/instruments/' || i.id,
          ${score(sim('i.name'), exact('i.name'), `CASE WHEN i.cusip ILIKE $2 OR i.isin ILIKE $2 THEN 1 ELSE 0 END`)}
   FROM instrument i WHERE (${hit('i.name')} OR i.cusip ILIKE $2 OR i.isin ILIKE $2) AND ${known('i')}`,
  `SELECT 'campus', c.id, c.name, nullif(concat_ws(', ', c.county, c.state), ''), '/data-centers/' || c.id,
          ${score(sim('c.name'), exact('c.name'), bestOf('c.aliases'), `CASE WHEN ${anyHit('c.parcel_ids')} THEN 0.9 ELSE 0 END`)}
   FROM campus c WHERE (${hit('c.name')} OR ${anyHit('c.aliases')} OR ${anyHit('c.parcel_ids')}) AND ${known('c')}`,
  `SELECT 'building', b.id, b.label, c.name, '/data-centers/' || b.campus_id || '?tab=buildings',
          ${score(sim('b.label'), exact('b.label'), `CASE WHEN ${anyHit('b.parcel_ids')} THEN 0.9 ELSE 0 END`)}
   FROM building b JOIN campus c ON c.id = b.campus_id
   WHERE (${hit('b.label')} OR ${anyHit('b.parcel_ids')}) AND ${known('b')}`,
  `SELECT 'plant', p.id, p.name, nullif(concat_ws(', ', p.county, p.state), ''), '/power/' || p.id,
          ${score(sim('p.name'), exact('p.name'))}
   FROM power_plant p WHERE ${hit('p.name')} AND ${known('p')}`,
  `SELECT 'entity', e.id, e.name, e.kind, NULL,
          ${score(sim('e.name'), exact('e.name'), bestOf('e.aliases'), `CASE WHEN lower(e.ticker) = lower($1) OR e.cik = $1 THEN 1 ELSE 0 END`)}
   FROM entity e WHERE (${hit('e.name')} OR ${anyHit('e.aliases')} OR lower(e.ticker) = lower($1) OR e.cik = $1) AND ${known('e')}`,
  `SELECT DISTINCT ON (f.subject_type, f.subject_id, f.value #>> '{}') 'permit', f.subject_id, f.value #>> '{}',
          concat_ws(' · ', 'Air permit', coalesce(pp.name, c.name, s.name, f.subject_id)),
          CASE f.subject_type WHEN 'plant' THEN '/power/' WHEN 'campus' THEN '/data-centers/' WHEN 'site' THEN '/sites/' END
            || f.subject_id,
          CASE WHEN (f.value #>> '{}') ILIKE $2 THEN 1 ELSE ${sim(`f.value #>> '{}'`)} END::float8
   FROM (SELECT * FROM fact_current WHERE $3::date IS NULL AND field = 'air_permit_number'
         UNION ALL
         SELECT * FROM fact_asof($3::date) WHERE $3::date IS NOT NULL AND field = 'air_permit_number') f
   LEFT JOIN power_plant pp ON f.subject_type = 'plant' AND pp.id = f.subject_id
   LEFT JOIN campus c ON f.subject_type = 'campus' AND c.id = f.subject_id
   LEFT JOIN site s ON f.subject_type = 'site' AND s.slug = f.subject_id
   WHERE (f.value #>> '{}') ILIKE $2
   ORDER BY f.subject_type, f.subject_id, f.value #>> '{}', f.learned_at DESC`,
];

export async function search(
  db: Db,
  q: string,
  opts: { pattern: string; asOf: string | null; perKind: number; limit: number },
): Promise<SearchResult[]> {
  const union = BRANCHES.map((b) => `(SELECT * FROM (${b}) t ORDER BY 6 DESC, 3 LIMIT $4)`).join('\nUNION ALL\n');
  const res = await db.query<SearchResult>(
    `SELECT kind, id, label, sub, href, round(score::numeric, 3)::float8 AS score
     FROM (${union}) u ORDER BY score DESC, label LIMIT $5`,
    [q, opts.pattern, opts.asOf, opts.perKind, opts.limit],
  );
  return res.rows;
}
