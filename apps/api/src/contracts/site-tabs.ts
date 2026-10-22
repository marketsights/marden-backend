// Payloads of the Site detail tabs that handoff/api/types.ts names only in words (endpoints.md:
// "GET /api/sites/:slug/{evidence,power,opposition,events,debt,lease} -> tab data"). Copied from the
// web app's provisional contract (marden-front-end src/lib/site-tab-types.ts) so both sides check
// against one shape; read models site_tab:<tab> (services/marden/readmodels/views/sites_*.py) build them.
import type { DebtBasis, Evidence, LinkRole, LinkStatus, PowerPlantRow, SiteBuildings, SiteSchedule, Sourced } from './types.js';

/** A tab KPI: value with its tag, one sub-line, and whether the value is bad news. */
export interface SiteKpi<T> {
  value: Sourced<T>;
  note?: string;
  tone?: 'default' | 'bad' | 'warn';
}

// ---------- GET /api/sites/:slug/evidence ----------
/** Permit, Zoning, Docket, Filing, Utility, Court, Press, Rating, County, Imagery … */
export interface SiteRecord extends Evidence {
  type: string;
}
export interface SiteEvidenceTab {
  /** Newest first. */
  records: SiteRecord[];
  /** Monthly Sentinel-2 images, oldest first (same shape as SiteSchedule.imagery). */
  imagery: SiteSchedule['imagery'];
  /** The linked campuses' buildings, plan and imagery read (same shape as SiteSchedule.imageryCampuses). */
  imageryCampuses?: SiteSchedule['imageryCampuses'];
  imageryNaip?: SiteSchedule['imageryNaip'];
  /** Finding from the change scores, e.g. "Little visible change since June" (M). */
  imageryFinding?: Sourced<string>;
}

// ---------- GET /api/sites/:slug/power ----------
export interface PowerSourceTrack {
  /** "Gas pipeline", "Gas turbines", "Fuel cells", "Grid". */
  source: string;
  // eslint-disable-next-line @typescript-eslint/no-redundant-type-constituents -- known values for readers; any string is accepted (web app contract)
  status: 'Planned' | 'Slipped' | 'Withdrawn' | 'Denied' | 'Pending' | 'In service' | string;
  /** First record that dated this source: the start of its bar. */
  from: string;
  /** Planned in-service date now (YYYY-MM-DD, YYYY-MM or YYYY); value null when no date is filed. */
  plannedInService: Sourced<string>;
  /** Each record that changed the plan, oldest first. */
  changes: { date: string; label: string; evidence: Evidence }[];
  /** Shown on the bar, e.g. "Up to 2.45 GW planned". */
  note?: string;
}
export interface SitePowerTab {
  powerNeeded: SiteKpi<string>;
  /** Marden count of filed permits, in GW. */
  permittedOnsiteGw: SiteKpi<number>;
  gasInService: SiteKpi<string>;
  gridConnection: SiteKpi<string>;
  /** Chart title, e.g. "Every power source is late or unpermitted". */
  headline?: string;
  sources: PowerSourceTrack[];
  /** Collectors watching this site's dockets. */
  watched: { agency: string; what: string; cadence: string; url?: string }[];
  plant?: PowerPlantRow | null;
  /** In-service date history, newest first. */
  history: {
    recorded: string; source: string; change: string; evidence: Evidence;
    /** Other outlets' reports of the same development that day (same power source). */
    reports?: { change: string; evidence: Evidence }[];
  }[];
}

// ---------- GET /api/sites/:slug/opposition ----------
export interface OppositionItem {
  /** YYYY-MM-DD, or YYYY-MM when only the month is reported. */
  date: string;
  /** "County vote", "Moratorium", "Bond suit", "Ruling", "Petition" … */
  type: string;
  text: string;
  /** Court or body. */
  where: string;
  status: string;
  statusTone?: 'red' | 'amber' | 'green' | 'grey';
  evidence: Evidence;
}
export interface SiteOppositionTab {
  openCases: SiteKpi<number>;
  latestRuling: SiteKpi<string>;
  nextHearing: SiteKpi<string>;
  ifVoided: SiteKpi<string>;
  /** Card title, e.g. "The bond approval is being challenged in four cases". */
  headline?: string;
  /** Newest first. */
  items: OppositionItem[];
  note?: string;
}

// ---------- GET /api/sites/:slug/events ----------
export interface LaneSegment {
  from: string;
  to: string;
  status: LinkStatus;
  /** Hover text: why the link had this status. */
  reason: string;
}
export interface SiteEvent extends Evidence {
  severity: LinkStatus;
  /** Chain link or area it touched: "Tenant", "Lenders", "Schedule". */
  link: string;
  /** Rule codes or rule words: "M1, M2", "Power denied". */
  ruleHit: string | null;
  /** "Cushion +1 → −2 months", "Green → red". */
  changed: string | null;
  /** Later press reports of the same event (same rule within 30 days, or the same headline elsewhere). */
  reports?: number;
}
export interface SiteEventsTab {
  /** Card title, e.g. "The chain broke on Jul 9; the loan repriced on Sep 19". */
  headline?: string;
  period: { start: string; end: string };
  /** One lane per link in role order, then the chain. */
  lanes: { role: LinkRole | 'chain'; segments: LaneSegment[] }[];
  /** Newest first. */
  events: SiteEvent[];
}

// ---------- GET /api/sites/:slug/debt ----------
export interface SiteDebtInstrument {
  instrumentId: string | null;
  /** Payer page for a tenant's own notes. */
  payerSlug?: string | null;
  name: string;
  sub: string;
  sizeB: Sourced<number>;
  /** Dealer quote, or a word: "traded", "private". null = not applicable. */
  quote: Sourced<string> | string | null;
  mardenPrice: Sourced<number> | null;
  /** Recovery range ("100c"; the price model's default reads "assumed 55c"), or a word: "first loss". */
  recovery: Sourced<string> | string | null;
}
/** One line of the recovery arithmetic; value null = n/d, with `reason` in plain words. */
export interface SiteRecoveryLine {
  label: string;
  value: Sourced<string>;
  reason?: string;
}
export interface SiteDebtTab {
  /** SPV that borrows, e.g. "Red Chiles A–D LLC". */
  spvName?: string;
  // eslint-disable-next-line @typescript-eslint/no-redundant-type-constituents -- known values for readers; any string is accepted (web app contract)
  capitalStack: { label: string; kind: 'loan' | 'equity' | string; amountB: Sourced<number> }[];
  stackNote?: string;
  instruments: SiteDebtInstrument[];
  recovery: {
    /** Party whose exit the recovery assumes, e.g. "Oracle". */
    ifWalks: string;
    /** The recovery model's own cents per dollar ("100c", "62–74c"); value null when it lacks an input. */
    range: Sourced<string>;
    /** Why the range is n/d, in plain words. */
    reason?: string;
    /** Only when the range is n/d: the price model's recovery, a default ("assumed 55c (price model v0.1 default)"). */
    assumed?: Sourced<string>;
    /** Value basis, less claims ahead, over the claims at the loan's rank = cents; then what model v0 leaves out. */
    inputs: SiteRecoveryLine[];
  };
  lenders: { name: string; role: Sourced<string>; share: Sourced<number> }[];
  holdersNote: string;
  /** What the main instrument's size, quote, Marden price and recovery each are. */
  basis?: DebtBasis;
}

// ---------- GET /api/sites/:slug/lease ----------
export interface LeaseTerm {
  // eslint-disable-next-line @typescript-eslint/no-redundant-type-constituents -- known values for readers; any string is accepted (web app contract)
  key: 'parties' | 'rent_start' | 'outside_date' | 'force_majeure' | 'rent' | 'term' | 'termination' | 'guarantee' | string;
  label: string;
  /** value null = unknown; shown as "[pending]" when the API says where it will come from, else n/d. */
  value: Sourced<string>;
  pending?: string;
}
export interface SiteLeaseTab {
  /** Card title, e.g. "Oracle's lease with the SPV". */
  title: string;
  note?: string;
  terms: LeaseTerm[];
  /** "What the terms mean for lenders". */
  outcomes: { label: string; text: string; value?: Sourced<string> }[];
  today: Sourced<string>;
  todayTone?: 'red' | 'amber' | 'green';
}

// ---------- GET /api/sites/:slug/buildings additions ----------
/** Optional fields on SiteBuildings the Buildings tab shows when the API sends them. */
export interface SiteBuildingsExtras {
  summary?: {
    planned: SiteKpi<number>;
    shellsUp: SiteKpi<number>;
    energized: SiteKpi<number>;
    progress: SiteKpi<number>;
  };
  /** A building fact in the review queue, e.g. "Sources disagree on the building count". */
  review?: { title: string; text: string; evidence: Evidence[] };
  /** Site link approved by a reviewer (spec 16). */
  linkApproved?: boolean;
}
export type SiteBuildingsTab = SiteBuildings & SiteBuildingsExtras;

// ---------- GET /api/sites and /api/completion additions (spec 16) ----------
/** "Bldgs" on Completion (03) and Sites (05): built / total buildings on the site's linked campuses.
 * Copied from the web app's src/lib/api-types.ts (WithBuildingCount); the sites and completion read
 * models send it on every row, and site_detail (GET /api/sites/:slug: Buildings tab count and the
 * header's "{n} buildings", spec 16), null (n/d) when no linked campus has a building. */
export interface SiteBuildingCount {
  built: number;
  total: number;
  /** Linked campus for the "›" link to Data centers (spec 14). */
  campusId: string | null;
}
export interface WithBuildingCount {
  buildings?: Sourced<SiteBuildingCount> | null;
}

// ---------- GET /api/sites/:slug/schedule additions (spec 04 Milestones) ----------
/** A plan window a source states: first and last month ('YYYY-MM'), the source's wording, and the
 * statement's source when the milestone's evidence row states it (read model sites_milestones). */
export interface StatedWindow {
  from: string;
  to: string;
  text: string;
  tag?: 'F' | 'R' | 'M';
  sourceName?: string;
  asOf?: string;
  sourceUrl?: string;
}
/** Dates the Schedule tab's milestone rows add to types.ts Milestone; a date no source states is absent
 * and `dateNotes` says why. `controlsRent`: share of the cushion model's simulated schedules in which
 * the milestone set the rent-start date; `rentPath`: on the critical path to rent start. */
export interface MilestoneDates {
  target?: StatedWindow;
  originalTarget?: StatedWindow;
  actual?: StatedWindow;
  dateNotes?: { target?: string; originalTarget?: string; actual?: string };
  rentPath?: boolean;
  controlsRent?: number;
}
/** The cushion run the Schedule tab shows: its day, simulation count, method and power mode. */
export interface CushionModelInfo {
  runDate: string;
  simulations: number | null;
  methodVersion: string | null;
  powerMode: string | null;
}
export type SiteScheduleDated = Omit<SiteSchedule, 'milestones'> & {
  milestones: (SiteSchedule['milestones'][number] & MilestoneDates)[];
  cushionModel?: CushionModelInfo;
};
