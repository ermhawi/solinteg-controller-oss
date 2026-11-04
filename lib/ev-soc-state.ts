/** Server-only: sanitized Volvo chart projection and advisory session inputs. */
import { readFile } from 'node:fs/promises';
type EvChargeMode = 'deadline' | 'economy'; // locally declared: /ev is not deployed in production

export interface EvSocObservedPoint {
  time: string; // Stockholm 15-minute key, second autumn fold suffixed #2
  socPct: number;
  capturedAtUtc: string; // local historian capture, NOT an OEM BMS sample
  reportedAtUtc: string; // upstream "last changed", NOT a sample timestamp
}

export interface EvSocScenario {
  socPct: number;
  targetPct: number;
  assumedUsableKwh: number;
  assumedEfficiency: number;
  chargeMode: EvChargeMode;
  departureLocalTime: string;
  maxBuyOre: number | null;
  reportedAtUtc: string;
  receivedAtUtc: string;
}

export interface EvSocChartState {
  history: EvSocObservedPoint[];
  asOfUtc: string | null;
  scenario: EvSocScenario | null;
}

function obj(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

function instant(value: unknown): number | null {
  if (typeof value !== 'string' || !/(?:Z|[+-]\d\d:\d\d)$/.test(value)) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function parseEvSocHistory(raw: unknown): EvSocObservedPoint[] {
  const value = obj(raw);
  if (value?.version !== 1 || value.source !== 'volvo_energy_api_observation' ||
      value.quality !== 'api_reported_not_verified_bms_sample' ||
      !Array.isArray(value.points) || value.points.length > 300) return [];
  const rows: EvSocObservedPoint[] = [];
  const seen = new Set<string>();
  for (const p of value.points) {
    const row = obj(p);
    if (!row || typeof row.time !== 'string' ||
        !/^\d{4}-\d\d-\d\dT\d\d:(?:00|15|30|45):00(?:#2)?$/.test(row.time) ||
        seen.has(row.time) || typeof row.socPct !== 'number' ||
        !Number.isFinite(row.socPct) || row.socPct < 0 || row.socPct > 100 ||
        instant(row.capturedAtUtc) === null || instant(row.reportedAtUtc) === null ||
        (instant(row.reportedAtUtc) ?? 0) > (instant(row.capturedAtUtc) ?? 0) + 5_000) continue;
    seen.add(row.time);
    rows.push({
      time: row.time, socPct: row.socPct,
      capturedAtUtc: row.capturedAtUtc as string,
      reportedAtUtc: row.reportedAtUtc as string,
    });
  }
  return rows;
}

export function parseEvSocScenario(raw: unknown, nowMs: number): EvSocScenario | null {
  const v = obj(raw);
  if (!v || v.source !== 'home_assistant' || v.shadow_observation_usable !== true ||
      v.energy_estimate_unverified !== true || v.physical_control_eligible !== false ||
      v.connected !== true || v.target_soc_pct !== 80 ||
      (v.charge_mode !== 'economy' && v.charge_mode !== 'deadline') ||
      typeof v.battery_soc_pct !== 'number' ||
      !Number.isFinite(v.battery_soc_pct) || v.battery_soc_pct < 0 || v.battery_soc_pct > 100 ||
      typeof v.assumed_usable_capacity_kwh !== 'number' ||
      !(v.assumed_usable_capacity_kwh >= 45 && v.assumed_usable_capacity_kwh <= 100) ||
      typeof v.assumed_ac_efficiency !== 'number' ||
      !(v.assumed_ac_efficiency >= 0.7 && v.assumed_ac_efficiency <= 1) ||
      typeof v.departure_local_time !== 'string' ||
      !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(v.departure_local_time)) return null;
  const checked = instant(v.checked_at_utc);
  const received = instant(v.energy_api_received_at_utc);
  const reported = instant(v.battery_reported_at_utc);
  if (checked === null || received === null || reported === null ||
      checked > nowMs + 5_000 || received > nowMs + 5_000 ||
      nowMs - checked > 5 * 60_000 || nowMs - received > 6 * 60_000 ||
      reported > received + 5_000) return null;
  const cap = v.max_buy_ore_per_kwh;
  if (cap !== null && cap !== undefined &&
      (typeof cap !== 'number' || !Number.isFinite(cap) || cap < -200 || cap > 3000)) return null;
  return {
    socPct: v.battery_soc_pct,
    targetPct: 80,
    assumedUsableKwh: v.assumed_usable_capacity_kwh,
    assumedEfficiency: v.assumed_ac_efficiency,
    chargeMode: v.charge_mode,
    departureLocalTime: v.departure_local_time,
    maxBuyOre: typeof cap === 'number' ? cap : null,
    reportedAtUtc: v.battery_reported_at_utc as string,
    receivedAtUtc: v.energy_api_received_at_utc as string,
  };
}

async function jsonFile(path: string): Promise<unknown> {
  try {
    return JSON.parse(await readFile(path, 'utf8'));
  } catch {
    return null; // Missing/private/unavailable observations cannot fabricate SoC.
  }
}

export async function readEvSocChartState(): Promise<EvSocChartState> {
  const [historyRaw, sessionRaw] = await Promise.all([
    jsonFile(process.env.EV_SOC_TIMELINE_PATH ||
      '/opt/solinteg/ev-shadow-runtime/var/ev-soc-timeline.json'),
    jsonFile(process.env.EV_SOC_STATUS_PATH ||
      '/opt/solinteg/ev-shadow-runtime/var/status.json'),
  ]);
  const history = parseEvSocHistory(historyRaw);
  const generated = obj(historyRaw)?.generatedAtUtc;
  return {
    history,
    asOfUtc: instant(generated) !== null ? generated as string : null,
    scenario: parseEvSocScenario(sessionRaw, Date.now()),
  };
}
