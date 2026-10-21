// Team rules (spec 13, 23, docs/access-control.md): only owners and admins invite, remove or change
// roles; admins can't make owners, and can't change or remove an owner's org role or product roles
// (owners can); the last owner can't be removed or demoted. Roles are Marden's, never Clerk's.
import type { Role } from '../contracts/app.js';
import { conflict, forbidden } from '../errors.js';

const MANAGERS: readonly Role[] = ['owner', 'admin'];

function assertManager(actor: Role): void {
  if (!MANAGERS.includes(actor)) throw forbidden('role_lacks_permission');
}

export function checkInvite(actor: Role, role: Role): void {
  assertManager(actor);
  if (actor !== 'owner' && role === 'owner') throw forbidden('admins_cannot_manage_owners');
}

export function checkRoleChange(actor: Role, target: Role, next: Role, owners: number): void {
  assertManager(actor);
  if (actor !== 'owner' && (target === 'owner' || next === 'owner')) throw forbidden('admins_cannot_manage_owners');
  if (target === 'owner' && next !== 'owner' && owners <= 1) throw conflict('last_owner');
}

export function checkRemoval(actor: Role, target: Role, owners: number): void {
  assertManager(actor);
  if (actor !== 'owner' && target === 'owner') throw forbidden('admins_cannot_manage_owners');
  if (target === 'owner' && owners <= 1) throw conflict('last_owner');
}

/** Setting or removing a member's role on one of the firm's products; `target` is their org role. */
export function checkProductRoleChange(actor: Role, target: Role): void {
  assertManager(actor);
  if (actor !== 'owner' && target === 'owner') throw forbidden('admins_cannot_manage_owners');
}
