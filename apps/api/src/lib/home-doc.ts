// GET /api/home (spec 01): the org's cut of the `home` read model. The read model holds every name's
// pieces for the week, each tagged with the watchlist entries it belongs to (HomeDoc, built by
// services/marden/readmodels/views/home.py); this keeps the org's names, sums the headline numbers over
// them and applies the user's price threshold. No value is recomputed: sums add the pieces' own values.
import type { HomeCatalyst, HomeChange, HomeDoc, HomeResponse, WatchKey } from '../contracts/app.js';
import type { Sourced, Tag } from '../contracts/types.js';

export interface Watched { sites: string[]; instruments: string[] }

const MAX_CHANGES = 20;  // the widget shows 5, ordered by status then date; the rest are for its re-sort
const MAX_COMING_UP = 8; // the widget shows the next 4
const TAG_RANK: Record<Tag, number> = { F: 0, R: 1, M: 2 };

export const watchKeys = (w: Watched): Set<WatchKey> =>
  new Set([...w.sites.map((s) => `site:${s}`), ...w.instruments.map((i) => `instrument:${i}`)]);

const kept = (keys: Set<WatchKey>) => (piece: { watch: WatchKey[] }): boolean => piece.watch.some((k) => keys.has(k));

/** Sum of Sourced numbers: the weakest tag, the newest as-of, each source once (as the site debt sum). */
export function sumSourced(values: Sourced<number>[]): Sourced<number> | null {
  const known = values.filter((v): v is Sourced<number> & { value: number } => typeof v.value === 'number');
  const [head, ...rest] = known;
  if (!head) return null;
  if (rest.length === 0) return { ...head };
  const tag = known.map((v) => v.tag).reduce((a, b) => (TAG_RANK[b] > TAG_RANK[a] ? b : a));
  const names = [...new Set(known.map((v) => v.sourceName).filter((n): n is string => Boolean(n)))].sort();
  const total = Math.round(known.reduce((s, v) => s + v.value, 0) * 1e4) / 1e4;
  const latest = rest.reduce((a, v) => (v.asOf > a ? v.asOf : a), head.asOf);
  return { value: total, tag, asOf: latest, ...(names.length ? { sourceName: names.join(' + ') } : {}) };
}

function watchedDebtUnder3(doc: HomeDoc, keys: Set<WatchKey>): HomeResponse['headline']['watchedDebtUnder3'] {
  // A watched site counts its whole debt; a watched loan on a site nobody watched counts its own size.
  const entries = doc.under3.filter(kept(keys)).filter((e) => !(e.site && keys.has(`site:${e.site}`)));
  const debt = sumSourced(entries.map((e) => e.debtB));
  if (debt) return { debtB: debt, names: [...new Set(entries.map((e) => e.name))] };
  // No watched name under 3 months: $0 when the cushion model has a value for one of them, else n/d.
  const known = doc.cushionKnown.some((k) => keys.has(k));
  return { debtB: { value: known ? 0 : null, tag: 'M', asOf: doc.asOf, sourceName: 'Marden cushion model' }, names: [] };
}

const stripChange = ({ watch: _w, severity: _s, ...change }: HomeDoc['changes'][number]): HomeChange => change;

function priceMoveAtLeast(change: HomeChange, pts: number): boolean {
  if (change.type !== 'price') return true;
  const to = typeof change.to.value === 'number' ? change.to.value : null;
  return typeof change.from === 'number' && to !== null && Math.abs(to - change.from) >= pts;
}

/** The first of each id, in order (a filing or step copied to a site and its instruments shows once). */
function unique<T>(items: T[], id: (item: T) => string): T[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    const key = id(item);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

const changeId = (c: HomeChange): string =>
  c.type === 'filing' ? `filing|${c.record.date}|${c.record.sourceUrl}|${c.record.text}` : `${c.type}|${c.kind}|${c.refId}|${c.label ?? ''}`;

const comingUp = (doc: HomeDoc, keys: Set<WatchKey>): HomeCatalyst[] =>
  unique(doc.comingUp.filter(kept(keys)), (c) => `${c.date}|${c.what}`)
    .slice(0, MAX_COMING_UP)
    .map((c) => ({ when: c.when, what: c.what, refUrl: c.refUrl }));

/** The org's Home from the shared document: only pieces of watched names, headline sums over them. */
export function cutHome(
  doc: HomeDoc,
  watched: Watched,
  opts: { priceThresholdPts: number; unreadAlerts: HomeResponse['unreadAlerts']; lastVisit: string | null },
): HomeResponse {
  const keys = watchKeys(watched);
  const changes = doc.statusChanges.filter(kept(keys));
  const marks = doc.fundMarksHigh.filter(kept(keys));
  return {
    asOf: doc.asOf,
    lastVisit: opts.lastVisit,
    headline: {
      watchedDebtUnder3: watchedDebtUnder3(doc, keys),
      statusChangesThisWeek: { count: changes.length, names: changes.map((c) => c.line) },
      fundMarksHigh: { count: marks.length, topName: marks[0]?.name ?? null, gapPts: marks[0]?.gapPts ?? null },
    },
    unreadAlerts: opts.unreadAlerts,
    watched,
    watchlistChanges: unique(
      doc.changes.filter(kept(keys)).map(stripChange).filter((c) => priceMoveAtLeast(c, opts.priceThresholdPts)),
      changeId,
    ).slice(0, MAX_CHANGES),
    comingUp: comingUp(doc, keys),
  };
}
