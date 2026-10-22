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
  imagery: { month: string; url: string; caption: string; changeScore?: number }[];
  documents: Evidence[];
}
export interface SiteDetail extends SiteSummary {
  facts: string;                              // "2.2–2.45 GW · ~$18B construction loan"
  trackingNames: string[];
  tabCounts: { evidence: number; opposition: number; events: number };
  debtExposed: { instrumentId: string; name: string; sizeB: Sourced<number>; dealerQuote?: Sourced<string>; mardenPrice: Sourced<number>; recovery: Sourced<string>; holdersNote: string };
  materialEvents: (Evidence & { ruleCode?: string; severity: LinkStatus })[];
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
export interface InstrumentDetail extends InstrumentRow {
  facts: string;
  fundMarks: { low: number; high: number; filing: string; date: string } | null; // null = n/d: no fund filing reports it
  nextMarksPublic: string | null;             // null = n/d: no fund files marks for it
  recovery: Sourced<string>;
  priceHistory: { date: string; price: number }[];
  holders: { count: number; parMm: number; rows: Holder[] };
  stress: { scenario: string; priceAfter: number; change: number; holderNavHitMm: number }[];
  terms: Record<string, Sourced<string>>;
  payers: { name: string; share: Sourced<number> }[];
  marketCheck: Record<string, Sourced<string>>;
}

// GET /api/curve
export interface CurveTier { tier: string; points: { years: number; yieldPct: number; instrumentId: string }[]; fit: { years: number; yieldPct: number }[] }

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
  financed: FinancedLink | null;
  lastChange: Evidence;
  firstSeen: string;
}
export interface DataCenterPage { asOf: string; total: number; page: number; pageSize: number; rows: DataCenterRow[] }

// GET /api/data-centers/summary (same filters)
export interface DataCenterSummary {
  campuses: number; buildings: number;
  mwByStatus: { status: DcStatus; mw: number }[];
  newThisWeek: { count: number; newest?: { id: string; name: string } };
  financed: { count: number; debtB: number };
  coverage: { field: string; pct: number }[];   // fill rate per field, for the footer
}

export interface Building {
  id: string; label: string; footprint: GeoJSON.Polygon;
  sqft: Sourced<number>; facilityMw: Sourced<number>; itMw: Sourced<number>;
  status: Sourced<DcStatus>; buildingPermit: Sourced<string>; coDate: Sourced<string>;
  firstVisible: string | null; changeScore?: Sourced<number>;
}

// GET /api/data-centers/:id
export interface DataCenterDetail extends DataCenterRow {
  facts: Record<string, Sourced<string | number>>;
  ownerOfRecord: Sourced<string>; parcelIds: string[];
  balancingAuthority: Sourced<string>;
  largeLoad?: { request: Sourced<string>; queuePosition?: Sourced<string> };
  backupGenerators?: { count: Sourced<number>; mw: Sourced<number>; fuel: Sourced<string> };
  keyDates: { label: string; date: Sourced<string> }[];
  imagery: { month: string; url: string }[];
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
  financed: FinancedLink | null;
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
  id: string; lessee: string; lessor: Sourced<string>; siteSlug: string | null; campusId: string | null;
  kind: 'Operating' | 'Finance' | 'Build-to-suit' | 'Compute contract' | string;
  status: 'Signed, not commenced' | 'Commenced' | 'Announced' | 'Amended' | 'Terminated' | string;
  totalB: Sourced<number>; annualB: Sourced<number>; commencement: Sourced<string>; termYears: Sourced<number>;
  attribution: 'Filed' | 'Reported' | 'Inferred'; confidence: 'High' | 'Medium' | 'Low' | null;  // confidence set for Inferred
  turnsAdded: Sourced<number>; headroomBeforeTurns: Sourced<number>; triggerLease: boolean;
  lesseeBand: Sourced<DowngradeRisk>; priorityScore: Sourced<number>;
  dependentDebt: { instrumentId: string; name: string; amountB: Sourced<number>; ratingLinked: boolean; holdersCount: number }[];
  evidence: Evidence[];
}
export interface LesseeReconciliation {
  lessee: string; filing: { form: string; period: string; url: string };
  disclosedNotCommencedB: Sourced<number>; filedOrReportedB: number; inferredB: number; attributedB: number; unattributedB: number; attributedShare: number;
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
