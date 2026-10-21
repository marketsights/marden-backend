// Per-user preferences inside an org: alert settings and Home layout. Missing rows mean the defaults.
import type { AlertSettings, HomeLayout } from '../contracts/types.js';
import type { Queryable } from '../db.js';
import { defaultAlertSettings, defaultHomeLayout } from '../lib/defaults.js';

type Who = { orgId: string; userId: string };

export async function getAlertSettings(c: Queryable, who: Who): Promise<AlertSettings> {
  const res = await c.query<{ settings: Partial<AlertSettings> }>(
    `SELECT settings FROM alert_settings WHERE org_id = $1 AND user_id = $2`,
    [who.orgId, who.userId],
  );
  const stored = res.rows[0]?.settings;
  if (!stored) return defaultAlertSettings();
  const defaults = defaultAlertSettings();
  return { ...defaults, ...stored, types: { ...defaults.types, ...(stored.types ?? {}) } };
}

export async function putAlertSettings(c: Queryable, who: Who, settings: AlertSettings): Promise<void> {
  await c.query(
    `INSERT INTO alert_settings (org_id, user_id, settings) VALUES ($1, $2, $3)
     ON CONFLICT (org_id, user_id) DO UPDATE SET settings = EXCLUDED.settings`,
    [who.orgId, who.userId, JSON.stringify(settings)],
  );
}

export async function getHomeLayout(c: Queryable, who: Who): Promise<HomeLayout> {
  const res = await c.query<{ layout: HomeLayout }>(
    `SELECT layout FROM home_layout WHERE org_id = $1 AND user_id = $2`,
    [who.orgId, who.userId],
  );
  return res.rows[0]?.layout ?? defaultHomeLayout();
}

export async function putHomeLayout(c: Queryable, who: Who, layout: HomeLayout): Promise<void> {
  await c.query(
    `INSERT INTO home_layout (org_id, user_id, layout) VALUES ($1, $2, $3)
     ON CONFLICT (org_id, user_id) DO UPDATE SET layout = EXCLUDED.layout`,
    [who.orgId, who.userId, JSON.stringify(layout)],
  );
}
