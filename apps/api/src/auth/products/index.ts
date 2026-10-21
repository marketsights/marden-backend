// The product catalog in code (docs/access-control.md "Adding a product"): one matrix per product id
// in the product table. A product row with no entry here grants nothing (fail closed), and staff
// cannot give it to a firm.
import { MARDEN } from './marden.js';
import type { ProductMatrix, ProductRegistry } from './types.js';

export type { ProductMatrix, ProductRegistry } from './types.js';

export const PRODUCTS: ProductRegistry = {
  marden: MARDEN,
};

/** The registry's own matrix for a product id; null for any other id (`constructor`, `toString` too). */
export function matrixFor(products: ProductRegistry, productId: string): ProductMatrix | null {
  return Object.hasOwn(products, productId) ? (products[productId] ?? null) : null;
}
