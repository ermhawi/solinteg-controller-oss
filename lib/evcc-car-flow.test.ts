import { describe, expect, it } from 'vitest';
import { carFlowTelemetryConsistent, carSupplyBounds, carSupplyRoute, houseWithoutCarW, parseEvccCarState } from './evcc-car-flow';

const now = Date.parse('2026-11-04T11:00:00Z');
const charging = {
  timestamp: '2026-11-04T10:59:30Z',
  source: 'waveshare_evcc_fc03_readonly',
  read_ok: true,
  writes: 0,
  state: 'C2',
  charging_detected: true,
  configured_pwm_raw: 100,
  pilot_pwm_raw: 100,
  channel_currents_a: [0.1, 6, 0.1],
  measured_current_a: 6.2,
};

describe('EVCC live car energy paths', () => {
  it('rejects incoherent AC/PV/battery samples rather than showing a false local arrow', () => {
    const valid = {
      pv_w: 531, battery_w: -329, inverter_ac_w: 234, grid_w: -2397,
      house_load_w: 2631, meter_l1_w: -169, meter_l2_w: -130,
      meter_l3_w: -2097, grid_unbalanced_output_raw: 0,
    };
    const inconsistent = {
      pv_w: 493, battery_w: -537, inverter_ac_w: 1957, grid_w: 42,
      house_load_w: 1915, meter_l1_w: 405, meter_l2_w: 437,
      meter_l3_w: -800, grid_unbalanced_output_raw: 0,
    };
    expect(carFlowTelemetryConsistent(valid)).toBe(true);
    expect(carFlowTelemetryConsistent(inconsistent)).toBe(false);
    expect(carFlowTelemetryConsistent({ ...valid, meter_l2_w: 100 })).toBe(false);
    expect(carFlowTelemetryConsistent({ ...valid, grid_unbalanced_output_raw: 1 })).toBe(false);
    expect(carFlowTelemetryConsistent({ ...valid, meter_l3_w: undefined })).toBe(false);
    expect(carFlowTelemetryConsistent(null)).toBe(false);
    const car = parseEvccCarState(charging, now);
    expect(carSupplyRoute(carSupplyBounds(car,
      carFlowTelemetryConsistent(inconsistent) ? inconsistent.meter_l3_w : undefined
    ))).toBe('unknown');
  });

  it('uses measured L3 current and nominal voltage, never the configured limit as actual power', () => {
    const car = parseEvccCarState(charging, now);
    expect(car).toMatchObject({ connected: true, charging: true, currentA: 6, powerEstimateW: 1380 });
    expect(carSupplyBounds(car, -550)).toEqual({
      gridToCarUpperW: 550, localToCarLowerW: 830, powerEstimateW: 1380,
    });
  });

  it('shows only conservative grid upper/local lower bounds, never exact battery provenance', () => {
    const car = parseEvccCarState(charging, now);
    expect(carSupplyBounds(car, -2000)).toMatchObject({ gridToCarUpperW: 1380, localToCarLowerW: 0 });
    expect(carSupplyBounds(car, 240)).toMatchObject({ gridToCarUpperW: 0, localToCarLowerW: 1380 });
    expect(carSupplyBounds(car, 0)).toMatchObject({ gridToCarUpperW: 0, localToCarLowerW: 1380 });
    expect(carSupplyBounds(car, undefined)).toBeNull();
    expect(carSupplyBounds(car, Number.NaN)).toBeNull();
  });

  it('uses the actual Ict2 when the pilot supports 16 A', () => {
    const car = parseEvccCarState({ ...charging,
      configured_pwm_raw: 267, pilot_pwm_raw: 267,
      channel_currents_a: [0.1, 10, 0.1], measured_current_a: 10.2,
    }, now);
    expect(car?.powerEstimateW).toBe(2300);
  });

  it('keeps a connected but paused EV distinct from a charging EV', () => {
    const car = parseEvccCarState({ ...charging, state: 'B2',
      charging_detected: false, configured_pwm_raw: 1000, pilot_pwm_raw: 267,
      channel_currents_a: [null, null, null], measured_current_a: null,
    }, now);
    expect(car).toMatchObject({ state: 'B2', connected: true, charging: false,
      currentA: null, powerEstimateW: null });
    expect(carSupplyBounds(car, -1000)).toBeNull();
  });

  it('represents unplugged A1 as not connected, without inventing 0 measured amps', () => {
    const car = parseEvccCarState({ ...charging, state: 'A1', charging_detected: false }, now);
    expect(car).toMatchObject({ connected: false, charging: false, powerEstimateW: null });
  });

  it('fails closed on stale, missing, failed, or unexpected readings', () => {
    const invalid = [
      { ...charging, timestamp: '2026-11-04T10:58:54Z' },
      { ...charging, timestamp: '2026-11-04T11:00:10Z' },
      { ...charging, source: 'unverified' },
      { ...charging, read_ok: false },
      { ...charging, writes: 1 },
      { ...charging, configured_pwm_raw: 1000 },
      { ...charging, pilot_pwm_raw: 267 },
      { ...charging, state: 'C2', charging_detected: false },
      { ...charging, channel_currents_a: [6, 0.1, 0.1] },
      { ...charging, channel_currents_a: [null, 6, 0.1] },
      { ...charging, measured_current_a: 10 },
      { ...charging, channel_currents_a: [0.1, 12, 0.1], measured_current_a: 12.2 },
    ];
    for (const reading of invalid) expect(parseEvccCarState(reading, now)).toBeNull();
  });

  it('activates exactly one supply indicator or no indicator when unknown', () => {
    const car = parseEvccCarState(charging, now);
    expect(carSupplyRoute(carSupplyBounds(car, -1800))).toBe('grid');
    expect(carSupplyRoute(carSupplyBounds(car, -1350))).toBe('grid'); // ± measurement tolerance
    expect(carSupplyRoute(carSupplyBounds(car, -550))).toBe('local');
    expect(carSupplyRoute(carSupplyBounds(car, 100))).toBe('local');
    expect(carSupplyRoute(carSupplyBounds(car, undefined))).toBe('unknown');
    expect(carSupplyRoute(null)).toBe('unknown');
  });

  it('shows house load excluding EV without changing grid or site totals', () => {
    const car = parseEvccCarState(charging, now);
    expect(houseWithoutCarW(2400, car)).toBe(1020);
    expect(houseWithoutCarW(2400, car) + car!.powerEstimateW!).toBe(2400);
    expect(houseWithoutCarW(900, car)).toBe(0);
    expect(houseWithoutCarW(2400, null)).toBe(2400);
    const paused = parseEvccCarState({
      ...charging, state: 'B2', charging_detected: false,
    }, now);
    expect(houseWithoutCarW(2400, paused)).toBe(2400);
  });

  it('marks a missing EVCC record as unknown, never as disconnected', () => {
    expect(parseEvccCarState(null, now)).toBeNull();
    expect(parseEvccCarState({}, now)).toBeNull();
    expect(carSupplyBounds(null, -1200)).toBeNull();
  });
});
