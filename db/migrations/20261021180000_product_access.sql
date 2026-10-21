-- migrate:up
-- Per-product access (docs/access-control.md). Clerk is identity only (users, organizations,
-- membership, MFA, SSO); these tables decide what a member may use:
--   product              the catalog. One row per product Marden sells; each product's
--                        role -> permission matrix lives in code (apps/api/src/auth/products/<id>.ts).
--   org_product          which firm has which product, on which plan, with how many seats, from when to when.
--   member_product_role  each member's role on each of the firm's products. No row = no access to that product.
-- membership.role stays the ORG-level administrative role (who manages the firm's members and their
-- product roles); it grants no product permission by itself.
CREATE TABLE product (
  id           text PRIMARY KEY,                -- 'marden'; matches apps/api/src/auth/products/<id>.ts
  name         text NOT NULL,
  description  text,
  active       boolean NOT NULL DEFAULT true,   -- false: the product grants nothing anywhere
  created_at   timestamptz NOT NULL DEFAULT now()
);

INSERT INTO product (id, name, description) VALUES
  ('marden', 'Marden', 'The V1 app: every current screen and endpoint.');

CREATE TABLE org_product (
  org_id      text NOT NULL REFERENCES org(id) ON DELETE CASCADE,
  product_id  text NOT NULL REFERENCES product(id),
  plan        text NOT NULL CHECK (plan IN ('trial', 'design_partner', 'paid')),
  seats       integer CHECK (seats IS NULL OR seats >= 0),   -- null = unlimited
  status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'ended')),
  starts_on   date NOT NULL,                    -- first day of access (UTC)
  ends_on     date,                             -- last day of access (UTC, inclusive); null = open-ended
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, product_id),
  CHECK (ends_on IS NULL OR ends_on >= starts_on)
);
CREATE INDEX org_product_product ON org_product (product_id);

CREATE TABLE member_product_role (
  org_id      text NOT NULL,
  user_id     text NOT NULL,
  product_id  text NOT NULL,
  role        text NOT NULL CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
  granted_by  text REFERENCES app_user(id) ON DELETE SET NULL,   -- null: granted by a webhook rule or migration
  granted_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, user_id, product_id),
  FOREIGN KEY (org_id, user_id) REFERENCES membership(org_id, user_id) ON DELETE CASCADE,
  FOREIGN KEY (org_id, product_id) REFERENCES org_product(org_id, product_id) ON DELETE CASCADE
);
CREATE INDEX member_product_role_product ON member_product_role (org_id, product_id);

COMMENT ON COLUMN org.plan IS
  'Deprecated: plans live on org_product (per product). Kept for now; nothing reads it.';

-- Orgs whose plan Marden set explicitly (design_partner or paid: Clerk cannot set a plan, and webhooks
-- create orgs on the default trial) keep the V1 app they had: the plan moves to org_product and each
-- member keeps their current role on it. Trial orgs get no product (fail closed); staff add it with
-- POST /api/admin/orgs/:orgId/products.
INSERT INTO org_product (org_id, product_id, plan, seats, status, starts_on)
SELECT id, 'marden', plan, NULL, 'active', (created_at AT TIME ZONE 'UTC')::date
FROM org WHERE plan IN ('design_partner', 'paid');

INSERT INTO member_product_role (org_id, user_id, product_id, role, granted_by)
SELECT m.org_id, m.user_id, 'marden', m.role, NULL
FROM membership m JOIN org_product op ON op.org_id = m.org_id AND op.product_id = 'marden';

-- migrate:down
DROP TABLE IF EXISTS member_product_role, org_product, product;
COMMENT ON COLUMN org.plan IS NULL;
