// The business day: US Eastern (spec 22: snapshots and ?asOf days are dated in US Eastern; Python's
// services/marden/readmodels/context.py business_today / day_end). The database session is UTC, so a bare date
// compared with a timestamp is midnight UTC, 8 pm EDT / 7 pm EST of the day before in New York, and
// `new Date().toISOString().slice(0, 10)` is tomorrow from 8 pm on. Every "known on day D" read compares
// timestamps with dayEnd(D) and every "today" is businessToday().

export const BUSINESS_TZ = 'America/New_York';

/**
 * SQL: the instant the day bound to `dayPh` ends in business time, midnight US Eastern after it. A row
 * learned, created or recorded before it was known on that day. fact_asof(day) uses the same instant
 * (db/migrations/20261022050000_fact_asof_business_day.sql).
 */
export function dayEnd(dayPh: string): string {
  return `((${dayPh}::date + 1)::timestamp AT TIME ZONE '${BUSINESS_TZ}')`;
}

const PARTS = new Intl.DateTimeFormat('en-US', {
  timeZone: BUSINESS_TZ, hourCycle: 'h23',
  year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
});

/** New York wall-clock time at `at`, as if it were UTC (ms). */
function wallClock(at: Date): number {
  const p = Object.fromEntries(PARTS.formatToParts(at).map((x) => [x.type, Number(x.value)]));
  return Date.UTC(p.year ?? NaN, (p.month ?? NaN) - 1, p.day ?? NaN, p.hour ?? NaN, p.minute ?? NaN, p.second ?? NaN);
}

/** The business day `at` falls on (YYYY-MM-DD). */
export function businessDay(at: Date): string {
  return new Date(wallClock(at)).toISOString().slice(0, 10);
}

/** Today in business time (YYYY-MM-DD). */
export const businessToday = (now: Date = new Date()): string => businessDay(now);

/** dayEnd in JS: midnight US Eastern after `day` (YYYY-MM-DD), with that night's UTC offset (EDT or EST). */
export function dayEndAt(day: string): Date {
  const next = new Date(`${day}T00:00:00Z`);
  next.setUTCDate(next.getUTCDate() + 1);
  const wall = next.getTime(); // the next midnight as New York wall-clock time
  // The instant is that wall time minus New York's UTC offset then (-4 h EDT, -5 h EST). The first pass takes
  // the offset at midnight UTC, the second at the instant itself; DST switches at 2 am, so midnight always exists.
  let at = wall;
  for (let i = 0; i < 2; i++) at = wall - (wallClock(new Date(at)) - at);
  return new Date(at);
}
