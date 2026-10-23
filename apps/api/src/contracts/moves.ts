// Moves and event effects (read models moves, instrument_moves, site_debt_effects; services/marden/readmodels/
// views/moves.py and debt_effects.py; methods docs/methods/moves-v0.1.md and event-impact-v0.1.md). The web app
// keeps an identical copy in src/lib/moves-types.ts (import path aside).
import type { Evidence, Sourced, Tag } from './types.js';

export type MoveWindowKey = '1D' | '1W' | '1M' | 'QTD' | 'YTD';

/** One end of a move: the price compared and its source (a trade's tag as stored, or the Marden price, M). */
export interface MovePoint {
  date: string; price: number; tag: Tag; sourceName: string; sourceUrl?: string;
  basis: 'trade' | 'model';
  trades?: number;                             // prints that day (traded)
  ytm: number | null; ytw: number | null; dmBp: number | null;   // yield model at this price; null = n/d
}
/** What moved the price most in the window: a priced event, rates, spreads, or (traded) the trades. */
export interface MoveDriver {
  kind: 'event' | 'rates' | 'spreads' | 'trade' | 'none';
  text: string;                                // "Projected completion moved from Aug 2028 to Jan 2029 (Oct 21)"
  pts: number | null;                          // its part of the move, points (M)
  href: string | null;                         // the event on the site's Material events tab
  detail: string;                              // hover: how the move splits
}
export interface MoveWindow {
  key: MoveWindowKey;
  nominalStart: string;                        // where the window starts by the calendar
  start: string | null;                        // date of the start price actually compared
  end: string | null;                          // date of the end price
  partial: boolean;                            // the history begins inside the window
  basis: 'trade' | 'model' | null;
  from: MovePoint | null; to: MovePoint | null;
  change: Sourced<number>;                     // points; null = n/d (see why)
  yieldMeasure: 'YTW' | 'YTM';
  yieldChangeBp: Sourced<number>;              // M (no Marden price: the trade's tag); null = n/d (see yieldWhy)
  dmChangeBp: Sourced<number> | null;          // floating rate only
  why: string | null;                          // why change is n/d
  yieldWhy: string | null;
  note: string | null;                         // why start differs from nominalStart
  driver: MoveDriver;
}
export interface MoveLastTrade { date: string; price: number; trades: number; tag: Tag; sourceName: string; sourceUrl?: string }
export interface MoveRow {
  instrumentId: string; name: string; type: string; sizeB: Sourced<number>;
  issuer: string;
  site: { slug: string; name: string } | null;
  tenant: string | null; endUser: string | null;
  couponType: 'fixed' | 'floating' | null;
  lastTrade: MoveLastTrade | null;             // null = no trade on record
  windows: Record<MoveWindowKey, MoveWindow>;
}
export interface MethodRef { label: string; methodVersion: string; sourceUrl: string }
// GET /api/moves?type=&site=&tenant=&endUser=
export interface MovesPage {
  asOf: string;
  windows: { key: MoveWindowKey; label: string; nominalStart: string }[];
  history: { modelFrom: string | null; tradesFrom: string | null; note: string };
  rows: MoveRow[];
  method: MethodRef;
}

/** One instrument's price on an event's day with the event's inputs and without them (M). */
export interface InstrumentEffect {
  instrumentId: string; name: string;
  date: string;                                // the day the price model first saw it
  label: string;                               // the input that changed, in words
  without: Sourced<number>; with: Sourced<number>;
  change: Sourced<number>;                     // points; null = n/d (see why)
  yieldMeasure: 'YTW' | 'YTM';
  yieldChange: Sourced<number> | null;         // percentage points
  dmChangeBp: Sourced<number> | null;
  note: string | null;                         // e.g. the stored price of that day differs
  why: string | null;
}
export interface TradePrint { date: string; price: number; trades: number; tag: Tag; sourceName: string; sourceUrl?: string }
/** Observed trades around the event: the last trade day before it and the first after it. */
export interface MarketMove {
  instrumentId: string; name: string;
  before: TradePrint | null; after: TradePrint | null;
  change: Sourced<number>;                     // the trades' own tag
  why: string | null;
}
export interface DebtEffect {
  changed: boolean;
  line: string;                                // "Effect on debt: Project Jupiter Construction loans −1.4 pts (87.3 → 85.9), YTW +0.6%"
  reason: string | null;                       // plain words when there is no change
  effectiveDate: string | null;
  label: string | null;
  effects: InstrumentEffect[];
  market: MarketMove[];
  inputs: (Evidence & { kind?: string; kindWords?: string })[];   // the records the price model read for it
}
// GET /api/sites/:slug/debt-effects. effects is keyed `${date}|${text}` of the Events and Evidence rows;
// a row without an entry takes `default`.
export interface SiteDebtEffects { asOf: string; slug: string; default: DebtEffect; effects: Record<string, DebtEffect>; method: MethodRef }

export interface WhatMovedRow {
  date: string;
  kind: 'event' | 'market';                    // market: a tenant's or end user's bond around a site's event
  title: string; label: string; href: string | null;
  effect: InstrumentEffect | null;
  market: MarketMove | null;
  inputs: (Evidence & { kind?: string; kindWords?: string })[];
  note: string | null;
}
// GET /api/instruments/:id/moves
export interface InstrumentMoves {
  instrumentId: string; asOf: string;
  windows: Record<MoveWindowKey, MoveWindow>;
  lastTrade: MoveLastTrade | null;
  measure: 'YTW' | 'YTM';
  couponType: 'fixed' | 'floating' | null;
  whatMoved: WhatMovedRow[];
  method: MethodRef;
}
