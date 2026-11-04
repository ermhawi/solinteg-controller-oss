// EVCC FC03 reports *measured current*, not active power or the source of that power.
// The site's L3 CT measures net grid exchange, not a dedicated EV energy meter.
export interface EvccCarState {
  timestamp: string;
  state: string;
  connected: boolean;
  charging: boolean;
  currentA: number | null;
  powerEstimateW: number | null; // Ict2 × nominal 230 V, PF assumed 1
}

export interface CarSupplyBounds {
  gridToCarUpperW: number;
  localToCarLowerW: number; // PV + battery; their individual contributions are unknown
  powerEstimateW: number;
}

// A corrupted inverter sample must never turn one unreliable CT value into a
// confident car-source arrow. Same independent telemetry coherence bounds as
// the live EV policy; these affect presentation only, never the raw counters.
export function carFlowTelemetryConsistent(live: {
  pv_w: number;
  battery_w: number;
  inverter_ac_w: number;
  grid_w: number;
  house_load_w: number;
  meter_l1_w?: number;
  meter_l2_w?: number;
  meter_l3_w?: number;
  grid_unbalanced_output_raw?: number;
} | null): boolean {
  if (!live || live.grid_unbalanced_output_raw !== 0) return false;
  const { pv_w, battery_w, inverter_ac_w, grid_w, house_load_w,
    meter_l1_w, meter_l2_w, meter_l3_w } = live;
  if ([pv_w, battery_w, inverter_ac_w, grid_w, house_load_w,
    meter_l1_w, meter_l2_w, meter_l3_w].some(
    (value) => typeof value !== 'number' || !Number.isFinite(value)
  )) return false;
  const phases = (meter_l1_w as number) + (meter_l2_w as number) + (meter_l3_w as number);
  return pv_w >= 0 && house_load_w >= 0
    && Math.abs(phases - grid_w) <= Math.max(80, Math.abs(grid_w) * 0.05)
    && Math.abs(inverter_ac_w - grid_w - house_load_w) <= Math.max(150, house_load_w * 0.10)
    && Math.abs(inverter_ac_w - (pv_w + battery_w))
      <= 500 + 0.10 * Math.max(Math.abs(inverter_ac_w), pv_w, Math.abs(battery_w));
}

// Mutually exclusive *status indication*, never proof of an exclusive physical source.
export type CarSupplyRoute = 'grid' | 'local' | 'unknown';
const GRID_ROUTE_TOLERANCE_W = 125; // nominal voltage/PF and CT measurement uncertainty

export function carSupplyRoute(bounds: CarSupplyBounds | null): CarSupplyRoute {
  if (!bounds) return 'unknown';
  return bounds.localToCarLowerW <= GRID_ROUTE_TOLERANCE_W ? 'grid' : 'local';
}

// The inverter's house_load_w includes the EV. A separate car node must not double-count it.
export function houseWithoutCarW(houseLoadW: number, car: EvccCarState | null): number {
  if (!Number.isFinite(houseLoadW)) return houseLoadW;
  return Math.max(0, houseLoadW - (car?.charging ? car.powerEstimateW ?? 0 : 0));
}

function validCurrent(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}

export function parseEvccCarState(input: unknown, nowMs: number): EvccCarState | null {
  if (!input || typeof input !== 'object' || Array.isArray(input)) return null;
  const e = input as Record<string, unknown>;
  const ageMs = nowMs - Date.parse(String(e.timestamp ?? ''));
  if (!Number.isFinite(ageMs) || ageMs < -5_000 || ageMs > 65_000
      || e.source !== 'waveshare_evcc_fc03_readonly'
      || e.read_ok !== true || e.writes !== 0
      || typeof e.state !== 'string') return null;

  const connected = e.state === 'B1' || e.state === 'B2' || e.state === 'C1' || e.state === 'C2';
  if (e.state !== 'C2') {
    if (!['A1', 'A2', 'B1', 'B2'].includes(e.state) || e.charging_detected !== false) return null;
    return { timestamp: e.timestamp as string, state: e.state, connected, charging: false,
      currentA: null, powerEstimateW: null };
  }

  const channels = e.channel_currents_a;
  if (e.charging_detected !== true
      || !Array.isArray(channels) || channels.length !== 3
      || !channels.every(validCurrent)
      || channels[0] > 0.5 || channels[2] > 0.5
      || channels[1] < 5 || channels[1] > 18.5
      || !validCurrent(e.measured_current_a)
      || Math.abs(channels.reduce((sum: number, a: number) => sum + a, 0) - e.measured_current_a) > 0.21
      || !Number.isInteger(e.configured_pwm_raw) || e.configured_pwm_raw !== e.pilot_pwm_raw
      || (e.configured_pwm_raw as number) < 98 || (e.configured_pwm_raw as number) > 300
      || channels[1] > (e.configured_pwm_raw as number) * 0.06 + 0.5) return null;

  return { timestamp: e.timestamp as string, state: e.state, connected: true,
    charging: true, currentA: channels[1], powerEstimateW: Math.round(channels[1] * 230) };
}

export function carSupplyBounds(car: EvccCarState | null, gridL3W: number | null | undefined): CarSupplyBounds | null {
  if (!car?.charging || car.powerEstimateW === null || car.powerEstimateW <= 0
      || typeof gridL3W !== 'number' || !Number.isFinite(gridL3W)) return null;
  const gridToCarUpperW = Math.min(car.powerEstimateW, Math.max(0, -gridL3W));
  return {
    gridToCarUpperW,
    localToCarLowerW: car.powerEstimateW - gridToCarUpperW,
    powerEstimateW: car.powerEstimateW,
  };
}
