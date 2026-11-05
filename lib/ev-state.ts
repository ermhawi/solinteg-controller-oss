/** Server-only, local snapshots. Does not call HA or send physical commands. */
import { randomUUID } from 'node:crypto';
import { open, readFile, rename, unlink, lstat } from 'node:fs/promises';
import { dirname } from 'node:path';
import type { EvOverview, EvControlStatus, EvSettings, EvShadow, EvStatus } from './ev-settings';
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

export function evSettingsLiveConfigured(): boolean {
  return process.env.EV_LIVE_SETTINGS_ENABLED === '1';
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

  const rawControl = await jsonFile(
    process.env.EV_PRICE_STATUS_PATH ||
      '/opt/solinteg/ev-shadow-runtime/var/ev-price-controller-status.json',
  );
  const c = record(rawControl.data);
  const ownerStates = new Set(['none', 'pending', 'owned', 'releasing',
    'released', 'revoked', 'unknown']);
  let control: EvControlStatus | null = null;
  if (c?.schema_version === 1 && c.source === 'evcc_price_controller_status' &&
      ownerStates.has(String(c.owner_state)) &&
      typeof c.control_enabled === 'boolean' &&
      typeof c.state === 'string' && typeof c.reason === 'string' &&
      typeof c.timestamp === 'string') {
    control = {
      timestamp: c.timestamp,
      controlEnabled: c.control_enabled,
      state: c.state.slice(0,80),
      reason: c.reason.slice(0,180),
      ownerState: String(c.owner_state),
      effectiveChargeMode: c.effective_charge_mode === 'deadline' ||
        c.effective_charge_mode === 'economy' ? c.effective_charge_mode : null,
      effectiveDepartureAtUtc: stringValue(c.effective_departure_at_utc),
      effectiveSettingsRevision: stringValue(c.effective_settings_revision),
    };
  }
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
      energy_api_received_at_utc: stringValue(statusRecord.energy_api_received_at_utc),
      observation_quality: stringValue(statusRecord.observation_quality),
      timestamp_origin: stringValue(statusRecord.timestamp_origin),
      shadow_observation_usable: statusRecord.shadow_observation_usable === true,
      shadow_estimate_ready: statusRecord.shadow_estimate_ready === true,
      shadow_reason: stringValue(statusRecord.shadow_reason),
      energy_estimate_unverified: statusRecord.energy_estimate_unverified === true,
      assumed_usable_capacity_kwh: numberValue(statusRecord.assumed_usable_capacity_kwh),
      assumed_ac_efficiency: numberValue(statusRecord.assumed_ac_efficiency),
      remaining_soc_pct: numberValue(statusRecord.remaining_soc_pct),
      connected: typeof statusRecord.connected === 'boolean' ? statusRecord.connected : null,
      remaining_ac_kwh: numberValue(statusRecord.remaining_ac_kwh),
    };
  }

  // Prefer the separate, read-only HA/price scenario. It has NO EVCC
  // dependency, and physical current is always zero. The older EVCC
  // read-only monitor may supply diagnostic data when no scenario exists.
  const priceShadow = await jsonFile(
    process.env.EV_SHADOW_PLAN_PATH || '/opt/solinteg/ev-shadow-plan.json',
  );
  const rawShadow = priceShadow.data === null && priceShadow.error === null
    ? await jsonFile(process.env.EVCC_SHADOW_PATH || '/opt/solinteg/evcc-shadow.json')
    : priceShadow;
  const shadowRecord = record(rawShadow.data);
  let shadow: EvShadow | null = null;
  if (shadowRecord?.mode === 'shadow') {
    shadow = {
      timestamp: stringValue(shadowRecord.timestamp) ?? '',
      source: stringValue(shadowRecord.source),
      energy_estimate_unverified: shadowRecord.energy_estimate_unverified === true,
      observation_quality: stringValue(shadowRecord.observation_quality),
      charge_mode: shadowRecord.charge_mode === 'deadline' || shadowRecord.charge_mode === 'economy'
        ? shadowRecord.charge_mode : null,
      charging_suggested_a: numberValue(shadowRecord.charging_suggested_a),
      // This dashboard is scenario-only. Do not present an untrusted JSON
      // field as permission to actually energize the car.
      charging_permitted_a: 0,
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
    liveControlConfigured: evSettingsLiveConfigured(),
    control,
    controlFresh: fresh(control?.timestamp ?? null, 125_000),
    status,
    statusError: rawStatus.error,
    shadow,
    statusFresh: fresh(status?.checked_at_utc ?? null, 150_000),
    shadowFresh: fresh(shadow?.timestamp ?? null, 150_000),
  };
}

export class EvSettingsConflict extends Error {}
export class EvSettingsUnsafe extends Error {}

/** A browser preference is NOT a direct device command. */
export async function validateLiveEvSettingsWrite(): Promise<void> {
  if (!evSettingsLiveConfigured()) return;
  const overview = await readEvOverview();
  const control = overview.control;
  if (!overview.controlFresh || !control?.controlEnabled ||
      control.state === 'error' || control.state === 'disabled' ||
      (control.ownerState !== 'none' && control.ownerState !== 'released')) {
    throw new EvSettingsUnsafe('Laddcontrollerns pausstatus är osäker. Inget laddläge ändrades.');
  }
  const path = process.env.EVCC_READONLY_PATH ||
    '/opt/solinteg/ev-shadow-runtime/var/evcc-readonly.json';
  const { data, error } = await jsonFile(path);
  const evcc = record(data);
  const timestamp = typeof evcc?.timestamp === 'string' ? evcc.timestamp : null;
  if (error || !fresh(timestamp, 65_000) ||
      evcc?.source !== 'waveshare_evcc_fc03_readonly' ||
      evcc?.read_ok !== true || evcc?.writes !== 0 ||
      evcc?.physical_control_eligible !== false ||
      !Array.isArray(evcc?.channel_currents_a) ||
      evcc.channel_currents_a.length !== 3 ||
      !evcc.channel_currents_a.every((current) =>
        typeof current === 'number' && Number.isFinite(current) &&
        current >= 0 && current <= 6.5)) {
    throw new EvSettingsUnsafe('EVCC:s aktuella kabelstatus kan inte verifieras.');
  }
  const currents = evcc.channel_currents_a as number[];
  const connectedCharging = evcc.state === 'C2' &&
    evcc.configured_pwm_raw === 100 && evcc.pilot_pwm_raw === 100 &&
    currents[1] >= 5.5;
  const safelyIdle = (evcc.state === 'A1' || evcc.state === 'B1' ||
    evcc.state === 'B2') &&
    evcc.configured_pwm_raw === 100 &&
    currents.every((current) => current <= 0.5);
  const unplugged = evcc.state === 'A1' &&
    currents.every((current) => current <= 0.5);
  if (!connectedCharging && !safelyIdle && !unplugged) {
    throw new EvSettingsUnsafe(
      'Laddaren är pausad eller byter tillstånd. Vänta till 6 A-laddning eller urkopplad bil.',
    );
  }
}

export async function saveEvSettings(value: EvSettings): Promise<EvSettings> {
  // This path is configured by the operator in a DEDICATED shared settings
  // directory, never the protected EVCC runtime directory.
  const path = settingsPath();
  const lockPath = path + '.write.lock';
  let lock;
  try {
    lock = await open(lockPath, 'wx', 0o600);
  } catch (error) {
    if (error && typeof error === 'object' && 'code' in error &&
        error.code === 'EEXIST') {
      throw new EvSettingsConflict('En annan ändring av laddläget pågår.');
    }
    throw error;
  }
  try {
    const previousFile = await jsonFile(path);
    const previous = previousFile.data === null && previousFile.error === null
      ? DEFAULT_EV_SETTINGS : parseEvSettings(previousFile.data);
    if (!previous || previousFile.error) {
      throw new EvSettingsUnsafe('Befintliga laddinställningar är ogiltiga.');
    }
    try {
      const oldInfo = await lstat(path);
      if (!oldInfo.isFile() || oldInfo.isSymbolicLink()) {
        throw new EvSettingsUnsafe('Inställningsfilen är inte en vanlig fil.');
      }
    } catch (error) {
      if (!(error && typeof error === 'object' && 'code' in error &&
        error.code === 'ENOENT')) throw error;
    }
    if ((previous.revision ?? null) !== (value.revision ?? null)) {
      throw new EvSettingsConflict('Laddinställningarna ändrades i en annan flik. Uppdatera sidan.');
    }
    if (previous.charge_mode === value.charge_mode &&
        previous.departure_local_time === value.departure_local_time &&
        previous.max_buy_ore_per_kwh === value.max_buy_ore_per_kwh) {
      return previous;
    }
    // Re-read physical state after acquiring the per-settings write lock.
    await validateLiveEvSettingsWrite();
    const next: EvSettings = {
      charge_mode: value.charge_mode,
      departure_local_time: value.departure_local_time,
      max_buy_ore_per_kwh: value.max_buy_ore_per_kwh,
      revision: randomUUID(),
    };
    const tmp = path + '.' + randomUUID() + '.tmp';
    try {
      const file = await open(tmp, 'wx', 0o640);
      try {
        await file.writeFile(JSON.stringify(next, null, 2) + '\n', 'utf8');
        await file.chmod(0o640);
        await file.sync();
      } finally {
        await file.close();
      }
      await rename(tmp, path);
      const dir = await open(dirname(path), 'r');
      try { await dir.sync(); } finally { await dir.close(); }
    } catch (error) {
      await unlink(tmp).catch(() => {});
      throw error;
    }
    return next;
  } finally {
    await lock.close();
    await unlink(lockPath).catch(() => {});
  }
}
