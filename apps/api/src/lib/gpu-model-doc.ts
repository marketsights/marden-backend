// GPU model page document (spec 19, GET /api/gpus/:model and the gpu-model export). The model key is
// matched case-insensitively against the models the GPU index lists, so /gpus/h100 serves H100. A model
// the index does not list is a 404; one it lists but the GPU model has not computed yet is a 503
// not_ready, as is any model while the index itself is not built.
import type { GpuIndexPage } from '../contracts/app.js';
import type { GpuModelDetail } from '../contracts/types.js';
import type { Db } from '../db.js';
import { notFound, notReady } from '../errors.js';

/** Model names the index document lists (price points, provider prices, value curves). */
export function indexedModels(index: Pick<GpuIndexPage, 'index' | 'providers'> & { valueByAge?: GpuIndexPage['valueByAge'] }): string[] {
  const rows = [...(index.index ?? []), ...(index.providers ?? []), ...(index.valueByAge ?? [])];
  return [...new Set(rows.map((r) => r.model))];
}

export async function loadGpuModel(db: Db, model: string, asOf: string | null): Promise<GpuModelDetail> {
  const doc = await db.readModel<GpuModelDetail>('gpu_model', model, asOf);
  if (doc !== null) return doc;
  const index = await db.readModel<GpuIndexPage>('gpus', 'all', asOf);
  if (index === null) throw notReady('gpu_model', model);
  const canonical = indexedModels(index).find((m) => m.toLowerCase() === model.toLowerCase());
  if (!canonical) throw notFound('gpu_model');
  if (canonical !== model) {
    const named = await db.readModel<GpuModelDetail>('gpu_model', canonical, asOf);
    if (named !== null) return named;
  }
  throw notReady('gpu_model', canonical);
}
