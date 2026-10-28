/** Two EV intentions; this file contains no charge, inverter or HA commands. */
export type EvChargeMode = 'deadline' | 'economy';

export interface EvSettings {
  charge_mode: EvChargeMode;
  departure_local_time: string;
  max_buy_ore_per_kwh: number | null;
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
  const allowed = new Set(Object.keys(DEFAULT_EV_SETTINGS));
  if (Object.keys(value).some((key) => !allowed.has(key))) return null;

  const charge_mode = value.charge_mode ?? DEFAULT_EV_SETTINGS.charge_mode;
  const departure_local_time =
    value.departure_local_time ?? DEFAULT_EV_SETTINGS.departure_local_time;
  const max_buy_ore_per_kwh = value.max_buy_ore_per_kwh ?? null;

  if (charge_mode !== 'deadline' && charge_mode !== 'economy') return null;
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

  return { charge_mode, departure_local_time, max_buy_ore_per_kwh };
}

export interface EvStatus {
  checked_at_utc: string;
  valid_for_planning: boolean;
  reason: string | null;
  battery_soc_pct: number | null;
  battery_reported_at_utc: string | null;
  connected: boolean | null;
  remaining_ac_kwh: number | null;
  source: 'home_assistant';
}

export interface EvShadow {
  timestamp: string;
  charge_mode: EvChargeMode | null;
  charging_suggested_a: number | null;
  charging_permitted_a: number;
  grid_topup_suggested_a: number;
  price_buy_ore_kwh: number | null;
  price_cheap_threshold_ore_kwh: number | null;
  price_reason: string | null;
  writes: number;
}

export interface EvOverview {
  settings: EvSettings;
  settingsError: string | null;
  settingsWriteEnabled: boolean;
  status: EvStatus | null;
  statusError: string | null;
  shadow: EvShadow | null;
  statusFresh: boolean;
  shadowFresh: boolean;
}
