/** Server-only, authenticated read of a separate EV cost report. No device/DB writes. */
import { createHash, timingSafeEqual } from 'node:crypto';
import { readFile } from 'node:fs/promises';

export type CostQuality = 'metered' | 'estimated' | 'partial' | 'unavailable';

export type DrivingCost = {
  event_id: string;
  occurred_at_utc: string;
  started_at_utc: string | null;
  distance_km: number;
  consumed_dc_kwh: number;
  known_cost_ore: number;
  cost_ore: number | null;
  unpriced_dc_kwh: number;
  kr_per_mil: number | null;
  quality: CostQuality;
};

type CostSummary = {
  distance_km: number;
  consumed_dc_kwh: number;
  known_cost_ore: number;
  cost_ore: number | null;
  unpriced_dc_kwh: number;
  kr_per_mil: number | null;
  quality: CostQuality;
};

type CostInventory = {
  stored_dc_kwh: number;
  known_value_ore: number;
  inventory_value_ore: number | null;
  unpriced_dc_kwh: number;
  average_ore_per_dc_kwh: number | null;
  quality: CostQuality;
};

export type EvCostReport = {
  schema_version: 1;
  source: 'ev_cost_ledger';
  as_of_utc: string;
  ledger_revision: string;
  model: 'weighted_average_stored_dc_v1';
  inventory: CostInventory;
  summary: CostSummary;
  charged_ac_kwh_by_source: Record<'grid' | 'solar' | 'public' | 'unknown', number>;
  aux_consumed_dc_kwh: number;
  direct_aux_ac_kwh: number;
  direct_aux_known_cost_ore: number;
  direct_aux_cost_ore: number | null;
  direct_aux_unpriced_ac_kwh: number;
  event_count: number;
  driving: DrivingCost[];
  writes_to_devices: 0;
};

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}
function finite(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}
function quality(value: unknown): value is CostQuality {
  return value === 'metered' || value === 'estimated'
    || value === 'partial' || value === 'unavailable';
}
function nullableNumber(value: unknown): value is number | null {
  return value === null || finite(value);
}
function validSummary(value: unknown): value is CostSummary {
  const s = record(value);
  return s !== null
    && finite(s.distance_km) && s.distance_km >= 0
    && finite(s.consumed_dc_kwh) && s.consumed_dc_kwh >= 0
    && finite(s.known_cost_ore) && nullableNumber(s.cost_ore)
    && finite(s.unpriced_dc_kwh) && s.unpriced_dc_kwh >= 0
    && nullableNumber(s.kr_per_mil) && quality(s.quality);
}
function validDrive(value: unknown): value is DrivingCost {
  const row = record(value);
  if (row === null || typeof row.event_id !== 'string'
      || row.event_id.length > 100 || typeof row.occurred_at_utc !== 'string'
      || !Number.isFinite(Date.parse(row.occurred_at_utc))) return false;
  const start = row.started_at_utc;
  if (start !== null && (typeof start !== 'string'
      || !Number.isFinite(Date.parse(start))
      || Date.parse(start) > Date.parse(row.occurred_at_utc))) return false;
  return validSummary(row) && row.distance_km > 0;
}
function validReport(value: unknown): value is EvCostReport {
  const r = record(value);
  const inventory = record(r?.inventory);
  const sources = record(r?.charged_ac_kwh_by_source);
  return r !== null && r.schema_version === 1 && r.source === 'ev_cost_ledger'
    && r.model === 'weighted_average_stored_dc_v1'
    && r.writes_to_devices === 0
    && typeof r.as_of_utc === 'string' && Number.isFinite(Date.parse(r.as_of_utc))
    && typeof r.ledger_revision === 'string' && /^[a-f0-9]{64}$/.test(r.ledger_revision)
    && Number.isSafeInteger(r.event_count) && (r.event_count as number) >= 0
    && validSummary(r.summary)
    && inventory !== null
    && finite(inventory.stored_dc_kwh) && inventory.stored_dc_kwh >= 0
    && finite(inventory.known_value_ore)
    && nullableNumber(inventory.inventory_value_ore)
    && finite(inventory.unpriced_dc_kwh) && inventory.unpriced_dc_kwh >= 0
    && nullableNumber(inventory.average_ore_per_dc_kwh)
    && quality(inventory.quality)
    && sources !== null
    && (['grid', 'solar', 'public', 'unknown'] as const)
      .every((source) => finite(sources[source]) && (sources[source] as number) >= 0)
    && finite(r.aux_consumed_dc_kwh) && (r.aux_consumed_dc_kwh as number) >= 0
    && finite(r.direct_aux_ac_kwh) && (r.direct_aux_ac_kwh as number) >= 0
    && finite(r.direct_aux_known_cost_ore)
    && nullableNumber(r.direct_aux_cost_ore)
    && finite(r.direct_aux_unpriced_ac_kwh) && (r.direct_aux_unpriced_ac_kwh as number) >= 0
    && Array.isArray(r.driving) && r.driving.length <= 10000
    && r.driving.every(validDrive);
}

export function evCostEnabled(): boolean {
  return (process.env.EV_COST_READ_TOKEN?.length ?? 0) >= 32
    && Boolean(process.env.EV_COST_REPORT_PATH);
}

export function evCostAuthorized(request: Request): boolean {
  if (!evCostEnabled()) return false;
  const value = request.headers.get('authorization');
  if (!value?.startsWith('Bearer ')) return false;
  const provided = value.slice(7);
  const expected = process.env.EV_COST_READ_TOKEN ?? '';
  const digest = (text: string) => createHash('sha256').update(text).digest();
  return timingSafeEqual(digest(provided), digest(expected));
}

export async function readEvCostReport(): Promise<EvCostReport | null> {
  const path = process.env.EV_COST_REPORT_PATH;
  if (!path || !evCostEnabled()) return null;
  try {
    const content = await readFile(path, 'utf8');
    if (Buffer.byteLength(content) > 2_000_000) return null;
    const data: unknown = JSON.parse(content);
    return validReport(data) ? data : null;
  } catch {
    return null;
  }
}

export function costResponse(payload: unknown, status = 200): Response {
  return Response.json(payload, {
    status,
    headers: { 'Cache-Control': 'private, no-store, max-age=0' },
  });
}

export function costAuthFailure(request: Request): Response | null {
  if (!evCostEnabled()) return costResponse({ error: 'EV cost API not configured' }, 503);
  if (!evCostAuthorized(request)) return costResponse({ error: 'Unauthorized' }, 401);
  return null;
}

export function drivingSummary(rows: DrivingCost[]): CostSummary {
  const distance = rows.reduce((s, row) => s + row.distance_km, 0);
  const energy = rows.reduce((s, row) => s + row.consumed_dc_kwh, 0);
  const known = rows.reduce((s, row) => s + row.known_cost_ore, 0);
  const unknown = rows.reduce((s, row) => s + row.unpriced_dc_kwh, 0);
  const full = distance > 0 && rows.every((row) => row.cost_ore !== null);
  const amount = (n: number) => Math.round((n + Number.EPSILON) * 10000) / 10000;
  return {
    distance_km: amount(distance),
    consumed_dc_kwh: amount(energy),
    known_cost_ore: amount(known),
    cost_ore: full ? amount(known) : null,
    unpriced_dc_kwh: amount(unknown),
    kr_per_mil: full ? Math.round((known / (distance / 10) / 100) * 100) / 100 : null,
    quality: distance === 0 ? 'unavailable'
      : !full ? 'partial'
        : rows.some((row) => row.quality === 'estimated') ? 'estimated' : 'metered',
  };
}

/** Timestamp filters are UTC instants, not Stockholm-local ambiguous clock times. */
export function timeRange(request: Request): { from: number; to: number } | null {
  const params = new URL(request.url).searchParams;
  if ([...params.keys()].some((key) => key !== 'from' && key !== 'to')) return null;
  if (params.getAll('from').length > 1 || params.getAll('to').length > 1) return null;
  const parse = (value: string | null, fallback: number) => {
    if (value === null) return fallback;
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/.test(value)) {
      return NaN;
    }
    return Date.parse(value);
  };
  const from = parse(params.get('from'), -Infinity);
  const to = parse(params.get('to'), Infinity);
  return !Number.isNaN(from) && !Number.isNaN(to) && from < to
    ? { from, to } : null;
}
