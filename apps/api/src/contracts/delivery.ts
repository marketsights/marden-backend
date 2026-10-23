// Delivery record and Mispriced (read models delivery and mispriced; services/marden/readmodels/views/delivery.py
// and mispriced.py; methods docs/methods/delivery-record-v0.1.md and mispriced-v0.1.md). The web app keeps an
// identical copy in src/lib/delivery-types.ts (import path aside).
import type { Sourced, Tag } from './types.js';

// ---------------------------------------------------------------- GET /api/delivery
export type DeliveryRole = 'developer' | 'issuer' | 'tenant';
/** A dated statement compared: the month it gives and the record behind it (null tag: the record is not on file). */
export interface DeliveryClaim {
  month: string;                               // YYYY-MM ('Q4 2026' counts as its last month)
  text: string;                                // the source's words, "2026-Q4", "Estimated completion 2026-03-21"
  tag: Tag | null; sourceName: string | null; sourceUrl?: string; asOf: string | null;
}
/** One milestone, building or in-service date judged against the first target a source stated for it. */
export interface DeliveryItem {
  siteSlug: string; siteName: string;
  href: string;                                // the site's Schedule or Buildings tab
  kind: 'milestone' | 'building' | 'in_service';
  what: string;                                // "First hall energized", "Building 3", "In-service date"
  first: DeliveryClaim | null;                 // first stated target
  current: DeliveryClaim | null;               // the completion when done, else the current target
  done: boolean;
  outcome: 'on_time' | 'late' | 'open' | 'open_late' | 'done_unjudged';
  slipMonths: number | null;                   // current (or completion) month minus the first month (M)
  atLeast: boolean;                            // still open past its date: the slip grows every month
}
/** A schedule push: a milestone marked slipped, a filed or reported change that moved a date later, or a later
 *  Marden projected completion (tag M). One a day per site. */
export interface DeliveryPush {
  siteSlug: string; siteName: string; date: string; text: string;
  tag: Tag; sourceName: string; sourceUrl?: string; href: string;
}
export interface DeliverySite {
  slug: string; name: string; href: string;
  roles: DeliveryRole[];                       // this company's roles on the site
  debtB: Sourced<number> | null;
  deadline: { outsideDate: string; assumed: boolean; cushionMonths: Sourced<number> | null; behind: boolean | null } | null;
  items: DeliveryItem[];
  pushes: DeliveryPush[];                      // newest first
}
export interface DeliveryRow {
  key: string;                                 // entity id where the company has one (payer pages use it)
  name: string;
  roles: DeliveryRole[];
  payerHref: string | null;
  rank: number | null;                         // worst first; null = no slip to judge
  sitesTracked: Sourced<number>;
  onTime: Sourced<number>; late: Sourced<number>; open: Sourced<number>;
  openLate: number;                            // of the open ones, behind their first target
  unjudged: number;                            // done, but no first target or no completion date on file
  judged: number;                              // items with a slip
  avgSlipMonths: Sourced<number>;              // value null: nothing to judge (why.slip)
  worstSlipMonths: Sourced<number>;
  worst: { siteName: string; what: string; href: string; atLeast: boolean } | null;
  pushes90d: Sourced<number>;
  behindDeadline: Sourced<number>;             // share 0-1 of the sites with a deadline; null = none has one
  behindCount: number; deadlineSites: number; assumedDeadlines: number;
  debtB: Sourced<number> | null;               // sum of the sites' debt, weakest tag of the parts
  vsPeers: { deltaMonths: number | null; text: string };
  why: { slip?: string; behindDeadline?: string; debtB?: string };
  sites: DeliverySite[];
}
export interface DeliveryPage {
  asOf: string;
  rows: DeliveryRow[];
  peers: { medianAvgSlipMonths: Sourced<number>; developers: number };
  pushDays: number;
  gaps: string[];                              // what the record cannot see, in plain words
  method: { label: string; methodVersion: string; sourceUrl: string };
}

// ---------------------------------------------------------------- GET /api/mispriced
/** The market price compared: a trade (F), a quote (R), a reported dealer quote, or the funds' filed marks. */
export interface MarketEvidence {
  kind: 'trade' | 'quote' | 'dealer' | 'fund_mark';
  price: number; date: string; tag: Tag; sourceName: string; sourceUrl?: string;
  prints?: number;                             // trades that day
  holders?: number; parMm?: number;            // fund marks: funds and par behind the average
}
/** An event dated after the market evidence. */
export interface MispricedEvent {
  date: string;
  kind: 'slip' | 'signal' | 'quote' | 'material' | 'filing' | 'rating' | 'permit' | 'opposition' | 'notice' | 'news'
    | 'interconnection' | 'queue' | 'tenant_news';
  text: string; tag: Tag; sourceName: string; sourceUrl?: string; href: string | null;
  effectPts: number | null;                    // the event-impact model's effect on this instrument (M); null = none
  effectNote: string | null;                   // why there is no effect
  severity: string | null;                     // the event pipeline's red / amber / green
}
export type MispricedGroup = 'downside' | 'bounce' | 'stale';
export interface MispricedRow {
  instrumentId: string; name: string; href: string; type: string | null; issuer: string;
  site: { slug: string; name: string } | null;
  sizeB: Sourced<number>;
  mardenPrice: Sourced<number>;
  market: MarketEvidence | null;
  gapPts: Sourced<number>;                     // Marden minus market; negative = market above our value
  gapBp: Sourced<number>;                      // Marden yield minus market yield, bp
  measure: 'YTW' | 'YTM';
  yieldMarden: number | null; yieldMarket: number | null;
  groups: MispricedGroup[];
  negativeEvent: boolean; positiveEvent: boolean;
  events: MispricedEvent[];                    // the most important first, at most 8
  eventCount: number;
  driver: MispricedEvent | null;
  daysSinceEvent: number | null;               // days the market has had since the driver
  confidence: string;                          // "Model only: no trade or quote since Sep 24"
  assumptions: string[];                       // model inputs that are assumptions, not records
  basisNote: string | null;                    // set when the gap rests on those assumptions, not on events
  modelBasis: string[];                        // what the Marden price leans on
  stale: { fundMark: MarketEvidence; trade: MarketEvidence; gapPts: Sourced<number> } | null;
  score: number;                               // |gap| x size ($B), the sort
  why: { market?: string; gapBp?: string; size?: string };
}
export interface MispricedPage {
  asOf: string;
  rows: MispricedRow[];                        // |gap| x size, largest first
  counts: Record<MispricedGroup, number>;
  thresholds: { gapPts: number; eventPts: number };
  method: { label: string; methodVersion: string; sourceUrl: string };
}
