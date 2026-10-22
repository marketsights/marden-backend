// Lease map (spec 25) points: GET /api/leases?format=geojson places each lease at the campus it sits
// on, so the map and the ranked table read one document. A lease names its campus directly
// (campusId) or its financed sites (`sites`, else the single `siteSlug`), whose campuses come from each
// site's Buildings tab (site_tab:buildings, spec 16). Coordinates, facility MW and status are the campus row's own
// (dc_row, spec 14), served as published. Leases with neither link are left off the map; they
// still appear in the table. Every read is as of the same snapshot date.
import type * as GeoJSON from 'geojson';
import type { DataCenterRow, DcStatus, LeaseMapPage, SiteBuildings, SiteSummary, Sourced } from '../contracts/types.js';
import type { Db } from '../db.js';

/** One campus on the lease map and the leases that sit on it. */
export interface LeaseLocation {
  /** Campus id (dc_row key). */
  id: string;
  name: string;
  lat: number;
  lon: number;
  state: string;
  county: string;
  facilityMw: Sourced<number>;
  itMw: Sourced<number>;
  status: Sourced<DcStatus>;
  /** Financed sites on this campus that leases name, with the site's own GW figure. */
  sites: { slug: string; name: string; gw: Sourced<string> | null }[];
  leaseIds: string[];
}

export type LeaseLocationCollection = GeoJSON.FeatureCollection<GeoJSON.Point, LeaseLocation> & {
  asOf: string;
  /** Leases in the document, and how many of them sit on a mapped campus. */
  total: number;
  located: number;
};

/** Latest doc per key for one view, as of the date (one query for all keys). */
async function latestDocs<T>(db: Db, view: string, keys: string[], asOf: string | null): Promise<Map<string, T>> {
  if (keys.length === 0) return new Map();
  const res = await db.query<{ key: string; doc: T }>(
    `SELECT DISTINCT ON (key) key, doc FROM read_model
     WHERE view = $1 AND key = ANY($2::text[]) AND ($3::date IS NULL OR as_of <= $3::date)
     ORDER BY key, as_of DESC`,
    [view, keys, asOf],
  );
  return new Map(res.rows.map((r) => [r.key, r.doc]));
}

const located = (row: DataCenterRow | undefined): row is DataCenterRow =>
  !!row && Number.isFinite(row.lat) && Number.isFinite(row.lon);

/** The lease's financed sites: every linked site (`sites`), else its single `siteSlug`. */
export function leaseSiteSlugs(lease: LeaseMapPage['leases'][number]): string[] {
  const slugs = (lease.sites ?? []).map((s) => s.slug).filter(Boolean);
  return slugs.length > 0 ? [...new Set(slugs)] : lease.siteSlug ? [lease.siteSlug] : [];
}

const campusIdsOf = (tab: SiteBuildings | undefined) => (tab?.campuses ?? []).map((c) => c.id).filter(Boolean);

/** Campus ids per lease: its own campusId, else every campus on its linked sites. */
function campusesByLease(page: LeaseMapPage, siteTabs: Map<string, SiteBuildings>): Map<string, string[]> {
  const out = new Map<string, string[]>();
  for (const lease of page.leases) {
    const ids = lease.campusId ? [lease.campusId] : leaseSiteSlugs(lease).flatMap((slug) => campusIdsOf(siteTabs.get(slug)));
    out.set(lease.id, [...new Set(ids)]);
  }
  return out;
}

const newLocation = (id: string, row: DataCenterRow): LeaseLocation => ({
  id, name: row.name, lat: row.lat, lon: row.lon, state: row.state, county: row.county,
  facilityMw: row.facilityMw, itMw: row.itMw, status: row.status, sites: [], leaseIds: [],
});

function addSite(loc: LeaseLocation, slug: string | null, site: SiteSummary | undefined): void {
  if (!slug || loc.sites.some((s) => s.slug === slug)) return;
  loc.sites.push({ slug, name: site?.name ?? slug, gw: site?.gw ?? null });
}

export async function leaseLocations(db: Db, page: LeaseMapPage, asOf: string | null): Promise<LeaseLocationCollection> {
  const slugs = [...new Set(page.leases.flatMap(leaseSiteSlugs))];
  const [siteTabs, sites] = await Promise.all([
    latestDocs<SiteBuildings>(db, 'site_tab:buildings', slugs, asOf),
    db.readModel<SiteSummary[]>('sites', 'all', asOf),
  ]);
  const siteBySlug = new Map((sites ?? []).map((s) => [s.slug, s]));
  const campusesOf = campusesByLease(page, siteTabs);
  const rows = await latestDocs<DataCenterRow>(db, 'dc_row', [...new Set([...campusesOf.values()].flat())], asOf);

  const byCampus = new Map<string, LeaseLocation>();
  let placed = 0;
  for (const lease of page.leases) {
    const here = (campusesOf.get(lease.id) ?? []).filter((id) => located(rows.get(id)));
    for (const id of here) {
      const loc = byCampus.get(id) ?? newLocation(id, rows.get(id) as DataCenterRow);
      byCampus.set(id, loc);
      loc.leaseIds.push(lease.id);
      // The lease's sites on this campus (all of them when the lease names the campus itself).
      const sitesHere = leaseSiteSlugs(lease).filter((slug) => lease.campusId === id || campusIdsOf(siteTabs.get(slug)).includes(id));
      for (const slug of sitesHere) addSite(loc, slug, siteBySlug.get(slug));
    }
    if (here.length > 0) placed += 1;
  }

  const features = [...byCampus.values()]
    .sort((a, b) => a.id.localeCompare(b.id))
    .map((loc): GeoJSON.Feature<GeoJSON.Point, LeaseLocation> => ({
      type: 'Feature', id: loc.id, geometry: { type: 'Point', coordinates: [loc.lon, loc.lat] }, properties: loc,
    }));
  return { type: 'FeatureCollection', features, asOf: page.asOf, total: page.leases.length, located: placed };
}
