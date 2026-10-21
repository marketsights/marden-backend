// Risk list weeks (spec 10): the published lists are keyed by the Monday that starts their week.
import type { Db } from '../db.js';

const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

/** The Monday of the ISO week that holds `day` (YYYY-MM-DD), so ?week= accepts any day of the week. */
export function mondayOf(day: string): string {
  const d = new Date(`${day}T00:00:00Z`);
  const back = (d.getUTCDay() + 6) % 7; // Monday = 0 … Sunday = 6
  d.setUTCDate(d.getUTCDate() - back);
  return d.toISOString().slice(0, 10);
}

/** Every week with a published risk list as of the date, oldest first (the week picker's range). */
export async function publishedWeeks(db: Db, asOf: string | null): Promise<string[]> {
  const res = await db.query<{ key: string }>(
    `SELECT DISTINCT key FROM read_model WHERE view = 'risk_list' AND ($1::date IS NULL OR as_of <= $1::date) ORDER BY key`,
    [asOf],
  );
  return res.rows.map((r) => r.key).filter((key) => ISO_DAY.test(key));
}
