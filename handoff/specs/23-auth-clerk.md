# 23 · Authentication and access (Clerk)

**Decision:** Clerk replaces WorkOS everywhere. Clerk owns identity: users, sessions, MFA, organizations, memberships, invitations and enterprise SSO. Marden owns authorization: `packages/contracts/permissions.ts` decides what each role and plan may do, and the API enforces it on every request.

> **Update 2026-10-21 (owner decision): roles are Marden's, not Clerk's.** The marden.ai production Clerk instance is shared with another product. Marden creates no Clerk roles, never assigns or changes a Clerk role, and never reads one: org roles (owner, admin, member, viewer) live in Marden's database, and every invitation Marden sends uses Clerk's built-in basic member role `org:member`. Clerk webhooks create and delete memberships but never change a Marden role. Where this spec maps Marden roles to Clerk roles, that mapping no longer applies; marden-backend `docs/access-control.md` is the reference.

## Setup in Clerk (human, once per instance: development for staging, production later)

- **Sign-up mode: restricted / invitation only.** Customers join by invitation to their organization; no public sign-up.
- **Sign-in:** email code and password. MFA required for owners, admins and staff.
- **Organizations on; personal accounts off.** Every user must belong to an organization.
- **Organization roles: none for Marden.** Marden only uses Clerk's built-in basic member role `org:member` (on every invitation it sends) and creates, assigns or changes no Clerk role. The instance's role settings belong to whoever runs the shared instance and mean nothing to Marden.
- **Webhook endpoint:** `https://<api>/api/webhooks/clerk`, subscribed to:
  - `user.*`
  - `organization.*`
  - `organizationMembership.*`
  - `organizationInvitation.*`
  - `session.revoked`
  - `session.ended`
- **Enterprise SSO (SAML or OIDC):** configured per customer organization when a fund asks.

## Roles and staff

| Marden role | From | Notes |
| --- | --- | --- |
| owner | Marden DB `membership.role` | Billing, transfer ownership, delete org. At least one per org. The first member of an org is its owner. |
| admin | Marden DB `membership.role` | Invite, remove, change roles (never an owner's org or product roles), alert defaults, API keys |
| member | Marden DB `membership.role` | Use everything in the plan: watchlists, alerts, stress runs, exports |
| viewer | Marden DB `membership.role` | Read only; no exports, no stress runs, no watchlist edits. The role of anyone who joins without a Marden invitation. |
| staff | Marden DB `staff_users` (not Clerk metadata) | Review queue, pipeline health, impersonation (read only, audit-logged). Requires MFA and a `@marden.ai` address. |

- **Roles are set by Marden only:** the first member of an org becomes owner; a member who joins through a Marden invitation gets the role on that invitation (kept in Marden's invitation table, never in Clerk); anyone else joins as viewer; afterwards only an owner or admin changes it, in Marden's database with an audit log entry.
- **Clerk roles are ignored.** Whatever role Clerk shows on a membership, including one set by the other product on the shared instance, never grants or changes anything in Marden.

- **Plan** (trial, design_partner, paid) lives on the Marden `organizations` row, set by billing or staff, never by Clerk metadata a customer could edit.
- **Permissions** come only from `permissions.ts`. Clerk custom permissions are not used, so there is one source of truth.

## How it works

- **Web (`@clerk/nextjs`):**
  - `clerkMiddleware` protects every app route; public routes are sign-in and the 403/404 pages only.
  - `<OrganizationSwitcher>` in the header; `useCan` reads the user's Marden permissions from `GET /api/me`.
- **API (`@clerk/fastify`):** every request runs these steps in order.
  1. Verify the Clerk session token (signature, expiry, issuer, authorized party = the web origin).
  2. Resolve the active Clerk organization to the Marden organization by `clerk_org_id`.
  3. Load the membership role from Marden's database (never from a Clerk role or the token's org role claim).
  4. Check the route's permission and the org's plan.
  5. `SET LOCAL app.org_id` so row-level security applies.

  Any failure means no access: 401 if the token is missing or invalid; 403 if there is no active org, the org is unknown, the role or plan lacks the permission, or the plan has lapsed.
- **Fail closed:** in staging and production the API refuses to start without `CLERK_SECRET_KEY` and `CLERK_PUBLISHABLE_KEY`. Stub auth exists only in local fixture mode and is compiled out of production builds.
- **SSE (`/api/stream`):**
  - Authenticated on connect with the same checks.
  - Only delivers events the user may read.
  - The server closes it when the session is revoked or ends (webhook) and re-checks at least every 10 minutes.
- **Sync:**
  - Clerk webhooks keep the `users`, `organizations`, `memberships` and `invitations` tables current. Each webhook is verified with `CLERK_WEBHOOK_SIGNING_SECRET`, processed idempotently by event id, and safe to receive out of order.
  - Webhooks create and delete memberships but never set or change a role after creation (Marden decides the role when the membership is created); a revoked Clerk invitation marks Marden's invitation revoked.
  - A nightly reconcile job compares Clerk's Backend API with the database and fixes drift, logging every change.
- **Audit:** sign-in, sign-out, invite, role change, removal, SSO change, API key change and staff impersonation are written to `audit_log`.
- **Customer API keys** are issued by Marden (hashed, scoped, revocable), not by Clerk; see the api-keys task.

## Account screen changes (spec 13)

- Team table role picker: Owner, Admin, Member, Viewer. Invites and removals go through the Clerk Backend API from our API, then sync back: an invitation always carries Clerk's basic `org:member` role, and the chosen Marden role waits in Marden's invitation table until the membership webhook arrives. Role changes write only Marden's database and the audit log; the API never calls Clerk to change a role.
- Security card: sign-in methods and MFA status from Clerk, and the SSO connection status for the org.
- **Rules:** only owners and admins invite; admins can't make owners, and can't change or remove an owner's org role or product roles (owners can); the last owner can't be removed or demoted.

## Definition of "tested and working fully"

All of these must pass in the `auth` gate. Steps 1–3 run on every merge; steps 4–5 run against the real Clerk development instance in phase 2 and in the acceptance run.

1. **Token verification (unit, locally generated keys):** each of these returns 401 and never reaches a handler:
   - valid token: passes
   - expired token
   - token not yet valid
   - wrong issuer
   - wrong authorized party
   - tampered signature
   - missing token
   - token from another Clerk instance
2. **Role mapping (unit):** every Marden role × every permission × every plan gives exactly the outcome in `permissions.ts`. A Clerk role on a membership (known or not) changes nothing, and no call asks Clerk to create or change a role.
3. **Webhooks (integration, signed test payloads):**
   - valid signature is processed once, and replaying it changes nothing
   - bad or missing signature returns 400
   - out-of-order membership events end in the correct state
   - deleting a user removes access on their next request
4. **Real Clerk instance (integration):**
   - A setup script uses the Backend API to create two test orgs, each with one user per Marden role (set in Marden's database; every Clerk membership has the basic `org:member` role), using `+clerk_test` email addresses (verification code `424242`; no email is sent).
   - Each user signs in through `@clerk/testing` (`clerkSetup`, `setupClerkTestingToken`) and calls every endpoint. Every outcome matches the matrix, and every cross-org request returns 404.
   - The script deletes all test users and orgs afterwards.
5. **End to end (Playwright, real instance):**
   - sign in; land on Home
   - switch org
   - owner invites a member, who accepts with the test code
   - admin changes the new member to viewer, and their export button disappears and the export API returns 403
   - admin can't remove the owner; the last owner can't leave
   - sign out, and the stream closes and protected pages redirect to sign-in
   - a revoked session loses access within one request

Env: `CLERK_PUBLISHABLE_KEY`, `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` (same value), `CLERK_SECRET_KEY`, `CLERK_WEBHOOK_SIGNING_SECRET`, optional `CLERK_JWT_KEY` for networkless verification.
