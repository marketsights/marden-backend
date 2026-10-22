import type * as GeoJSON from 'geojson'; // npm i -D @types/geojson
// Response shapes for Marden's internal app endpoints (not a customer API).
// Every displayed value is Sourced<T>. Fixtures in /fixtures follow these types.

export type Tag = 'F' | 'R' | 'M';           // Filed record | Reported in press | Marden model
export interface Sourced<T> {
  value: T | null;                            // null = n/d (not disclosed)
  tag: Tag;
  sourceUrl?: string;                         // document or method page
  sourceName?: string;                        // "FERC eLibrary", "N-PORT", "Reuters"
  asOf: string;                               // ISO date the value was true
  methodVersion?: string;                     // for M values, e.g. "price v0.3"
}

export type LinkRole = 'end_user' | 'tenant' | 'developer' | 'lenders';
export type LinkStatus = 'red' | 'amber' | 'green' | 'none';
export type ChainStatus = 'Broken' | 'Strained' | 'Holding' | 'No build';

export interface ChainLink {
  role: LinkRole;
  party: string;                              // "Oracle"
  status: LinkStatus;
  ruleCode?: string;                          // "T2"
  reason: string;                             // tooltip text
  fact?: Sourced<string>;                     // "Force majeure notice Sep 24"
}

export interface CushionProjection {
  p10: string; p50: string; p90: string;      // ISO month 'YYYY-MM'
  outsideDate: Sourced<string>;               // 'YYYY-MM' or 'YYYY' when only the year is known
  outsideDateAssumed?: string;                // ISO date the cushion uses when the lease gives only a year, e.g. '2028-12-31'
  cushionMonths: Sourced<number> | null;      // outsideDate − p50; null when no lease date
  deltaWeek: Sourced<number>;                 // change vs 7 days ago
  oddsOfMissing?: Sourced<number>;            // 0..1; methodVersion 'odds v0' until the backtest calibrates it
  costPerMonthLate?: Sourced<number>;         // USD
}

export interface Evidence {
  date: string; text: string; tag: Tag; sourceName: string; sourceUrl: string;
}

// GET /api/completion
export interface CompletionRow {
  slug: string; name: string; state: string;
  chain: ChainLink[];                         // always 4, in role order
  projection: CushionProjection | null;
  driver: 'Power' | 'Build' | 'Delivery' | 'Permits' | 'Funding' | null; // null = n/d: the cushion model named no driver
  lastEvidence: Evidence;
  debtB: Sourced<number>;
  leadLenders: string;
}
export interface CompletionResponse {
  asOf: string;
  summary: {
    pastOutside: { count: number; debtB: number; names: string[] };
    under3: { count: number; debtB: number; names: string[] };
    slippedThisWeek: { count: number; months: number; record?: Evidence };
    debtByBand: { band: '<0' | '0-3' | '3-6' | '6-12' | '12+'; debtB: number }[];
  };
  rows: CompletionRow[];
}

// GET /api/sites and /api/sites/:slug
export interface SiteSummary {
  slug: string; name: string; county: string; state: string;
  endUser: string; tenant: string | null; developer: string;
  gw: Sourced<string>; chain: ChainLink[]; status: ChainStatus;
  cushionMonths: Sourced<number> | null; rentStartP50: string | null;
  debtB: Sourced<number>; leadLenders: string;
}
export interface Milestone {
  key: 'land_zoning' | 'air_permit' | 'utility_agreement' | 'gas_or_grid' | 'onsite_generation' | 'shells' | 'first_hall' | 'rent_start' | string;
  label: string;
  status: 'Done' | 'Denied' | 'Withdrawn' | 'Slipped' | 'Not started' | 'Early works' | 'Projected' | 'At risk';
  events: { date: string; kind: 'done' | 'denied' | 'slipped' }[];
  projected?: { p10: string; p50: string; p90: string };
  latestEvidence?: Evidence;
}
export interface SiteSchedule {
  projection: CushionProjection | null;       // null = n/d: the cushion model has not projected this site
  milestones: Milestone[];
  cushionHistory: { date: string; months: number; event?: string }[];
  imagery: ImageryImage[];
  /** The linked campuses' buildings, plan and imagery read, for marking up the images. */
  imageryCampuses?: ImageryCampus[];
  /** Newest NAIP aerial photo (0.6 m, public domain) over the same box as the Sentinel-2 images. */
  imageryNaip?: Sourced<NaipImage>;
  documents: Evidence[];
}
/** One monthly Sentinel-2 image (oldest first in every list). */
export interface ImageryImage {
  month: string; url: string; caption: string;
  changeScore?: number;                       // Marden change score (M) vs the earlier image named by comparedWith
  date?: string;                              // acquisition day
  bbox?: [number, number, number, number];    // [west, south, east, north], WGS84: the image is a lon/lat grid over exactly this box
  comparedWith?: string;                      // acquisition day of the earlier image the score compares with (~3 months before)
  changeThreshold?: number;                   // score from which Marden records a visible change
  changeMethod?: string;                      // method version of the score, e.g. "change-score@0.1"
}
/** A building outline for the image markup: footprint, status and construction dates as recorded. */
export interface ImageryBuilding {
  id: string; label: string; footprint: GeoJSON.Polygon;
  status: Sourced<DcStatus>;
  constructionStart: Sourced<string>;         // null = n/d
  constructionEnd: Sourced<string>;           // null = n/d
  imageryStage?: BuildingImageryStage;
}
/** What 10 m optical imagery shows on a footprint per capture (Marden model, M, building-stage@0.1).
 * Construction progress only: optical imagery cannot show whether a hall is energized. */
export type ImageryStageName = 'bare_ground' | 'under_construction' | 'roofed';
export interface BuildingImageryStage {
  latest: Sourced<{ stage: ImageryStageName; detail: string; confidence: number }>;
  roofedBy: Sourced<string>;                  // first capture of a roofed run later captures uphold; null = not seen roofed
  readings: { date: string; stage: ImageryStageName; confidence: number }[];   // oldest first
}
/** A NAIP aerial photo: one or more tiles, each a lon/lat image over its bbox. */
export interface NaipImage {
  date: string; gsd: number;                  // acquisition day; metres per pixel
  bbox: [number, number, number, number];
  tiles: { url: string; bbox: [number, number, number, number] }[];
  caption: string;
}
/** A campus as the image markup needs it; every field is a stored row (n/d when none). */
export interface ImageryCampus {
  id: string; name: string; lat: number | null; lon: number | null;
  buildings: ImageryBuilding[];
  plan: Sourced<{ date: string; buildingsOperational: number }[]>;   // planned buildings operational by date
  operational: Sourced<number>;               // newest dated count of buildings operational
  buildingCount: Sourced<number>;             // buildings the campus is stated to have
  originalSchedule: Sourced<string>;          // delivery schedule as originally stated
  schedule: Sourced<string>;                  // delivery schedule as now stated
  constructionNote: Sourced<string>;
  read: Sourced<{ date: string | null; flag: string | null; text: string; roofHa: number | null }>;   // Marden's read of the imagery (M)
}
export interface SiteDetail extends SiteSummary {
  facts: string;                              // "2.2–2.45 GW · ~$18B construction loan"
  trackingNames: string[];
  tabCounts: { evidence: number; opposition: number; events: number };
  debtExposed: { instrumentId: string; name: string; sizeB: Sourced<number>; dealerQuote?: Sourced<string>; mardenPrice: Sourced<number>; recovery: Sourced<string>; holdersNote: string; basis?: DebtBasis };
  /** One row per development, dated by its earliest report; `sources` lists every report when there are several. */
  materialEvents: (Evidence & { ruleCode?: string; severity: LinkStatus; sources?: Evidence[] })[];
}
/** What the debt card's numbers each are (spec 04): sizes, quotes and recoveries are not comparable unlabelled. */
export interface DebtBasis {
  instrument: string;                         // "Construction loans (EX-JUPITER-190)"
  size: {
    label: string;                            // "Committed facility size" | "Drawn amount" | "Par held by filers" | "Facility size as reported"
    reported: Sourced<string>;                // the amount as its source states it, quoted
    borrower: Sourced<string>;
    tranches: Sourced<string>;                // what the filing cited for the amount says; null = n/d
  };
  quote: {                                    // null = no dealer quote on file
    refersTo: string;
    price: Sourced<number>;
    bid: Sourced<number>; ask: Sourced<number>; mid: Sourced<number>;   // null = n/d
    range: Sourced<string>;                   // range of quotes the report gives, "89–91"; null = n/d
    note: string;
  } | null;
  model: { refersTo: string; price: Sourced<number> };
  vsQuote: string | null;                     // "Model value: 85.9; −4.1 points vs dealer quote (Sep 18)"
  recovery: {
    basis: 'derived' | 'assumption' | 'archive' | 'nd';
    headline: string;                         // "Assumption, not derived: 55c on senior secured construction loans"
    debtClass: string;
    scenario: string;
    assumptions: string[];
    method: Sourced<string>;                  // method page
    value: Sourced<string>;
  };
}

// GET /api/instruments and /api/instruments/:id
export interface InstrumentRow {
  id: string; name: string; type: string; sizeB: Sourced<number>;
  matures: string | null; mardenPrice: Sourced<number>; yieldPct: Sourced<string> | null;
  change1w: Sourced<number>; markGap: Sourced<number> | 'no fund' | 'traded' | 'private' | null;
  chainStatus: ChainStatus | null;           // null = n/d: no status judged for its site or issuer
}
export interface Holder {
  name: string; filing: 'N-PORT' | '10-Q'; filingUrl: string; filingDate: string;
  parMm: number; lastMark: number; gapPts: number; navHitMm: number; shareOfNav: number;
}
/** A lender, arranger or agent of a loan or bond, from a filing (F) or a report (R): `source.value` is its role. */
export interface Lender { name: string; role: string | null; commitmentUsd?: number; source: Sourced<string> }
export interface InstrumentDetail extends InstrumentRow {
  facts: string;
  fundMarks: { low: number; high: number; filing: string; date: string } | null; // null = n/d: no fund filing reports it
  nextMarksPublic: string | null;             // null = n/d: no fund files marks for it
  recovery: Sourced<string>;
  priceHistory: { date: string; price: number }[];
  holders: { count: number; parMm: number; rows: Holder[] };
  stress: { scenario: string; priceAfter: number; change: number; holderNavHitMm: number }[];
  terms: Record<string, Sourced<string>>;
  termsNotDisclosed?: Record<string, string>; // Terms label -> why it is n/d ("not disclosed in filings checked: ...")
  lenders?: Lender[];                         // lender group named in filings (F) and reports (R); [] = none named
  payers: { name: string; share: Sourced<number> }[];
  marketCheck: Record<string, Sourced<string>>;
}

// GET /api/curve
export interface CurveSource { tag: Tag; asOf: string; sourceName: string; sourceUrl?: string; methodVersion?: string }
export interface CurveObservation { date: string; price: number; tag: Tag; kind: string; sourceName: string; sourceUrl?: string }
export interface CurveDot {
  years: number; yieldPct: number; instrumentId: string;
  spreadBp?: number | null;                   // yield minus the Treasury par yield at the same maturity (bp)
  source?: CurveSource;                       // the yield's source: the Marden model price (M) on the run day
  lastObservation?: CurveObservation | null;  // latest observed price behind it (TRACE, fund mark, quote)
  excluded?: string | null;                   // why the dot is left out of its tier's fit (outlier, ...)
}
export interface CurveTier {
  tier: string;
  kind?: 'tier' | 'benchmark';                // 'benchmark': the Treasury par curve (no dots)
  points: CurveDot[];
  fit: { years: number; yieldPct: number; spreadBp?: number }[];
  range?: [number, number] | null;            // maturities the fit covers with dots (solid); dashed beyond
  robustSdBp?: number | null;                 // robust SD of the fitted dots around the curve (bp)
  note?: string | null;                       // plain words: why no curve, or why its spread is flat
  source?: CurveSource;
}

// GET /api/risk-list?week=
export interface RiskRow {
  rank: number; instrumentId: string; name: string; size: string;
  why: Evidence[];                            // rendered on line 2
  price: Sourced<number> | null; change1w: Sourced<number> | null;
  fundMark: number | 'no fund holds it' | 'traded' | 'private' | null; gap: number | null;
  odds: Sourced<number>;                      // 0..1; methodVersion 'odds v0' (uncalibrated) shown in the column header
  move: { kind: 'up' | 'down' | 'same' | 'new'; by?: number };
}
export interface RiskList { week: { start: string; end: string }; rows: RiskRow[]; note: { title: string; paragraphs: { lead: string; text: string; refs: string[] }[] }; earlierCalls: { name: string; flagged: string; moved: string; leadDays: number }[] }

// GET /api/watchlist, /api/alerts
export interface WatchItem {
  id: string; kind: 'site' | 'instrument'; refId: string; name: string; typeLabel: 'Site' | 'Loan' | 'Bond';
  sub: string; status: ChainStatus; cushionMonths: number | null;
  price: Sourced<number> | null; change1w: Sourced<number> | null; markGap: Sourced<number> | string | null; nextCatalyst: string;
}
export interface Alert { id: string; at: string; headline: string; tag: Tag; ruleLabel: string; refUrl: string; read: boolean; emailed: boolean }
export interface AlertSettings {
  types: Record<'cushion' | 'milestone' | 'power' | 'opposition' | 'tenant' | 'price_or_mark' | 'construction_stall', boolean>;
  instantRed: boolean; dailyDigest: boolean; weeklyNote: boolean; priceThresholdPts: 1 | 2 | 3;
}

// GET /api/payers/:slug
export interface Payer {
  slug: string; name: string; role: 'tenant' | 'end_user';
  kpis: { siteDebtB: Sourced<number>; gwLeased: Sourced<string>; rating: Sourced<string>; cds5y: Sourced<string>; notesB: Sourced<number>; noteHolders: Sourced<number> };
  sites: { slug: string; name: string; gw: string; rentStartP50: string | null; outsideDate: Sourced<string>; cushionMonths: number | null; debtB: number; status: ChainStatus }[];
  paidBy: PayerAbove[];
  credit: Record<string, Sourced<string>>;
  stressSummary?: { scenario: string; siteDebtLossB: number; notesLossB: number };
}
/** Who pays the payer: a party one link up (an end user of its sites, or the payer of a lease or compute contract it is owed). */
export interface PayerAbove {
  name: string; slug: string | null;           // slug: its own payer page; null when it is not a payer
  status: ChainStatus;                         // its worst link in the status model's outputs
  ruleCode: string | null; rule: string | null; // the status rule that link tripped ('E1', "End user's plan needs new funding"); null while Holding
  note: Sourced<string>;                       // the statement that tripped it (else its first funding statement)
  endUserOn: string[];                         // slugs of the payer's sites where it is the end user
  owes: string[];                              // what it owes the payer on the obligation map: 'Lease' | 'Compute contract'
  facts: { label: string; fact: Sourced<string>; tripped: boolean }[]; // funding, commitments, rating; newest first
}

// POST /api/stress/run
export interface StressRequest { scenario: 'end_user_funding_miss' | 'tenant_downgrade' | 'power_delay'; params: Record<string, number | string>; severity: 'base' | 'severe' }
export interface StressResult {
  kpis: { exposedB: number; valueLostB: number; valueLostPct: number; tenantNotesLossB?: number; loansBelow95: string[]; fundsHit: number };
  flow: { site: string; debtB: number; priceChange: number; severity: LinkStatus }[];
  rows: { instrumentId: string; name: string; sizeB: Sourced<number>; why: string; ruleCode?: string; rentCover?: { before: Sourced<number>; after: Sourced<number> }; priceNow: number; priceAfter: number; holders: string }[];
  // rentCover is omitted until a filing discloses the rent; the UI hides the column when no row has it
}

// GET/PUT /api/home/layout
export type WidgetKey = 'headline' | 'watchlist_changes' | 'alerts' | 'tightest_cushions' | 'risk_top5' | 'coming_up' | 'mark_gaps' | 'saved_stress' | 'payer_exposure' | 'curve_5y' | 'site_schedule' | 'instrument_price';
export interface HomeLayout { widgets: { key: WidgetKey; params?: Record<string, string> }[] }

// GET /api/stream (Server-Sent Events). One event per change; the client refetches or patches the affected view.
export type StreamEvent =
  | { type: 'alert'; alert: Alert }
  | { type: 'status'; siteSlug: string; chain: ChainLink[] }
  | { type: 'cushion'; siteSlug: string; cushionMonths: Sourced<number> }
  | { type: 'price'; instrumentId: string; price: Sourced<number>; change1w: Sourced<number> }
  | { type: 'mark'; instrumentId: string; holder: string; mark: Sourced<number> }
  | { type: 'pipeline'; source: string; status: 'ok' | 'delayed' | 'down'; at: string }
  | { type: 'campus'; campusId: string }          // a data center fact published; refetch that campus or row
  | { type: 'plant'; plantId: string }            // a power plant fact published
  | { type: 'home'; asOf: string };              // the Home read model was rebuilt; refetch GET /api/home

// ---------- Data centers and on-site power (specs 14–17) ----------

export type DcStatus = 'Planned' | 'Under construction' | 'Operating' | 'Paused' | 'Cancelled';
export type PowerSource = 'Grid' | 'On-site gas' | 'Hybrid' | 'Unknown';
export type DocType = 'Permit' | 'Zoning' | 'Filing' | 'Utility' | 'Court' | 'Press' | 'Imagery';

export interface FinancedLink { siteSlug: string; siteName: string; chainStatus: ChainStatus; debtB: Sourced<number> }
// Marden's note on a campus with no asset-level debt to link (a Sourced value, tag M): built on the owner's balance
// sheet (the owner's filing is the source) or no asset-level debt found (what was searched, and when)
export interface FinancingNote {
  kind: 'owner_funded' | 'not_found';
  owner: string | null;                   // whose balance sheet funds the campus (owner_funded)
  payerId: string | null;                 // the owner's payer page, when Marden has one
  note: string;                           // one line
  searched: string[];                     // where asset-level debt was looked for
}

// GET /api/data-centers  (?state=&county=&status=&developer=&tenant=&utility=&iso=&powerSource=&mwMin=&mwMax=&firstSeenSince=&opposition=&financed=&watchlist=&q=&sort=&page=&format=json|geojson)
export interface DataCenterRow {
  id: string; name: string; aliases: string[];
  county: string; state: string; lat: number; lon: number;
  developer: Sourced<string>; tenant: Sourced<string>;
  status: Sourced<DcStatus>;
  facilityMw: Sourced<number>; itMw: Sourced<number>;
  buildings: { built: number; total: number };
  powerSource: Sourced<PowerSource>; utility: Sourced<string>; iso: Sourced<string>;
  plantId: string | null;
  onlineYear: Sourced<number>;
  financed: FinancedLink | null;          // the primary financed site: the one with the most debt
  financedSites: FinancedLink[];          // every approved financed site on the campus, most debt first (financed = [0])
  financing: Sourced<FinancingNote> | null; // why no debt is linked (owner-funded / not found); null = no note
  lastChange: Evidence;
  firstSeen: string;
}
export interface DataCenterPage { asOf: string; total: number; page: number; pageSize: number; rows: DataCenterRow[] }

// GET /api/data-centers/summary (same filters)
export interface DataCenterSummary {
  campuses: number; buildings: number;
  mwByStatus: { status: DcStatus; mw: number }[];
  newThisWeek: { count: number; newest?: { id: string; name: string } };
  financed: { count: number; debtB: number };   // campuses with a financed site; debt summed once per financed site (a site securing several campuses counts once)
  coverage: { field: string; pct: number }[];   // fill rate per field, for the footer
}

export interface Building {
  id: string; label: string; footprint: GeoJSON.Polygon;
  sqft: Sourced<number>; facilityMw: Sourced<number>; itMw: Sourced<number>;
  status: Sourced<DcStatus>; buildingPermit: Sourced<string>; coDate: Sourced<string>;
  firstVisible: string | null; changeScore?: Sourced<number>;
  imageryStage?: BuildingImageryStage;     // Sentinel-2 construction stage (M); never an operating status
}

// GET /api/data-centers/:id
export interface DataCenterDetail extends DataCenterRow {
  facts: Record<string, Sourced<string | number>>;
  ownerOfRecord: Sourced<string>; parcelIds: string[];
  balancingAuthority: Sourced<string>;
  largeLoad?: { request: Sourced<string>; queuePosition?: Sourced<string> };
  backupGenerators?: { count: Sourced<number>; mw: Sourced<number>; fuel: Sourced<string> };
  keyDates: { label: string; date: Sourced<string> }[];
  imagery: ImageryImage[];
  imageryCampuses?: ImageryCampus[];
  imageryNaip?: Sourced<NaipImage>;
  tabCounts: { buildings: number; records: number; history: number };
}
// GET /api/data-centers/:id/records
export interface DcRecord extends Evidence { type: DocType; agency: string }
// GET /api/data-centers/:id/history
export interface DcHistoryEvent { date: string; field: string; before: string | null; after: string; evidence: Evidence }

export type PlantStatus = 'Permit pending' | 'Permitted' | 'Under construction' | 'Operating' | 'Withdrawn' | 'Denied';
export type PlantTech = 'Simple-cycle turbine' | 'Reciprocating engine' | 'Fuel cell' | 'Combined cycle' | 'Other';

export interface PowerUnit {
  oem: string | null; model: string | null; count: number;   // null = n/d (not in the permit or filing)
  ratingMwEach: Sourced<number>; totalMw: number | null;    // totalMw computed in code; null when count or rating is n/d
  fuel: string | null; status: PlantStatus; onlineYear: number | null; permitRef: string | null;
}
export interface AirPermit {
  agency: string; number: string; type: 'Standard' | 'Minor' | 'Major' | 'Title V' | string;
  status: PlantStatus; filed: string | null; decided: string | null; url: string;
  amendments: Evidence[];
}

// GET /api/power-plants  (?state=&status=&tech=&oem=&model=&operator=&servesId=&agency=&permitType=&gridMode=&financed=&watchlist=&q=&sort=&page=&format=json|geojson)
export interface PowerPlantRow {
  id: string; name: string; county: string | null; state: string; lat: number | null; lon: number | null; // null = n/d (not located yet)
  serves: { id: string; name: string } | null;
  operator: Sourced<string>;
  technology: PlantTech | null; units: { count: number; byModel: { oem: string; model: string; count: number }[] };
  permittedMw: Sourced<number>;                        // sum of unit ratings
  status: Sourced<PlantStatus>;
  airPermit: Pick<AirPermit, 'agency' | 'number' | 'type' | 'status' | 'decided' | 'url'> | null; // null = n/d: no air permit found yet
  fuelSupply: Sourced<string>;
  gridMode: 'Islanded' | 'Grid-tied' | 'Unknown';
  financed: FinancedLink | null;          // the primary financed site: the one with the most debt
  financedSites: FinancedLink[];          // every approved financed site of the campuses it serves, most debt first (financed = [0])
  lastChange: Evidence;
}
export interface PowerPlantPage { asOf: string; total: number; page: number; pageSize: number; rows: PowerPlantRow[] }

// GET /api/power-plants/summary
export interface PowerPlantSummary {
  plants: number; units: number;
  mwByStatus: { status: PlantStatus; mw: number }[];
  mwByTech: { tech: PlantTech; mw: number }[];
  mwByOem: { oem: string; byStatus: { status: PlantStatus; mw: number }[] }[];
  permitsLast30d: number;
}

// GET /api/power-plants/:id
export interface PowerPlantDetail extends PowerPlantRow {
  unitRows: PowerUnit[];
  permit: AirPermit | null;                   // null = n/d: no air permit found yet
  fuel: { pipeline: Sourced<string>; regulator: string; docket: string; inService: { date: string; evidence: Evidence }[] } | null; // null = n/d
  servedCampus: { id: string; name: string; status: DcStatus; facilityMw: Sourced<number>; buildings: number; loadCoverPct: Sourced<number> } | null;
  history: DcHistoryEvent[];
}

// Site detail additions (spec 16): GET /api/sites/:slug/buildings
export interface SiteBuildings { campuses: { id: string; name: string }[]; buildings: Building[]; plant?: PowerPlantRow }

// ---------- Obligation map (spec 18) ----------
export type NodeType = 'AI lab' | 'Cloud' | 'Neocloud' | 'Chipmaker' | 'Developer' | 'SPV' | 'Lender' | 'Guarantor' | 'Power provider' | 'Other';
export type EdgeType = 'Lease' | 'Compute purchase' | 'Chip purchase' | 'Equity investment' | 'Loan or credit facility' | 'Guarantee or backstop' | 'Warrant' | 'Revenue share' | 'Power purchase';
export type EdgeStatus = 'Announced' | 'Signed' | 'Started' | 'Amended' | 'Disputed' | 'Terminated';

export interface ObligationNode { id: string; name: string; type: NodeType; status: ChainStatus | null; owedB: number; owesB: number }
export interface ObligationEdge {
  id: string; from: string; to: string; type: EdgeType; status: EdgeStatus;
  totalB: Sourced<number>; annual: { year: number; amountB: Sourced<number> }[];
  start: Sourced<string>; termYears: Sourced<number>; conditions: Sourced<string>[];
  linkedSites: string[]; linkedInstruments: string[];
  evidence: Evidence[];
}
// GET /api/obligations  (?type=&status=&minB=&company=&format=graph|table)
export interface ObligationGraph { asOf: string; nodes: ObligationNode[]; edges: ObligationEdge[]; kpis: { totalB: number; filedPct: number; changedThisMonth: number } }
// GET /api/obligations/:company
export interface CompanyObligations {
  node: ObligationNode;
  owes: ObligationEdge[]; isOwed: ObligationEdge[];
  owesByYear: { year: number; byCounterparty: { id: string; amountB: number }[] }[];
  concentration: { payerId: string; share: Sourced<number> }[];
  fundingGap: { year: number; committedB: number; fundedB: number; gapB: Sourced<number> }[];
  // Plain-words reason for a list left empty (nobody owes the company; owed contracts with no payment schedule).
  ndReasons?: { concentration?: string; fundingGap?: string };
}
// POST /api/stress/run with scenario 'contagion': params { company, payPct }
export interface ContagionResult {
  hops: { hop: 1 | 2 | 3; companies: { id: string; lossB: number; coverageAfter: number | null }[] }[];
  instruments: { instrumentId: string; name: string; priceNow: number; priceAfter: number; holders: string }[];
  totalAtRiskB: number;
}

// ---------- GPU collateral (spec 19) ----------
export interface GpuPricePoint { date: string; model: string; term: 'on-demand' | '1y' | '3y'; region: string; medianUsdHr: number; p25: number; p75: number; nProviders: number }
export interface GpuProviderPrice { provider: string; model: string; term: string; region: string; usdHr: Sourced<number>; capturedAt: string; pageUrl: string }
// GET /api/gpus, GET /api/gpus/:model
export interface GpuModelDetail {
  model: string; launch: Sourced<string>; newPrice: Sourced<number>;
  index: GpuPricePoint[]; providers: GpuProviderPrice[];
  resale: { date: string; usd: number; source: string; url: string }[];
  valueByAgeYears: { age: number; usd: Sourced<number> }[];
  assumptions: { utilization: number; annualDecline: number; haircut: number; usefulLifeYears: Sourced<number>; methodVersion: string };
  facilities: { instrumentId: string; name: string; units: Sourced<number> }[];
}
export interface GpuCollateral {
  instrumentId: string; model: string; units: Sourced<number>;
  valueTodayB: Sourced<number>; balanceB: Sourced<number>;
  ltvToday: Sourced<number>; ltvByQuarter: { quarter: string; ltv: number }[];
  recoveryIfCustomerWalks: Sourced<string>;
  deployment?: GpuDeployment;
}
// Delivered vs energized (spec 19). GET /api/gpus/deployment
export type DeploymentStatus = 'Earning' | 'Partly idle' | 'Idle' | 'n/d';
export interface GpuDeployment {
  scope: 'facility' | 'site'; id: string; name: string; model: string | null;
  unitsFinanced: Sourced<number>; unitsDelivered: Sourced<number>; unitsEnergized: Sourced<number>;
  idleUnits: Sourced<number>; idleShare: Sourced<number>; monthsIdle: Sourced<number>;
  idleInterestM: Sourced<number>; valueLostWhileIdleM: Sourced<number>;
  ltvToday: Sourced<number>; ltvIfOnSchedule: Sourced<number>;
  status: DeploymentStatus; missing: string[]; evidence: Evidence[];
  assumptions: { kwPerUnit: Sourced<number>; pue: number; couponPct: Sourced<number>; methodVersion: string };
}
export interface CompanyCip { company: string; quarter: string; cipB: Sourced<number>; ppeInServiceB: Sourced<number>; cipShare: number | null; changeQoQB: number | null }
export interface GpuDeploymentPage {
  asOf: string; facilities: GpuDeployment[]; companies: CompanyCip[];
  totals: { deliveredUnits: Sourced<number>; energizedUnits: Sourced<number>; idleShare: Sourced<number> };
}

// ---------- Power plant debt (spec 20) ----------
export interface UccLien { securedParty: string; debtor: string; state: string; fileNumber: string; filed: string; collateral: string; url: string }
export interface PlantDebt {
  owner: Sourced<string>;
  liens: UccLien[];
  mortgages: { lender: string; amount: Sourced<number>; recorded: string; url: string }[];
  financing: { lender: string; type: 'Loan' | 'Lease' | 'Vendor financing' | string; amount: Sourced<number>; evidence: Evidence }[];
  powerBuyer: { name: string; term: Sourced<string>; edgeId?: string } | null;
  equipmentOrders: { oem: string; units: number; delivery: Sourced<string> }[];
}

// ---------- Payer capacity and downgrade risk (spec 24) ----------
export interface RatingTrigger { metric: string; threshold: string; evidence: Evidence }
export interface RatingState { agency: 'S&P' | "Moody's" | 'Fitch'; rating: Sourced<string>; outlook: Sourced<string>; lastAction: Sourced<string>; triggers: RatingTrigger[] }
export interface PayerCapacityYear {
  fiscalYear: string; ocfB: Sourced<number>; capexB: Sourced<number>;
  leaseCommencingB: Sourced<number>; computeCommitmentsB: Sourced<number>; fundingGapB: Sourced<number>;
  leaseAdjLeverage: Sourced<number>;
}
export type DowngradeRisk = 'Low' | 'Watch' | 'High' | 'n/d';
export interface ExposedInstrument {
  instrumentId: string; name: string; link: 'Lease' | 'Compute contract' | 'Guarantee' | 'Residual value guarantee' | 'GPU contract backlog' | string;
  amountB: Sourced<number>; ratingTrigger: Sourced<string>; onTrip: Sourced<string>; holdersCount: number;
}
export interface PayerCapacity {
  slug: string; name: string; kind: 'Hyperscaler' | 'Cloud' | 'AI lab' | 'Neocloud' | string;
  ratings: RatingState[]; spreadBp: Sourced<number>;
  leaseAdjLeverageToday: Sourced<number>; leaseAdjLeveragePeak: Sourced<number>;
  triggerHeadroomTurns: Sourced<number>; breachYear: Sourced<string>;
  fundingGap3yB: Sourced<number>; aiRevenueCoverage: Sourced<number>;
  topCounterparty: { name: string; share: Sourced<number> } | null;
  downgradeRisk: Sourced<DowngradeRisk>;
  years: PayerCapacityYear[]; exposed: ExposedInstrument[];
  // Plain-words reasons for an n/d projected leverage path, 3-year funding gap or trigger headroom
  // (the inputs not disclosed, by name); omitted until the capacity model writes them.
  ndReasons?: { leveragePath?: string; fundingGap?: string; triggerHeadroom?: string };
}
// GET /api/payers
export interface PayersPage { asOf: string; rows: PayerCapacity[] }

// ---------- Off-balance-sheet lease map (spec 25) ----------
export interface LeaseRecord {
  id: string;
  lesseeId: string;                           // entity id of the lessee ('unnamed-tenants' when no source names it)
  lessee: string;                             // display name; "Tenant not disclosed" when no source names the tenant
  tenantNote: Sourced<string>;                // the sources' own description of an unnamed tenant ("AA- or higher rated company")
  lessor: Sourced<string>;
  siteSlug: string | null; siteName: string | null;   // the single linked site (null when several or none)
  sites: { slug: string; name: string }[];    // every linked site, with its display name
  campusId: string | null;
  mw: Sourced<number>;                        // contracted capacity of the lease (MW, critical IT unless the source says gross)
  kind: 'Operating' | 'Finance' | 'Build-to-suit' | 'Compute contract' | string;
  status: 'Signed, not started' | 'Started' | 'Announced' | 'Amended' | 'Terminated' | string;
  totalB: Sourced<number>; annualB: Sourced<number>; commencement: Sourced<string>; termYears: Sourced<number>;
  attribution: 'Filed' | 'Reported' | 'Inferred'; confidence: 'High' | 'Medium' | 'Low' | null;  // confidence set for Inferred
  turnsAdded: Sourced<number>; headroomBeforeTurns: Sourced<number>; triggerLease: boolean;
  lesseeBand: Sourced<DowngradeRisk>; priorityScore: Sourced<number>;
  dependentDebt: { instrumentId: string; name: string; amountB: Sourced<number>; ratingLinked: boolean; holdersCount: number }[];
  evidence: Evidence[];
}
export interface LesseeReconciliation {
  lessee: string; lesseeId: string | null; filing: { form: string; period: string; url: string };
  commencement: string | null; terms: string | null;  // the filing's commencement window and term range, as stated
  disclosedNotCommencedB: Sourced<number>;
  namedLeases: number; namedTotalB: number;            // the lessee's named leases with a stated total (uncapped sum)
  filedOrReportedB: number; inferredB: number; attributedB: number; unattributedB: number; attributedShare: number;
  note: string | null;                                 // why attribution was capped, when named totals exceed the filing
}
// GET /api/leases
export interface LeaseMapPage {
  asOf: string;
  totals: { disclosedB: number; attributedB: number; unattributedB: number; attributedShare: number; triggerLeases: number; debtExposedB: number };
  reconciliation: LesseeReconciliation[]; leases: LeaseRecord[];
}

// ---------- Project file: project-finance-level 360 view (spec 26) ----------
export type ChecklistArea = 'Parties' | 'Lease' | 'Structure' | 'Budget' | 'Draws' | 'Construction' | 'Contractor' | 'Equipment' | 'Power' | 'Permits' | 'Opposition' | 'Insurance' | 'Independent engineer';
export interface ChecklistItem { area: ChecklistArea; item: string; state: 'sourced' | 'n/d' | 'n/p'; value: Sourced<string>; researchNote?: string }
export interface ProjectParty { role: string; name: string; entityId: string | null; parent: string | null; evidence: Evidence }
export interface Tranche { name: string; type: string; amountB: Sourced<number>; drawnB: Sourced<number>; seniority: string; pricing: Sourced<string>; maturity: Sourced<string>; holdersCount: number; instrumentId: string | null }
export interface ProjectStructure {
  tranches: Tranche[]; security: Sourced<string>; completionGuarantee: Sourced<string>; sponsorSupport: Sourced<string>;
  reserves: Sourced<string>; sizingCovenants: Sourced<string>; waterfall: Sourced<string>; hedges: Sourced<string>;
  ratingTriggers: Sourced<string>; intercreditor: Sourced<string>;
}
export interface MechanicsLien { claimant: string; amountM: Sourced<number>; recorded: string; released: string | null; url: string }
export interface ProjectConstruction {
  declaredCostB: Sourced<number>; announcedCostB: Sourced<number>; costPerMwM: Sourced<number>; peerCostPerMwM: Sourced<number>;
  costToCompleteGapB: Sourced<number>; fundingGapB: Sourced<number>;
  drawsVsProgress: { date: string; drawnShare: number | null; builtShare: number | null }[]; drawPaceFlag: boolean | null; // null = n/d: no draw or progress series to compare
  generalContractor: Sourced<string>; liens: MechanicsLien[]; contractorStress: Sourced<'None' | 'Watch' | 'High'>;
  equipment: { item: string; oem: string; units: number | null; neededBy: string | null; delivery: Sourced<string>; late: boolean | null }[]; // late null = n/d: no need-by or delivery date
}
// GET /api/sites/:slug/project-file
export interface ProjectFile {
  siteSlug: string; completeness: number; checklist: ChecklistItem[];
  parties: ProjectParty[]; structure: ProjectStructure; construction: ProjectConstruction;
}

// ---------- Capacity and GPU estimate (spec 27) ----------
export interface Range3 { low: number | null; base: number | null; high: number | null }
export interface CapacityMethod {
  method: 'Filed' | 'Utility load' | 'Prime generation' | 'Backup generators' | 'Cooling' | 'Footprint' | 'Disclosed GPU count' | 'Financed GPUs';
  facilityMw: Range3; assumptions: string; evidence: Evidence[];
}
export interface CapacityEstimate {
  scope: 'building' | 'campus'; id: string; name: string;
  facilityMw: Range3; itMw: Range3; gpus: Range3; gpuValueB: Range3;
  pue: Sourced<number>; gpuGeneration: Sourced<string>; kwPerGpu: Sourced<number>; gpuShareOfIt: Sourced<number>;
  confidence: 'High' | 'Medium' | 'Low'; methods: CapacityMethod[]; disagreement: boolean;
  replacedByFiling: { field: 'facilityMw' | 'itMw' | 'gpus'; value: Sourced<number> }[];
  methodVersion: string;
}
