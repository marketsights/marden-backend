// On-site power table columns from spec 20 ("add columns Owner, Financed by, Debt ($, tag), Power
// buyer"): each plant row carries the roll-up of its plant_debt document (GET /api/power-plants/:id/debt),
// joined at read time so the row, the page, the /row refresh and the Excel export agree and stay as
// fresh as the debt document. Plant debt is n/d, never zero, when no record states an amount.
import type { PlantDebt, Sourced } from '../contracts/types.js';

/** Method of the plant-debt sum (a Marden roll-up of stated amounts, tag M). */
export const PLANT_DEBT_SUM_METHOD = 'plant-debt-sum@0.1';

export interface PlantDebtColumns {
  owner: Sourced<string>;
  /** Lien holders, lenders and mortgagees, each once, in the order the debt document lists them. */
  financedBy: string[];
  plantDebt: Sourced<number>;
  /** The buyer named on the plant's Power purchase edge (spec 18), or null when none is on record. */
  powerBuyer: string | null;
}

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);

export function financedBy(debt: PlantDebt): string[] {
  const seen = new Map<string, string>();
  for (const raw of [...debt.liens.map((l) => l.securedParty), ...debt.financing.map((f) => f.lender), ...debt.mortgages.map((m) => m.lender)]) {
    const name = raw?.trim();
    if (name && !seen.has(name.toLowerCase())) seen.set(name.toLowerCase(), name);
  }
  return [...seen.values()];
}

/** Sum of the financing and mortgage amounts a record states (USD), tag M; n/d when none is stated. */
export function plantDebtTotal(debt: PlantDebt, asOf: string): Sourced<number> {
  const amounts = [...debt.financing.map((f) => f.amount), ...debt.mortgages.map((m) => m.amount)];
  const stated = amounts.filter((a): a is Sourced<number> => !!a && isNum(a.value));
  const unstated = amounts.length - stated.length;
  if (stated.length === 0) {
    return { value: null, tag: 'M', asOf, methodVersion: PLANT_DEBT_SUM_METHOD, sourceName: 'No loan, lease or mortgage amount stated in any record' };
  }
  return {
    value: stated.reduce((sum, a) => sum + (a.value ?? 0), 0),
    tag: 'M',
    asOf: stated.map((a) => a.asOf).sort().pop() ?? asOf,
    methodVersion: PLANT_DEBT_SUM_METHOD,
    sourceName: `Sum of ${stated.length} stated ${stated.length === 1 ? 'amount' : 'amounts'}${unstated > 0 ? `; ${unstated} more not stated` : ''}`,
  };
}

const NOTHING_FILED: PlantDebt = {
  owner: { value: null, tag: 'F', asOf: '' }, liens: [], mortgages: [], financing: [], powerBuyer: null, equipmentOrders: [],
};

/** The four columns for one plant; all n/d when its debt document is not built yet. */
export function plantDebtColumns(debt: PlantDebt | null, asOf: string): PlantDebtColumns {
  if (!debt) return { owner: { value: null, tag: 'F', asOf }, financedBy: [], plantDebt: plantDebtTotal(NOTHING_FILED, asOf), powerBuyer: null };
  return {
    owner: debt.owner,
    financedBy: financedBy(debt),
    plantDebt: plantDebtTotal(debt, debt.owner?.asOf ?? asOf),
    powerBuyer: debt.powerBuyer?.name ?? null,
  };
}

/** A plant_row document with its spec 20 columns. `asOf` dates the n/d values (the row's own date). */
export function withPlantDebt<T extends object>(row: T, debt: PlantDebt | null | undefined): T & PlantDebtColumns {
  const r = row as { permittedMw?: { asOf?: string }; status?: { asOf?: string } };
  const asOf = r.status?.asOf ?? r.permittedMw?.asOf ?? new Date().toISOString().slice(0, 10);
  return { ...row, ...plantDebtColumns(debt ?? null, asOf) };
}
