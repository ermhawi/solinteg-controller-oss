/** Server-only, derived EVCC load. Never changes raw meter or fuse readings. */
import { readFileSync } from 'node:fs';

export interface EvHourlyLoad {
  utcHour: string; // UTC hour matching readings' strftime('%Y-%m-%dT%H') bucket
  evAvgW: number;  // independently measured Ict2 × NOMINAL 230 V
  samples: number;
}

const DEFAULT_PATH = '/opt/solinteg/ev-shadow-runtime/var/ev-load-hourly.json';
const HOUR_MS = 3_600_000;

export function validateEvHourlyLoad(raw: unknown, nowMs = Date.now()): EvHourlyLoad[] {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return [];
  const j = raw as Record<string, unknown>;
  if (j.version !== 1 || j.source !== 'waveshare_evcc_fc03_ict2' ||
      j.quality !== 'full_hour_measured_current_nominal_230v' ||
      typeof j.generatedAtUtc !== 'string' ||
      !Array.isArray(j.hours) || j.hours.length > 400) return [];
  const created = Date.parse(j.generatedAtUtc);
  if (!Number.isFinite(created) || created > nowMs + 5000 ||
      nowMs - created > 15 * 60_000) return [];
  const latestClosedHour = Math.floor(nowMs / HOUR_MS) * HOUR_MS;
  const seen = new Set<string>();
  const valid: EvHourlyLoad[] = [];
  for (const item of j.hours) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return [];
    const row = item as Record<string, unknown>;
    if (typeof row.utcHour !== 'string' ||
        !/^\d{4}-\d{2}-\d{2}T\d{2}:00:00Z$/.test(row.utcHour) ||
        typeof row.evAvgW !== 'number' || !Number.isFinite(row.evAvgW) ||
        row.evAvgW < 0 || row.evAvgW > 4000 ||
        typeof row.samples !== 'number' || !Number.isInteger(row.samples) ||
        row.samples < 100 || row.samples > 500 ||
        seen.has(row.utcHour)) return [];
    const at = Date.parse(row.utcHour);
    if (!Number.isFinite(at) || at >= latestClosedHour ||
        at < nowMs - 15 * 86_400_000) return [];
    seen.add(row.utcHour);
    valid.push({ utcHour: row.utcHour, evAvgW: row.evAvgW, samples: row.samples });
  }
  return valid;
}

export function readEvHourlyLoad(nowMs = Date.now()): EvHourlyLoad[] {
  try {
    return validateEvHourlyLoad(JSON.parse(readFileSync(
      process.env.EV_LOAD_HOURLY_PATH ?? DEFAULT_PATH, 'utf8')), nowMs);
  } catch {
    // No EVCC evidence is not evidence of 0 A: preserve meter-based
    // house-load history rather than subtracting an inferred vehicle load.
    return [];
  }
}
