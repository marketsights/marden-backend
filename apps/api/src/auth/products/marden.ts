// Product 'marden': the V1 app, every current screen and endpoint. Role -> permission matrix for a
// member's role on this product (member_product_role.role). Org administration (team, product
// assignments, billing) is not here: it follows the org-level membership role (permissions.ts).
import { PRODUCT_READ, PRODUCT_USE, type ProductPermission } from '../permissions.js';
import type { ProductMatrix } from './types.js';

/** Viewer: read only plus their own preferences (layout, alert settings). */
const VIEWER: readonly ProductPermission[] = [...PRODUCT_READ, 'prefs:write'];
/** Member: everything in the plan: watchlists, alerts, stress runs, exports. */
const MEMBER: readonly ProductPermission[] = [...VIEWER, ...PRODUCT_USE];

/** Product admins and owners use the product like members; no product-level setting exists yet. */
export const MARDEN: ProductMatrix = {
  viewer: VIEWER,
  member: MEMBER,
  admin: MEMBER,
  owner: MEMBER,
};
