// The unlicensed-price switch on the API's own fact reads (docs/methods/licensing.md §3).
//
// The API serves read models the Python builders made, and those builders apply
// MARDEN_SHOW_UNLICENSED_PRICES themselves. The few places the API reads `fact` itself (permit numbers
// for search and the data-center / plant search box, the staff review queue) apply the same switch here:
// with it false, rows whose source_name is 'unlicensed-commercial' are left out before fact_current /
// fact_asof precedence picks, exactly as services/marden/core/facts.py does.
import { dayEnd } from './business-day.js';

/**
 * The exact source_name of every 'unlicensed-commercial' entry in services/marden/core/licensing.py
 * (`unlicensed_source_names()`, sorted). services/tests/readmodels/test_licence_switch.py fails when the
 * two lists differ.
 */
export const UNLICENSED_SOURCE_NAMES: readonly string[] = [
  'Börse Frankfurt',
  'El Paso Electric',
  'FINRA TRACE (finra.org)',
  'ICE Data Indices, LLC via FRED',
  'SPP',
  'Yahoo Finance',
  'iShares HYBB fund OAS (BlackRock)',
  'iShares USHY fund OAS (BlackRock)',
  'iShares USIG fund OAS (BlackRock)',
];

/** The source names left out under the switch: none while it is true, every unlicensed name when false. */
export function hiddenSources(showUnlicensed: boolean): readonly string[] {
  return showUnlicensed ? [] : UNLICENSED_SOURCE_NAMES;
}

/** fact_current / fact_asof order after (subject, field): non-model first, newest valid_from, F over R, newest learned. */
const PRECEDENCE = `(tag = 'M'), valid_from DESC, (CASE tag WHEN 'F' THEN 0 WHEN 'R' THEN 1 ELSE 2 END), learned_at DESC, id DESC`;

/**
 * SQL source of facts as known on a day, licence switch applied: `fact_current` (asOfPh null) or
 * `fact_asof(day)` while nothing is hidden; otherwise the same precedence over the rows that remain, so a
 * field falls back to its next licensed value. `hiddenPh` is the placeholder bound to hiddenSources(...)
 * (a text[]), null when nothing is hidden.
 */
export function factSource(asOfPh: string | null, hiddenPh: string | null): string {
  if (hiddenPh === null) return asOfPh === null ? 'fact_current' : `fact_asof(${asOfPh}::date)`;
  const known = asOfPh === null
    ? `status = 'published' AND (valid_to IS NULL OR valid_to > CURRENT_DATE)`
    : `status IN ('published', 'superseded') AND valid_from <= ${asOfPh}::date
       AND learned_at < ${dayEnd(asOfPh)} AND (valid_to IS NULL OR valid_to > ${asOfPh}::date)`;
  return `(SELECT DISTINCT ON (subject_type, subject_id, field) * FROM fact
           WHERE ${known} AND source_name <> ALL(${hiddenPh}::text[])
           ORDER BY subject_type, subject_id, field, ${PRECEDENCE})`;
}
