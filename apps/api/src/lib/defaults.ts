// Product defaults written in the specs (not data): alert settings (spec 00 "Materiality and alerts",
// spec 13) and the six default Home widgets in order (spec 01, 02).
import { z } from 'zod';
import type { AlertSettings, HomeLayout, StressRequest, WidgetKey } from '../contracts/types.js';

export const ALERT_TYPES = ['cushion', 'milestone', 'power', 'opposition', 'tenant', 'price_or_mark', 'construction_stall'] as const;

export function defaultAlertSettings(): AlertSettings {
  return {
    types: { cushion: true, milestone: true, power: true, opposition: true, tenant: true, price_or_mark: true, construction_stall: true },
    instantRed: true,
    dailyDigest: true,
    weeklyNote: true,
    priceThresholdPts: 2,
  };
}

export const DEFAULT_WIDGETS: readonly WidgetKey[] = [
  'headline', 'watchlist_changes', 'alerts', 'tightest_cushions', 'risk_top5', 'coming_up',
];

export const defaultHomeLayout = (): HomeLayout => ({ widgets: DEFAULT_WIDGETS.map((key) => ({ key })) });

export const WIDGET_KEYS = [
  'headline', 'watchlist_changes', 'alerts', 'tightest_cushions', 'risk_top5', 'coming_up', 'mark_gaps',
  'saved_stress', 'payer_exposure', 'curve_5y', 'site_schedule', 'instrument_price',
] as const satisfies readonly WidgetKey[];

const typesShape = Object.fromEntries(ALERT_TYPES.map((t) => [t, z.boolean()])) as Record<(typeof ALERT_TYPES)[number], z.ZodBoolean>;

export const alertSettingsSchema = z.strictObject({
  types: z.strictObject(typesShape),
  instantRed: z.boolean(),
  dailyDigest: z.boolean(),
  weeklyNote: z.boolean(),
  priceThresholdPts: z.union([z.literal(1), z.literal(2), z.literal(3)]),
});

export const homeLayoutSchema = z.strictObject({
  widgets: z
    .array(z.strictObject({ key: z.enum(WIDGET_KEYS), params: z.record(z.string().max(100), z.string().max(200)).optional() }))
    .max(24),
});

/** The most instruments one run may be limited to (params.instruments). */
export const STRESS_MAX_INSTRUMENTS = 100;
/** params.instruments: the instrument ids a V1 run applies its shock to (the stress model's selected_result),
 *  deduplicated and sorted so the same choice is the same run (stress_run keys custom runs by the request). */
const stressInstruments = z
  .array(z.string().trim().min(1).max(100))
  .min(1)
  .max(STRESS_MAX_INSTRUMENTS)
  .transform((ids) => [...new Set(ids)].sort());
const stressParams = z
  .record(z.string().max(50), z.union([z.number(), z.string().max(200), stressInstruments]))
  .refine((p) => Object.entries(p).every(([k, v]) => Array.isArray(v) === (k === 'instruments')), {
    message: 'params.instruments is a list of instrument ids; every other parameter is a number or text',
  });

/** StressRequest (spec 09; params.instruments limits a run to chosen instruments) plus the contagion scenario
 *  (spec 18: params company, payPct). */
export const stressRequestSchema = z.discriminatedUnion('scenario', [
  z.strictObject({
    scenario: z.enum(['end_user_funding_miss', 'tenant_downgrade', 'power_delay']),
    params: stressParams,
    severity: z.enum(['base', 'severe']),
  }),
  z.strictObject({
    scenario: z.literal('contagion'),
    params: z.strictObject({ company: z.string().min(1).max(200), payPct: z.number().min(0).max(100) }),
    severity: z.enum(['base', 'severe']),
  }),
]);

export type StressRunRequest = StressRequest | z.infer<typeof stressRequestSchema>;
