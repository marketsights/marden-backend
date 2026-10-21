// Clerk Backend API calls for team changes. Clerk holds identity, organization membership and
// invitations; roles are Marden's (docs/access-control.md). The production Clerk instance is shared
// with another product, so Marden never creates, assigns or changes a Clerk role: every invitation
// carries Clerk's built-in basic member role, and there is deliberately no role-change call here.
// Absent without CLERK_SECRET_KEY.
import { createClerkClient } from '@clerk/backend';

/** Clerk's built-in basic member role: the only Clerk role Marden ever sends (on invitations). */
export const CLERK_MEMBER_ROLE = 'org:member';

export interface ClerkAdmin {
  /** A Clerk organization invitation with the basic member role; the Marden role is kept in org_invitation. */
  invite(orgId: string, email: string, inviterUserId: string): Promise<{ id: string; expiresAt: number | null }>;
  /** Ends the Clerk organization membership (membership existence is Clerk's). */
  removeMember(orgId: string, userId: string): Promise<void>;
  mfaEnabled(userId: string): Promise<boolean | null>;
}

export function createClerkAdmin(secretKey: string | null): ClerkAdmin | null {
  if (!secretKey) return null;
  const clerk = createClerkClient({ secretKey });
  return {
    async invite(orgId, email, inviterUserId) {
      const inv = await clerk.organizations.createOrganizationInvitation({
        organizationId: orgId, emailAddress: email, role: CLERK_MEMBER_ROLE, inviterUserId,
      });
      return { id: inv.id, expiresAt: typeof inv.expiresAt === 'number' ? inv.expiresAt : null };
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
