/** Server-only, local snapshots. Does not call HA or send physical commands. */
import { randomUUID } from 'node:crypto';
import { readFile, rename, unlink, writeFile } from 'node:fs/promises';
import type { EvOverview, EvSettings, EvShadow, EvStatus } from './ev-settings';
import { DEFAULT_EV_SETTINGS, parseEvSettings } from './ev-settings';

function settingsPath() {
  return process.env.EV_SETTINGS_PATH || '/opt/solinteg/ev-settings.json';
}

async function jsonFile(path: string): Promise<{ data: unknown; error: string | null }> {
  try {
    return { data: JSON.parse(await readFile(path, 'utf8')), error: null };
  } catch (err) {
    if (err && typeof err === 'object' && 'code' in err && err.code === 'ENOENT') {
      return { data: null, error: null };
    }
    return { data: null, error: 'Ogiltig eller oläsbar lokal JSON-fil' };
  }
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}
function stringValue(x: unknown): string | null {
  return typeof x === 'string' && x.length <= 240 ? x : null;
}
function numberValue(x: unknown): number | null {
  return typeof x === 'number' && Number.isFinite(x) ? x : null;
}
function fresh(value: string | null, maxAgeMs: number): boolean {
  if (!value) return false;
  const delta = Date.now() - Date.parse(value);
  return Number.isFinite(delta) && delta >= -5000 && delta <= maxAgeMs;
}

export function evSettingsWriteEnabled(): boolean {
  return (
    process.env.EV_SHADOW_SETTINGS_WRITES_ENABLED === '1' &&
    (process.env.EV_SHADOW_SETTINGS_WRITE_TOKEN?.length ?? 0) >= 32
  );
}

export async function readEvOverview(): Promise<EvOverview> {
  const prefs = await jsonFile(settingsPath());
  const parsedSettings = prefs.data === null ? DEFAULT_EV_SETTINGS : parseEvSettings(prefs.data);
  const settingsError = prefs.error || (!parsedSettings ? 'Inställningsfilen är ogiltig' : null);

  const rawStatus = await jsonFile(
    process.env.EV_STATUS_PATH || '/opt/solinteg/ev-session-status.json',
  );
  const statusRecord = record(rawStatus.data);
  let status: EvStatus | null = null;
  if (statusRecord?.source === 'home_assistant') {
    status = {
      source: 'home_assistant',
      checked_at_utc: stringValue(statusRecord.checked_at_utc) ?? '',
      valid_for_planning: statusRecord.valid_for_planning === true,
      reason: stringValue(statusRecord.reason),
      battery_soc_pct: numberValue(statusRecord.battery_soc_pct),
      battery_reported_at_utc: stringValue(statusRecord.battery_reported_at_utc),
      connected: typeof statusRecord.connected === 'boolean' ? statusRecord.connected : null,
      remaining_ac_kwh: numberValue(statusRecord.remaining_ac_kwh),
    };
  }

  const rawShadow = await jsonFile(
    process.env.EVCC_SHADOW_PATH || '/opt/solinteg/evcc-shadow.json',
  );
  const shadowRecord = record(rawShadow.data);
  let shadow: EvShadow | null = null;
  if (shadowRecord?.mode === 'shadow') {
    shadow = {
      timestamp: stringValue(shadowRecord.timestamp) ?? '',
      charge_mode: shadowRecord.charge_mode === 'deadline' || shadowRecord.charge_mode === 'economy'
        ? shadowRecord.charge_mode : null,
      charging_suggested_a: numberValue(shadowRecord.charging_suggested_a),
      charging_permitted_a: numberValue(shadowRecord.charging_permitted_a) ?? 0,
      grid_topup_suggested_a: numberValue(shadowRecord.grid_topup_suggested_a) ?? 0,
      price_buy_ore_kwh: numberValue(shadowRecord.price_buy_ore_kwh),
      price_cheap_threshold_ore_kwh: numberValue(shadowRecord.price_cheap_threshold_ore_kwh),
      price_reason: stringValue(shadowRecord.price_reason),
      writes: numberValue(shadowRecord.writes) ?? 0,
    };
  }

  return {
    settings: parsedSettings ?? DEFAULT_EV_SETTINGS,
    settingsError,
    settingsWriteEnabled: evSettingsWriteEnabled(),
    status,
    statusError: rawStatus.error,
    shadow,
    statusFresh: fresh(status?.checked_at_utc ?? null, 150_000),
    shadowFresh: fresh(shadow?.timestamp ?? null, 150_000),
  };
}

export async function saveEvSettings(value: EvSettings): Promise<void> {
  // Only callable after the API route's secret-authentication gate.
  const path = settingsPath();
  const tmp = path + '.' + randomUUID() + '.tmp';
  try {
    await writeFile(tmp, JSON.stringify(value, null, 2) + '\n', {
      encoding: 'utf8', mode: 0o600, flag: 'wx',
    });
    await rename(tmp, path);
  } catch (error) {
    await unlink(tmp).catch(() => {});
    throw error;
  }
}
