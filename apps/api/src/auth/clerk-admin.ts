// Clerk Backend API calls for team changes (spec 23: invite, remove and role changes go through Clerk
// from our API, then sync back by webhook). Absent without CLERK_SECRET_KEY.
import { createClerkClient } from '@clerk/backend';
import type { Role } from '../contracts/app.js';
import { clerkRoleFor } from './permissions.js';

export interface ClerkAdmin {
  invite(orgId: string, email: string, role: Role, inviterUserId: string): Promise<{ id: string }>;
  updateRole(orgId: string, userId: string, role: Role): Promise<void>;
  removeMember(orgId: string, userId: string): Promise<void>;
  mfaEnabled(userId: string): Promise<boolean | null>;
}

export function createClerkAdmin(secretKey: string | null): ClerkAdmin | null {
  if (!secretKey) return null;
  const clerk = createClerkClient({ secretKey });
  return {
    async invite(orgId, email, role, inviterUserId) {
      const inv = await clerk.organizations.createOrganizationInvitation({
        organizationId: orgId, emailAddress: email, role: clerkRoleFor(role), inviterUserId,
      });
      return { id: inv.id };
    },
    async updateRole(orgId, userId, role) {
      await clerk.organizations.updateOrganizationMembership({ organizationId: orgId, userId, role: clerkRoleFor(role) });
    },
    async removeMember(orgId, userId) {
      await clerk.organizations.deleteOrganizationMembership({ organizationId: orgId, userId });
    },
    async mfaEnabled(userId) {
      try {
        return (await clerk.users.getUser(userId)).twoFactorEnabled;
      } catch {
        return null;
      }
    },
  };
}
