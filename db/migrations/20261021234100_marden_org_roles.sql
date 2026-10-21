-- migrate:up
-- Org roles are Marden's (docs/access-control.md; owner decision 2026-10-21). The marden.ai Clerk
-- production instance is shared with another product, so a Clerk organization role says nothing about
-- Marden. Clerk holds identity, organization membership and invitations, always with Clerk's built-in
-- basic member role (org:member); Marden never creates, reads or changes Clerk roles.
--
-- membership.role (owner / admin / member / viewer) is set by Marden when the membership is created:
--   - the first member of an org becomes owner;
--   - a member who joins through a Marden invitation gets the role recorded on it (org_invitation);
--   - anyone else (added in Clerk's dashboard or by the other product) joins as viewer;
-- and afterwards changes only through PATCH /api/account/members/:id (Marden's tables plus audit_log).
-- Clerk webhooks create and delete memberships but never change a role after creation.
--
-- Existing rows keep their role. It was mirrored from Clerk before this change, which role_source
-- 'clerk' records.

ALTER TABLE membership
  ADD COLUMN role_source text NOT NULL DEFAULT 'clerk'
    CHECK (role_source IN ('clerk', 'first_member', 'invitation', 'default', 'changed', 'system'));
ALTER TABLE membership ALTER COLUMN role_source SET DEFAULT 'system';

COMMENT ON COLUMN membership.role IS
  'Marden org role (owner/admin/member/viewer), owned by Marden: set on creation (first member owner, '
  'invitation role, else viewer), changed only by PATCH /api/account/members/:id. Never from Clerk.';
COMMENT ON COLUMN membership.role_source IS
  'Where role came from: clerk (mirrored from a Clerk org role before Marden owned roles), first_member, '
  'invitation, default (joined without a Marden invitation), changed (an org owner or admin), system (dev, seeds).';

-- Marden's side of an organization invitation. Clerk sends the email and creates the membership on
-- acceptance (always org:member); the Marden role travels here, never in Clerk.
CREATE TABLE org_invitation (
  id           text PRIMARY KEY,                 -- Clerk organization invitation id
  org_id       text NOT NULL REFERENCES org(id) ON DELETE CASCADE,
  email        text NOT NULL CHECK (email = lower(email) AND email <> ''),
  role         text NOT NULL CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
  invited_by   text REFERENCES app_user(id) ON DELETE SET NULL,
  status       text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'revoked')),
  expires_at   timestamptz,                      -- Clerk's expiry; null = not known
  created_at   timestamptz NOT NULL DEFAULT now(),
  accepted_by  text REFERENCES app_user(id) ON DELETE SET NULL,
  accepted_at  timestamptz,
  CHECK ((status = 'accepted') = (accepted_at IS NOT NULL))
);
CREATE INDEX org_invitation_pending ON org_invitation (org_id, email) WHERE status = 'pending';

-- Invitations sent before this change carried the Marden role as a Clerk role; the API audit-logged
-- each one (action member.invite, target invitation:<Clerk id>, meta {email, role}). Carry them over so
-- people who accept them still get the role they were invited with. Clerk invitations expire after
-- 30 days by default. One whose address is already a member of the org counts as accepted.
INSERT INTO org_invitation (id, org_id, email, role, invited_by, status, expires_at, created_at, accepted_at)
SELECT inv.id, inv.org_id, inv.email, inv.role, inv.invited_by,
       CASE WHEN inv.joined THEN 'accepted' ELSE 'pending' END,
       inv.at + interval '30 days', inv.at,
       CASE WHEN inv.joined THEN now() END
FROM (
  SELECT DISTINCT ON (substr(a.target, 12))
         substr(a.target, 12) AS id, a.org_id, lower(a.meta ->> 'email') AS email, a.meta ->> 'role' AS role,
         (SELECT u.id FROM app_user u WHERE u.id = a.user_id) AS invited_by, a.at,
         EXISTS (SELECT 1 FROM membership m JOIN app_user u ON u.id = m.user_id
                 WHERE m.org_id = a.org_id AND lower(u.email) = lower(a.meta ->> 'email')) AS joined
  FROM audit_log a
  WHERE a.action = 'member.invite' AND a.target LIKE 'invitation:_%'
    AND a.org_id IN (SELECT id FROM org)
    AND a.meta ->> 'role' IN ('owner', 'admin', 'member', 'viewer')
    AND coalesce(a.meta ->> 'email', '') <> ''
  ORDER BY substr(a.target, 12), a.at DESC
) inv
ON CONFLICT (id) DO NOTHING;

-- migrate:down
DROP TABLE IF EXISTS org_invitation;
ALTER TABLE membership DROP COLUMN IF EXISTS role_source;
COMMENT ON COLUMN membership.role IS NULL;
