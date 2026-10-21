// A product's role -> permission matrix. Products may only grant product permissions; org
// administration and staff powers are never part of a product (the type enforces it).
import type { Role } from '../../contracts/app.js';
import type { ProductPermission } from '../permissions.js';

export type ProductMatrix = Readonly<Record<Role, readonly ProductPermission[]>>;

/** Product id (the product table's id) -> its matrix. A product without a matrix grants nothing. */
export type ProductRegistry = Readonly<Record<string, ProductMatrix>>;
