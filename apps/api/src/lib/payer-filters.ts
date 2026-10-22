// Payers index (spec 24) and Payer page (spec 08): URL filters on the payers document and the rows
// the Excel exports carry. Filtering and projection only: values are copied as served, never
// recomputed (the one sum is the exposed-instrument total the index shows next to its count).
import type { Payer, PayerCapacity, SiteSummary, Sourced } from '../contracts/types.js';
import { badRequest } from '../errors.js';
import { list, type Query } from './params.js';

/** Downgrade risk bands as the index sorts them; 'n/d' = the model has no band for this payer. */
export const RISK_BANDS = ['High', 'Watch', 'Low', 'n/d'] as const;

const lowerSet = (values: string[]): Set<string> => new Set(values.map((v) => v.toLowerCase()));

/** The band a row shows: its model value, or 'n/d' while the model has none. */
export const riskBand = (row: PayerCapacity): string => row.downgradeRisk?.value ?? 'n/d';

/** Payers index: kind (Hyperscaler, Cloud, AI lab, Neocloud) and risk (High, Watch, Low, n/d); comma lists match any. */
export function filterPayers(rows: PayerCapacity[], q: Query): PayerCapacity[] {
  const kind = lowerSet(list(q, 'kind'));
  const risk = lowerSet(list(q, 'risk'));
  for (const band of risk) {
    if (!RISK_BANDS.some((b) => b.toLowerCase() === band)) throw badRequest(`risk must be one of ${RISK_BANDS.join(', ')}`);
  }
  return rows.filter(
    (r) => (kind.size === 0 || kind.has(String(r.kind).toLowerCase())) && (risk.size === 0 || risk.has(riskBand(r).toLowerCase())),
  );
}

const AGENCY_COLUMNS = [['S&P', 'sp'], ["Moody's", 'moodys'], ['Fitch', 'fitch']] as const;

/** spRating, spOutlook, moodysRating, ... : each agency's rating and outlook, null when it has none. */
function ratingColumns(row: PayerCapacity): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [agency, prefix] of AGENCY_COLUMNS) {
    const state = row.ratings.find((r) => r.agency === agency);
    out[`${prefix}Rating`] = state?.rating ?? null;
    out[`${prefix}Outlook`] = state?.outlook ?? null;
  }
  return out;
}

/** Exposed instruments' total ($B) over the amounts that are known; null when none is. */
export function exposedTotalB(row: PayerCapacity): number | null {
  const known = row.exposed.map((e) => e.amountB?.value).filter((v): v is number => typeof v === 'number');
  return known.length === 0 ? null : Math.round(known.reduce((a, b) => a + b, 0) * 1e6) / 1e6;
}

/** One export row per payer: the index's columns, Sourced values kept so the sources sheet lists them. */
export function payerIndexRow(row: PayerCapacity): Record<string, unknown> {
  return {
    slug: row.slug,
    name: row.name,
    kind: row.kind,
    ...ratingColumns(row),
    leaseAdjLeverageToday: row.leaseAdjLeverageToday,
    leaseAdjLeveragePeak: row.leaseAdjLeveragePeak,
    triggerHeadroomTurns: row.triggerHeadroomTurns,
    breachYear: row.breachYear,
    fundingGap3yB: row.fundingGap3yB,
    aiRevenueCoverage: row.aiRevenueCoverage,
    topCounterparty: row.topCounterparty?.name ?? null,
    topCounterpartyShare: row.topCounterparty?.share ?? null,
    downgradeRisk: row.downgradeRisk,
    instrumentsExposed: row.exposed.length,
    exposedB: exposedTotalB(row),
    holderPositions: row.exposed.reduce((n, e) => n + (e.holdersCount ?? 0), 0),
  };
}

/** A Sourced value for a payer-site field the payer document carries plain, sourced like its KPI total. */
const likeKpi = <T>(value: T | null, kpi: Sourced<unknown>): Sourced<T> => ({ ...kpi, value });

/** The payer document's own GW and debt for a site, sourced like its KPI totals; its 0 and "n/d" read as n/d. */
function payerDocValues(payer: Payer, s: Payer['sites'][number]): { gw: Sourced<string>; debtB: Sourced<number> } {
  return {
    gw: likeKpi(s.gw && s.gw !== 'n/d' ? s.gw : null, payer.kpis.gwLeased),
    debtB: likeKpi(s.debtB ? s.debtB : null, payer.kpis.siteDebtB),
  };
}

/**
 * The Payer page's sites table: one row per site the payer is on. GW, cushion, debt, tenant and
 * status come from the site's own row in the `sites` document (Sourced, n/d kept as n/d); when that
 * document is missing the payer document's values are used, sourced like its KPI totals. That
 * document writes 0 and "n/d" where nothing is disclosed: both are exported as n/d, never 0.
 */
export function payerSiteRows(payer: Payer, sites: SiteSummary[] | null): Record<string, unknown>[] {
  const bySlug = new Map((sites ?? []).map((s) => [s.slug, s]));
  return payer.sites.map((s) => {
    const site = bySlug.get(s.slug);
    const own = payerDocValues(payer, s);
    return {
      slug: s.slug,
      site: s.name,
      tenant: site?.tenant ?? null,
      endUser: site?.endUser ?? null,
      gw: site?.gw ?? own.gw,
      rentStartP50: site?.rentStartP50 ?? s.rentStartP50,
      outsideDate: s.outsideDate,
      cushionMonths: site ? site.cushionMonths : s.cushionMonths,
      debtB: site?.debtB ?? own.debtB,
      status: site?.status ?? s.status,
    };
  });
}
