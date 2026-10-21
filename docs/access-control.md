# Access control: firms, products and roles

Who may do what in the Marden API. Spec 23 covers sign-in and the Clerk side; this page covers
authorization, which is Marden's alone.

## The model

- **Clerk is identity only.** It holds users, organizations (firms), membership, MFA and SSO. Marden
  creates no Clerk roles or permissions for this model and never changes the Clerk instance. The
  production Clerk instance is shared with another Marden product line.
- **Marden's Postgres decides access.**
  - A firm (`org`) holds **products** (`org_product`), each on a plan, with a seat limit and dates.
  - Each member holds a **role per product** (`member_product_role`). No row means no access to that
    product.
- **`membership.role` is the org-level administrative role.** It decides who manages the firm's members
  and their product roles. It grants no product data by itself. Clerk webhooks still set it from the
  Clerk organization role (spec 23).
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

## Tables

Migration: `db/migrations/20261021180000_product_access.sql`.

| Table | Key | Columns |
| --- | --- | --- |
| `product` | `id` | `name`, `description`, `active` (false: grants nothing anywhere), `created_at`. Seeded: `marden`, the V1 app, which covers every current screen and endpoint. |
| `org_product` | `org_id, product_id` | `plan` (`trial` / `design_partner` / `paid`), `seats` (null = unlimited), `status` (`active` / `suspended` / `ended`), `starts_on`, `ends_on` (null = open-ended), `created_at` |
| `member_product_role` | `org_id, user_id, product_id` | `role` (`owner` / `admin` / `member` / `viewer`), `granted_by` (null = webhook rule or migration), `granted_at`. Rows are deleted with the membership or with the `org_product`. |

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

## Endpoints

| Endpoint | Who | What |
| --- | --- | --- |
| `GET /api/me` | any member | `{ user, org: { id, name }, orgRole, products: [{ id, name, plan, status, role, seats, seatsUsed }], permissions, staff }`. `products` lists the org's live products; `role` is the caller's role, or null when none was assigned. `products: []` means the org has no live product. |
| `GET /api/account` | any member | `team[]`: each member with their org `role` and `products: [{ productId, role }]`. `products[]`: the org's products, live or not: `{ id, name, plan, status, seats, seatsUsed, startsOn, endsOn, live }`. Plus `seats` (member count) and `security`. |
| `PATCH /api/account/members/:id/products` | org owner or admin | Body `{ productId, role }`, where `role` null removes the role. Answers the member. Seat limit: 409 `seat_limit` with `seats` and `used`. Unknown member: 404 `member`. Product the org does not hold: 404 `product`. Audit action `member.product_role`, meta `{ productId, from, to }`. |
| `POST /api/admin/orgs/:orgId/products` | staff | Body `{ productId, plan, seats, startsOn, endsOn?, status? }`; `endsOn` defaults to null, `status` to `active`. Answers 201 with the org product. Errors: unknown org or product 404; product without a matrix in code 400; already held 409 `org_product_exists`; `endsOn` before `startsOn` 400. Audit action `org_product.create`, meta `{ productId, to }`. |
| `PATCH /api/admin/orgs/:orgId/products/:productId` | staff | Body: any of `{ plan, seats, startsOn, endsOn, status }`. Seats below those in use: 409 `seats_in_use`. Audit action `org_product.update`, meta `{ productId, from, to }`. |

## Webhooks and new members

- Clerk webhooks keep creating and updating `org`, `app_user` and `membership` rows.
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
