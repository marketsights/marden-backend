// Excel export (spec 00 "Exports"): a data sheet with the table's rows and columns as served, and a
// sources sheet listing every Sourced value and evidence record in those rows (field, tag, URL, as-of).
import ExcelJS from 'exceljs';

type Json = Record<string, unknown>;

const TAGS = new Set(['F', 'R', 'M']);
const MAX_CELL = 32_000; // Excel's cell limit is 32,767 characters
const MAX_DEPTH = 6;

const isObj = (v: unknown): v is Json => typeof v === 'object' && v !== null && !Array.isArray(v);

export const isSourced = (v: unknown): v is Json =>
  isObj(v) && 'value' in v && TAGS.has(v.tag as string) && typeof v.asOf === 'string';

const isEvidence = (v: unknown): v is Json =>
  isObj(v) && TAGS.has(v.tag as string) && typeof v.date === 'string' && typeof v.text === 'string';

const text = (v: unknown): string => (typeof v === 'string' ? v : '');
const clip = (s: string): string => (s.length > MAX_CELL ? `${s.slice(0, MAX_CELL)}…` : s);

function arrayCell(v: unknown[]): string | null {
  if (v.every((x) => typeof x === 'string' || typeof x === 'number')) return clip(v.join(', '));
  if (v.every((x) => isObj(x) && typeof x.party === 'string')) {
    return clip(v.map((x) => `${text((x as Json).party)} (${text((x as Json).status)})`).join(' → '));
  }
  return null;
}

function objectCell(v: Json): string | null {
  if (typeof v.built === 'number' && typeof v.total === 'number') return `${v.built} / ${v.total}`;
  if (typeof v.siteName === 'string') return v.siteName;
  if (typeof v.name === 'string' && typeof v.id === 'string') return v.name;
  return null;
}

/** One cell per top-level field, readable rather than raw JSON where the shape is known. */
export function cellValue(v: unknown): string | number | boolean {
  if (v === null || v === undefined) return '';
  if (typeof v === 'number' || typeof v === 'boolean') return v;
  if (typeof v === 'string') return clip(v);
  if (isSourced(v)) return v.value === null ? 'n/d' : cellValue(v.value);
  if (isEvidence(v)) return clip(`${String(v.date)} · ${String(v.text)}`);
  const shaped = Array.isArray(v) ? arrayCell(v) : isObj(v) ? objectCell(v) : null;
  return shaped ?? clip(JSON.stringify(v));
}

export interface SourceLine {
  row: string; field: string; value: string | number | boolean; tag: string;
  source: string; url: string; asOf: string; method: string;
}

const rowLabel = (row: Json, index: number): string => {
  for (const k of ['id', 'slug', 'instrumentId', 'refId', 'name']) if (typeof row[k] === 'string') return row[k];
  return `#${index + 1}`;
};

function walk(value: unknown, path: string, out: (path: string, v: Json) => void, depth: number): void {
  if (depth > MAX_DEPTH || value === null || typeof value !== 'object') return;
  if (Array.isArray(value)) {
    value.forEach((item, i) => walk(item, `${path}[${i}]`, out, depth + 1));
    return;
  }
  if (isSourced(value) || isEvidence(value)) out(path, value);
  for (const [k, v] of Object.entries(value)) {
    if (isSourced(value) && k === 'value') continue;
    walk(v, path ? `${path}.${k}` : k, out, depth + 1);
  }
}

export function collectSources(rows: Json[]): SourceLine[] {
  const lines: SourceLine[] = [];
  rows.forEach((row, i) => {
    const label = rowLabel(row, i);
    walk(row, '', (field, v: Json) => {
      const sourced = 'asOf' in v && 'value' in v; // Sourced<T>; otherwise an Evidence record
      lines.push({
        row: label,
        field,
        value: sourced ? cellValue(v) : cellValue(v['text']),
        tag: text(v['tag']),
        source: text(v['sourceName']),
        url: text(v['sourceUrl']),
        asOf: text(sourced ? v['asOf'] : v['date']),
        method: text(v['methodVersion']),
      });
    }, 0);
  });
  return lines;
}

export function columnsOf(rows: Json[]): string[] {
  const keys = new Set<string>();
  for (const row of rows) for (const k of Object.keys(row)) keys.add(k);
  return [...keys];
}

export async function buildWorkbook(meta: { table: string; view: string; asOf: string | null }, rows: Json[]): Promise<Buffer> {
  const wb = new ExcelJS.Workbook();
  wb.creator = 'Marden';
  wb.created = new Date();
  const data = wb.addWorksheet('Data', { views: [{ state: 'frozen', ySplit: 1 }] });
  const columns = columnsOf(rows);
  data.columns = columns.map((key) => ({ header: key, key, width: Math.min(Math.max(key.length + 2, 12), 40) }));
  for (const row of rows) data.addRow(Object.fromEntries(columns.map((k) => [k, cellValue(row[k])])));
  data.getRow(1).font = { bold: true };

  const sources = wb.addWorksheet('Sources', { views: [{ state: 'frozen', ySplit: 1 }] });
  sources.columns = [
    { header: 'Row', key: 'row', width: 24 }, { header: 'Field', key: 'field', width: 28 },
    { header: 'Value', key: 'value', width: 24 }, { header: 'Tag', key: 'tag', width: 6 },
    { header: 'Source', key: 'source', width: 24 }, { header: 'URL', key: 'url', width: 48 },
    { header: 'As of', key: 'asOf', width: 12 }, { header: 'Method', key: 'method', width: 14 },
  ];
  for (const line of collectSources(rows)) sources.addRow(line);
  sources.getRow(1).font = { bold: true };
  wb.title = `Marden ${meta.table} (${meta.view}) as of ${meta.asOf ?? 'latest'}`;
  return Buffer.from(await wb.xlsx.writeBuffer());
}
