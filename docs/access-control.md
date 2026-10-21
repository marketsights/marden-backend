# Access control: firms, products and roles

Who may do what in the Marden API. Spec 23 covers sign-in and the Clerk side; this page covers
authorization, which is Marden's alone.

## The model

- **Clerk is identity only.** It holds users, organizations (firms), membership, invitations, MFA and
  SSO. The production Clerk instance is shared with another product, so Marden never creates, reads,
  assigns or changes a Clerk role or permission, and never changes the Clerk instance. Every invitation
  Marden sends uses Clerk's built-in basic member role (`org:member`); a Clerk role on a membership,
  whoever set it, means nothing to Marden.
- **Marden's Postgres decides access.**
  - A firm (`org`) holds **products** (`org_product`), each on a plan, with a seat limit and dates.
  - Each member holds a **role per product** (`member_product_role`). No row means no access to that
    product.
- **`membership.role` is the org-level administrative role, and it is Marden's.** It decides who manages
  the firm's members and their product roles, and grants no product data by itself. Marden sets it when
  the membership is created and changes it only on an org owner's or admin's request (see
  [Org roles](#org-roles)); Clerk webhooks never change it.
- **Staff** (`app_user.is_staff`, a `@marden.ai` address and a verified second factor) keep their
  superset on top of whatever their roles grant:
  - read every product's data;
  - review, ops, and managing firms' products.

  Products and plans do not limit staff. Writes (watchlists, stress runs, exports) still need a product
  role.

### Effective permissions

A caller's permissions are the union of three things:

1. **Org role:** `ORG_ROLE_PERMISSIONS[membership.role]` (`apps/api/src/auth/permissions.ts`):
   - every member gets `me:read` and `account:read`;
   - admins add `members:invite` and `members:manage`;
   - owners add `billing:manage` and `org:delete`.
2. **Product roles:** for each of the member's roles on a **live** product, that product's matrix
   for the role (`apps/api/src/auth/products/<product>.ts`). Each permission is kept only when the
   product's plan passes the permission's plan gate in `ROUTE_RULES`. For example, `export:xlsx` is
   `design_partner` and `paid` only.
3. **Staff:** `STAFF_PERMISSIONS`, when the caller is staff.

A product is **live** when all of these hold:

- `org_product.status = 'active'`;
- `product.active`;
- `starts_on <= today <= ends_on`, where `ends_on` is inclusive, null means open-ended, and dates are
  UTC calendar days.

A permission is granted if **any** live product role grants it under a plan its route allows. Suspended,
ended, not-yet-started and expired products grant nothing, and neither does a product with no matrix in
code.

### Per request

`apps/api/src/auth/plugin.ts` and `grants.ts` run these checks on every request, in order:

1. Verify the session token; on failure, 401.
2. Resolve the active org and membership; on failure, 403 `no_active_org` or `not_a_member`.
3. Load the member's live product roles in the same query.
4. Look up the route's rule. Default deny: a registered route without a rule stops the server from
   starting.
   - **Org permission** (account, team, product assignments): needs the org role. Otherwise 403
     `forbidden` / `role_lacks_permission`.
   - **Product permission** (every data route):
     - The caller has no live product role at all: 403 `{ "error": "no_product" }`. This covers an org
       with no live product, and a member who has not been assigned one.
     - The caller has roles, but no role's matrix grants the permission: 403 `role_lacks_permission`.
     - A role grants it, but only under plans the route refuses: 403 `plan_lacks_permission`.
   - **Staff permission:** needs staff; otherwise 403 `role_lacks_permission`.

The SSE stream re-checks on each interval (`STREAM_RECHECK_MS`, default 10 minutes). It closes when the
membership is gone or the member's product roles no longer grant `stream:read`.

## Org roles

`membership.role` is one of `owner`, `admin`, `member` and `viewer`. Owner decision (2026-10-21): it is
Marden's, never Clerk's.

| When | Role | `membership.role_source` |
| --- | --- | --- |
| The first member of an org joins (Clerk membership webhook). | `owner` | `first_member` |
| A member joins through a Marden invitation (open, not revoked, not expired at the event time; matched by the address on the Clerk membership or the user, case-insensitive). | the role on the invitation | `invitation` |
| Anyone else joins: added in Clerk's dashboard, or by the other product on the shared instance. | `viewer` | `default` |
| An org owner or admin changes it (`PATCH /api/account/members/:id`). | the new role | `changed` |
| Rows that existed before this change: their role was mirrored from Clerk and is kept as it was. | unchanged | `clerk` |

- One org's joins take turns (a transaction advisory lock), so two members joining at the same moment
  cannot both be first. Each join is audit-logged: action `member.join`, meta `{ role, rule,
  invitationId }`.
- **Invitations** (`org_invitation`). `POST /api/account/invites` asks Clerk for an organization
  invitation with the basic member role, then records the Marden role on Marden's row: Clerk invitation
  id, org, address (lower case), role, inviter, status (`pending` / `accepted` / `revoked`), Clerk's
  expiry, and who accepted it. Clerk sends the email; when the person accepts, Clerk creates the
  membership and its webhook gives them the invited role. An `organizationInvitation.revoked` webhook
  marks the row revoked. The invitation carries no product role: the new member gets one when an org
  owner or admin assigns it.
- **Changes write Marden's tables and `audit_log` only.** `PATCH /api/account/members/:id` makes no Clerk
  call (audit action `member.role`, meta `{ from, to }`; setting the role a member already holds changes
  nothing). `DELETE /api/account/members/:id` ends the Clerk membership too, because membership is
  Clerk's (audit action `member.remove`).
- **Rules** (`apps/api/src/lib/member-rules.ts`):
  - only org owners and admins invite, change roles, assign product roles and remove members;
  - an admin cannot invite an owner, make someone an owner, or change or remove an owner's org role or
    product roles: 403 `admins_cannot_manage_owners`. Owners can do all of these;
  - the last owner cannot be removed or demoted: 409 `last_owner`.
- **Clerk webhooks never change a role.** `organizationMembership.created` creates the membership with the
  role above; `organizationMembership.updated` creates it if Marden has not seen it yet and otherwise
  changes nothing; `organizationMembership.deleted` deletes it with its product roles. The `role` field
  of these events is never read.

## Tables

Migrations: `db/migrations/20261021180000_product_access.sql` (products) and
`20261021234100_marden_org_roles.sql` (Marden-owned org roles: `membership.role_source`, `org_invitation`).

| Table | Key | Columns |
| --- | --- | --- |
| `product` | `id` | `name`, `description`, `active` (false: grants nothing anywhere), `created_at`. Seeded: `marden`, the V1 app, which covers every current screen and endpoint. |
| `org_product` | `org_id, product_id` | `plan` (`trial` / `design_partner` / `paid`), `seats` (null = unlimited), `status` (`active` / `suspended` / `ended`), `starts_on`, `ends_on` (null = open-ended), `created_at` |
| `member_product_role` | `org_id, user_id, product_id` | `role` (`owner` / `admin` / `member` / `viewer`), `granted_by` (null = webhook rule or migration), `granted_at`. Rows are deleted with the membership or with the `org_product`. |
| `membership` | `org_id, user_id` | `role` (the org role, Marden's), `role_source` (`clerk` / `first_member` / `invitation` / `default` / `changed` / `system`). |
| `org_invitation` | `id` (Clerk invitation id) | `org_id`, `email` (lower case), `role`, `invited_by`, `status` (`pending` / `accepted` / `revoked`), `expires_at`, `created_at`, `accepted_by`, `accepted_at`. |

- **Seats.** Role assignments on an `org_product` never exceed `seats`. Every assignment locks the
  `org_product` row, then counts.
  - A new assignment needs a free seat.
  - Changing an existing role does not take a seat.
  - Staff cannot set `seats` below the assignments already held.
- **`org.plan` is deprecated.** It is kept for now and nothing reads it. Plans live on `org_product`.
- **The migration carries existing access over.**
  - Orgs whose plan Marden set (`design_partner` or `paid`; Clerk cannot set a plan, and webhooks create
    orgs on the default `trial`) get `marden` on that plan, with unlimited seats from the org's creation
    date. Each member keeps their current role on it.
  - Trial orgs get no product (fail closed). Staff add one with the admin endpoint below.
- **The org roles migration keeps every current role.** Existing memberships keep the role Clerk last
  gave them, marked `role_source = 'clerk'`. Invitations sent before the change (audit action
  `member.invite`) become `org_invitation` rows with their Marden role and Clerk's default 30-day expiry;
  one whose address is already a member of the org counts as accepted.

## Endpoints

| Endpoint | Who | What |
| --- | --- | --- |
| `GET /api/me` | any member | `{ user, org: { id, name }, orgRole, products: [{ id, name, plan, status, role, seats, seatsUsed }], permissions, staff }`. `products` lists the org's live products; `role` is the caller's role, or null when none was assigned. `products: []` means the org has no live product. |
| `GET /api/account` | any member | `team[]`: each member with their org `role` and `products: [{ productId, role }]`. `products[]`: the org's products, live or not: `{ id, name, plan, status, seats, seatsUsed, startsOn, endsOn, live }`. Plus `seats` (member count) and `security`. |
| `POST /api/account/invites` | org owner or admin | Body `{ email, role }`, `role` default `viewer`. Clerk invitation with the basic member role; the Marden role goes to `org_invitation`. Answers 201 `{ invitationId, email, role }`. Admin inviting an owner: 403 `admins_cannot_manage_owners`. No Clerk secret key: 503. Audit action `member.invite`, meta `{ email, role }`. |
| `PATCH /api/account/members/:id` | org owner or admin | Body `{ role }`. Marden's tables only, no Clerk call. Answers the member. 403 `admins_cannot_manage_owners`, 409 `last_owner`, 404 `member`. Audit action `member.role`. |
| `DELETE /api/account/members/:id` | org owner or admin | Ends the Clerk membership and deletes the Marden one with its product roles. 204; same refusals as above; no Clerk secret key (outside dev mode): 503. Audit action `member.remove`. |
| `PATCH /api/account/members/:id/products` | org owner or admin | Body `{ productId, role }`, where `role` null removes the role. Answers the member. Only an owner changes an owner's product roles: 403 `admins_cannot_manage_owners`. Seat limit: 409 `seat_limit` with `seats` and `used`. Unknown member: 404 `member`. Product the org does not hold: 404 `product`. Audit action `member.product_role`, meta `{ productId, from, to }`. |
| `POST /api/admin/orgs/:orgId/products` | staff | Body `{ productId, plan, seats, startsOn, endsOn?, status? }`; `endsOn` defaults to null, `status` to `active`. Answers 201 with the org product. Errors: unknown org or product 404; product without a matrix in code 400; already held 409 `org_product_exists`; `endsOn` before `startsOn` 400. Audit action `org_product.create`, meta `{ productId, to }`. |
| `PATCH /api/admin/orgs/:orgId/products/:productId` | staff | Body: any of `{ plan, seats, startsOn, endsOn, status }`. Seats below those in use: 409 `seats_in_use`. Audit action `org_product.update`, meta `{ productId, from, to }`. |

## Webhooks and new members

- Clerk webhooks keep creating and updating `org` and `app_user` rows, and create and delete `membership`
  rows. They never change `membership.role` (see [Org roles](#org-roles)).
- A new member gets **no product role** until an org owner or admin assigns one (fail closed). There is
  one exception: the **first member** of an org that already holds live products becomes `owner` of
  each of them, seats permitting. That grant is audit-logged with `rule: 'first_member'`. One org's
  membership webhooks take this check in turn (a transaction advisory lock), so two members joining at
  the same moment cannot both count as first.
- A membership update never grants or changes product roles.
- Removing a member deletes their product roles.

## Dev mode

With `MARDEN_AUTH_MODE=dev`, startup gives org `dev` product `marden` on plan `paid` with unlimited
seats, and makes `dev-user` (staff) its owner. Local development keeps every screen.

## Adding a product

1. Write a migration that inserts the catalog row:
   `INSERT INTO product (id, name, description) VALUES ('pricing', 'Marden Pricing', '...')`.
2. Add `apps/api/src/auth/products/pricing.ts` with the role -> permission matrix. Build it from the
   existing vocabulary (`PRODUCT_READ`, `PRODUCT_USE`, `prefs:write` in `permissions.ts`). The
   `ProductMatrix` type only accepts product permissions, so a product can never grant org
   administration or staff powers.
3. Register it in `apps/api/src/auth/products/index.ts` (`PRODUCTS.pricing = PRICING`). Staff cannot
   give a firm a product that has no matrix.
4. New screens or endpoints for the product:
   - add their permission to the vocabulary and a rule to `ROUTE_RULES`; startup refuses a route
     without one;
   - add the permission to the matrices of the products that should grant it.
5. Staff give firms the product: `POST /api/admin/orgs/:orgId/products`. Org owners and admins then
   assign roles: `PATCH /api/account/members/:id/products`.

## Examples

A firm holds `marden` on `trial` and `pricing` on `paid`. Suppose the `pricing` matrix grants members
`instruments:read` and `export:xlsx`:

| Member | Org role | Product roles | Result |
| --- | --- | --- | --- |
| Ana | owner | none | Manages the team and product roles. Every data route answers 403 `no_product` until she assigns herself a role. |
| Ben | member | `marden: member` | Every V1 screen, watchlists and stress runs. Export answers 403 `plan_lacks_permission`, because `marden` is on trial. |
| Cy | member | `pricing: member` | Instruments, and exports (via `pricing`, which is paid). `/api/sites` answers 403 `role_lacks_permission`. |
| Dee | viewer | `marden: viewer`, `pricing: member` | Reads every V1 screen and exports via `pricing`. No watchlist edits. |

If staff suspend `pricing` (`PATCH .../products/pricing { "status": "suspended" }`):

- Cy's next request answers 403 `no_product`, and his stream closes on the next re-check.
- Dee keeps `marden` as a viewer, without export.
