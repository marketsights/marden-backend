// The Clerk Backend API wrapper (src/auth/clerk-admin.ts) with @clerk/backend mocked: invitations use
// Clerk's built-in basic member role only, removals end the membership, and no call ever creates,
// assigns or changes a Clerk role (the production instance is shared; roles are Marden's).
import { beforeEach, describe, expect, it, vi } from 'vitest';

const recorded = vi.hoisted(() => ({ calls: [] as { api: string; method: string; args: unknown[] }[] }));

vi.mock('@clerk/backend', () => {
  /** Every method of every Backend API namespace records its call; the ones used answer like Clerk. */
  const namespace = (api: string) => new Proxy({}, {
    get: (_target, method) => {
      if (typeof method !== 'string' || method === 'then') return undefined;
      return (...args: unknown[]) => {
        recorded.calls.push({ api, method, args });
        if (method === 'createOrganizationInvitation') return Promise.resolve({ id: 'orginv_1', expiresAt: 1_800_000_000_000 });
        if (method === 'getUser') return Promise.resolve({ twoFactorEnabled: true });
        return Promise.resolve({});
      };
    },
  });
  return {
    createClerkClient: () => new Proxy({}, {
      get: (_target, api) => (typeof api === 'string' && api !== 'then' ? namespace(api) : undefined),
    }),
  };
});

const { CLERK_MEMBER_ROLE, createClerkAdmin } = await import('../src/auth/clerk-admin.js');

describe('Clerk admin wrapper', () => {
  beforeEach(() => {
    recorded.calls.length = 0;
  });

  it('is absent without a secret key', () => {
    expect(createClerkAdmin(null)).toBeNull();
  });

  it("invites with Clerk's basic member role only", async () => {
    const admin = createClerkAdmin('sk_test_unit')!;
    expect(await admin.invite('org_1', 'pm@fund.test', 'user_1')).toEqual({ id: 'orginv_1', expiresAt: 1_800_000_000_000 });
    expect(CLERK_MEMBER_ROLE).toBe('org:member');
    expect(recorded.calls).toEqual([{
      api: 'organizations', method: 'createOrganizationInvitation',
      args: [{ organizationId: 'org_1', emailAddress: 'pm@fund.test', role: 'org:member', inviterUserId: 'user_1' }],
    }]);
  });

  it('removes a membership and reads MFA, and never calls a role API', async () => {
    const admin = createClerkAdmin('sk_test_unit')!;
    await admin.removeMember('org_1', 'user_2');
    expect(await admin.mfaEnabled('user_2')).toBe(true);
    await admin.invite('org_1', 'a@fund.test', 'user_1');
    expect(recorded.calls.map((c) => `${c.api}.${c.method}`)).toEqual([
      'organizations.deleteOrganizationMembership', 'users.getUser', 'organizations.createOrganizationInvitation',
    ]);
    const roleCalls = recorded.calls.filter((c) =>
      /role|updateOrganizationMembership\b|updateOrganizationMembershipMetadata/i.test(c.method) || /role/i.test(c.api));
    expect(roleCalls).toEqual([]);
    const sentRoles = recorded.calls.flatMap((c) => c.args).map((a) => (a as { role?: unknown }).role).filter((r) => r !== undefined);
    expect(sentRoles).toEqual(['org:member']);
    // The interface itself has no role call.
    expect(Object.keys(admin).sort()).toEqual(['invite', 'mfaEnabled', 'removeMember']);
  });
});
