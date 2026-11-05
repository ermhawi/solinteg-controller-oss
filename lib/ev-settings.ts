/** Two EV intentions; this file contains no charge, inverter or HA commands. */
export type EvChargeMode = 'deadline' | 'economy';

export interface EvSettings {
  charge_mode: EvChargeMode;
  departure_local_time: string;
  max_buy_ore_per_kwh: number | null;
  revision?: string; // server-generated optimistic concurrency / mission generation
}

export const DEFAULT_EV_SETTINGS: EvSettings = {
  charge_mode: 'deadline',
  departure_local_time: '07:00',
  max_buy_ore_per_kwh: null,
};

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

export function parseEvSettings(value: unknown): EvSettings | null {
  if (!isObject(value)) return null;
  const allowed = new Set([...Object.keys(DEFAULT_EV_SETTINGS), 'revision']);
  if (Object.keys(value).some((key) => !allowed.has(key))) return null;

  const charge_mode = value.charge_mode ?? DEFAULT_EV_SETTINGS.charge_mode;
  const departure_local_time =
    value.departure_local_time ?? DEFAULT_EV_SETTINGS.departure_local_time;
  const max_buy_ore_per_kwh = value.max_buy_ore_per_kwh ?? null;
  const revision = value.revision;

  if (charge_mode !== 'deadline' && charge_mode !== 'economy') return null;
  if (revision !== undefined && (typeof revision !== 'string' ||
    !/^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(revision))) return null;
  if (
    typeof departure_local_time !== 'string' ||
    !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(departure_local_time)
  ) return null;
  if (
    max_buy_ore_per_kwh !== null &&
    (typeof max_buy_ore_per_kwh !== 'number' ||
      !Number.isFinite(max_buy_ore_per_kwh) ||
      max_buy_ore_per_kwh < -200 ||
      max_buy_ore_per_kwh > 3000)
  ) return null;

  return { charge_mode, departure_local_time, max_buy_ore_per_kwh,
    ...(revision === undefined ? {} : { revision }) };
}

export interface EvStatus {
  checked_at_utc: string;
  valid_for_planning: boolean;
  reason: string | null;
  battery_soc_pct: number | null;
  battery_reported_at_utc: string | null;
  energy_api_received_at_utc: string | null;
  observation_quality: string | null;
  timestamp_origin: string | null;
  shadow_observation_usable: boolean;
  shadow_estimate_ready: boolean;
  shadow_reason: string | null;
  energy_estimate_unverified: boolean;
  assumed_usable_capacity_kwh: number | null;
  assumed_ac_efficiency: number | null;
  remaining_soc_pct: number | null;
  connected: boolean | null;
  remaining_ac_kwh: number | null;
  source: 'home_assistant';
}

export interface EvShadow {
  timestamp: string;
  source: string | null;
  energy_estimate_unverified: boolean;
  observation_quality: string | null;
  charge_mode: EvChargeMode | null;
  charging_suggested_a: number | null;
  charging_permitted_a: number;
  grid_topup_suggested_a: number;
  price_buy_ore_kwh: number | null;
  price_cheap_threshold_ore_kwh: number | null;
  price_reason: string | null;
  writes: number;
}

export interface EvControlStatus {
  timestamp: string;
  controlEnabled: boolean;
  state: string;
  reason: string;
  ownerState: string;
  effectiveChargeMode: EvChargeMode | null;
  effectiveDepartureAtUtc: string | null;
  effectiveSettingsRevision: string | null;
}

export interface EvEndedSession {
  confirmedAtUtc: string;
  lastChargingAtUtc: string;
  socPct: number | null;
  socApiReceivedAtUtc: string | null;
  socOemLastChangedAtUtc: string | null;
  quality: 'polled_cable_and_api_reported_soc_not_bms';
}

export interface EvOverview {
  settings: EvSettings;
  settingsError: string | null;
  settingsWriteEnabled: boolean;
  liveControlConfigured: boolean;
  control: EvControlStatus | null;
  controlFresh: boolean;
  lastEndedSession: EvEndedSession | null;
  status: EvStatus | null;
  statusError: string | null;
  shadow: EvShadow | null;
  statusFresh: boolean;
  shadowFresh: boolean;
}
