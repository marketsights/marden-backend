// The words a user sees for chain and link statuses, for export cells (display only). The contract
// values stay as they are ('Broken' | 'Strained' | 'Holding' | 'No build'; red | amber | green | none);
// docs/methods/status-rules-v0.1.md "Display words", services/marden/models/status_labels.py.
import type { ChainStatus, LinkStatus } from '../contracts/types.js';

export const CHAIN_STATUS_LABEL: Readonly<Record<ChainStatus, string>> = {
  Broken: 'At risk',
  Strained: 'Watch',
  Holding: 'No flags',
  'No build': 'Not started',
};

export const LINK_STATUS_LABEL: Readonly<Record<LinkStatus, string>> = {
  red: 'Red flag',
  amber: 'Warning',
  green: 'No flags',
  none: 'No data',
};

/** Export columns that hold a chain status. */
const CHAIN_STATUS_FIELDS = new Set(['status', 'chainStatus']);

/** 'At risk' for a chain status in a chain status column; any other value unchanged. */
export function chainStatusCell(field: string, value: unknown): unknown {
  if (!CHAIN_STATUS_FIELDS.has(field) || typeof value !== 'string') return value;
  return Object.hasOwn(CHAIN_STATUS_LABEL, value) ? CHAIN_STATUS_LABEL[value as ChainStatus] : value;
}

/** 'Red flag' for a link status; any other text unchanged. */
export const linkStatusWord = (value: string): string =>
  Object.hasOwn(LINK_STATUS_LABEL, value) ? LINK_STATUS_LABEL[value as LinkStatus] : value;
