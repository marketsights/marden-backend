// Alert settings (spec 11, 13) and Home layout (spec 01, 02): per user within the org, defaults
// until the user saves their own.
import type { FastifyInstance } from 'fastify';
import { authOf } from '../auth/plugin.js';
import type { Db } from '../db.js';
import { alertSettingsSchema, homeLayoutSchema } from '../lib/defaults.js';
import { audit } from '../queries/audit.js';
import { getAlertSettings, getHomeLayout, putAlertSettings, putHomeLayout } from '../queries/prefs.js';
import { parseBody } from './serve.js';

export function prefsRoutes(app: FastifyInstance, db: Db): void {
  app.get('/api/alert-settings', async (req) => {
    const auth = authOf(req);
    return db.withOrg(auth.orgId, (c) => getAlertSettings(c, auth));
  });

  app.put('/api/alert-settings', async (req) => {
    const auth = authOf(req);
    const settings = parseBody(alertSettingsSchema, req.body);
    await db.withOrg(auth.orgId, async (c) => {
      await putAlertSettings(c, auth, settings);
      await audit(c, { orgId: auth.orgId, userId: auth.userId, action: 'alert_settings.update', ip: req.ip, meta: { settings } });
    });
    return settings;
  });

  app.get('/api/home/layout', async (req) => {
    const auth = authOf(req);
    return db.withOrg(auth.orgId, (c) => getHomeLayout(c, auth));
  });

  app.put('/api/home/layout', async (req) => {
    const auth = authOf(req);
    const layout = parseBody(homeLayoutSchema, req.body);
    await db.withOrg(auth.orgId, async (c) => {
      await putHomeLayout(c, auth, layout);
      await audit(c, {
        orgId: auth.orgId, userId: auth.userId, action: 'home_layout.update', ip: req.ip,
        meta: { widgets: layout.widgets.map((w) => w.key) },
      });
    });
    return layout;
  });
}
