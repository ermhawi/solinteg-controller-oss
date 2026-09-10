import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

/**
 * Regression cover for OPTIMIZER-AUDIT.md P0-2: the DP used one
 * `min(GRID_KW, BATTERY_MAX_KW) / 4` as the battery's throughput cap, the grid import cap AND the
 * grid export cap. Whichever rating is smaller silently constrained the other quantity.
 *
 * Neither direction is observable with the shipped defaults (15.36 kW battery behind an 11 kW
 * connection makes the two caps coincide for grid flows), so both hardware shapes are constructed
 * here by stubbing the env the constants read at module load — hence vi.resetModules() plus a
 * dynamic import per case, the same pattern lib/__tests__/forecast.test.ts uses for
 * SOLAR_FORECAST_MODEL.
 */

async function loadOptimizer(env: Record<string, string>) {
  vi.resetModules();
  for (const [key, value] of Object.entries(env)) vi.stubEnv(key, value);
  return import('../optimizer');
}

beforeEach(() => {
  vi.resetModules();
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

/** Battery smaller than the connection — this site (7.5 kW battery, 10 kW grid). */
const SMALL_BATTERY = {
  SOLINTEG_BATTERY_KWH: '15.055',
  SOLINTEG_BATTERY_MAX_W: '7500',
  SOLINTEG_GRID_CAP_W: '10000',
  SOLINTEG_SOC_FLOOR_PCT: '10',
};

/** Battery larger than the connection — the original reference install. */
const BIG_BATTERY = {
  SOLINTEG_BATTERY_KWH: '25.6',
  SOLINTEG_BATTERY_MAX_W: '15360',
  SOLINTEG_GRID_CAP_W: '11000',
  SOLINTEG_SOC_FLOOR_PCT: '8',
};

describe('per-slot caps: the grid limit and the battery limit are separate (P0-2)', () => {
  it('house load does not eat the charging budget: import fills the GRID cap, not the battery cap', async () => {
    const { optimizeDispatch, BATTERY_MIN_SOC_KWH } = await loadOptimizer(SMALL_BATTERY);
    // One very cheap slot then an expensive stretch, with a flat 4 kW house load (1.0 kWh/slot).
    // Physically: grid passes 2.5 kWh/slot, the house takes 1.0, so 1.5 kWh is available for the
    // battery — comfortably inside the battery's own 1.875 kWh/slot. The old combined cap allowed
    // 1.875 kWh TOTAL, so the house's 1.0 left only ~0.86 kWh for charging.
    const slots = Array.from({ length: 8 }, (_, i) => ({
      startTime: `2026-01-15T0${i}:00:00`,
      buyPrice: i === 0 ? 20 : 300,
      sellPrice: 1,
      solarKwh: 0,
      consumptionKwh: 1.0,
    }));

    const result = optimizeDispatch(slots, BATTERY_MIN_SOC_KWH);

    expect(result[0].gridKwh).toBeGreaterThan(2.4); // uses the 2.5 kWh grid budget
    expect(result[0].gridKwh).toBeLessThanOrEqual(2.5 + 1e-6);
    expect(result[0].gridToBatteryKwh).toBeGreaterThan(1.4); // vs ~0.86 before
    expect(result[0].loadFromGridKwh).toBeCloseTo(1.0, 6);
  });

  it('still respects the battery limit: stored energy per slot never exceeds its own throughput', async () => {
    const { optimizeDispatch, BATTERY_MIN_SOC_KWH, BATTERY_MAX_KW } =
      await loadOptimizer(SMALL_BATTERY);
    // Same shape as above but with NO house load in the cheap slot, so the grid budget (2.5 kWh)
    // is entirely available to the battery and its own 1.875 kWh/slot rating has to be what binds.
    // Load still exists in the later expensive slots — otherwise stored energy would have no use
    // and the DP would (correctly) refuse to charge at all.
    const slots = Array.from({ length: 8 }, (_, i) => ({
      startTime: `2026-01-15T0${i}:00:00`,
      buyPrice: i === 0 ? 20 : 300,
      sellPrice: 1,
      solarKwh: 0,
      consumptionKwh: i === 0 ? 0 : 1.0,
    }));

    const result = optimizeDispatch(slots, BATTERY_MIN_SOC_KWH);

    let soc = BATTERY_MIN_SOC_KWH;
    for (const slot of result) {
      expect(Math.abs(slot.socAfter - soc)).toBeLessThanOrEqual(BATTERY_MAX_KW / 4 + 1e-6);
      soc = slot.socAfter;
    }
    expect(result[0].gridToBatteryKwh).toBeGreaterThan(1.8); // the battery's ~1.875, not the grid's 2.5
  });

  it('mirror case: a battery bigger than the connection absorbs solar at its OWN rate', async () => {
    const { optimizeDispatch, BATTERY_MIN_SOC_KWH, BATTERY_MAX_KW, GRID_KW } =
      await loadOptimizer(BIG_BATTERY);
    expect(BATTERY_MAX_KW / 4).toBeGreaterThan(GRID_KW / 4); // precondition for this case
    // A huge solar burst, no load, empty battery: auto-charging from surplus never touches the
    // meter, so the grid cap has no business limiting it. The combined cap throttled the battery
    // to the grid's 2.75 kWh/slot and exported the difference at a poor price instead.
    const slots = [
      {
        startTime: '2026-06-15T12:00:00',
        buyPrice: 110,
        sellPrice: 25,
        solarKwh: 10,
        consumptionKwh: 0,
      },
    ];

    const result = optimizeDispatch(slots, BATTERY_MIN_SOC_KWH);

    expect(result[0].action).toBe('idle'); // free solar, not a forced charge
    const stored = result[0].socAfter - BATTERY_MIN_SOC_KWH;
    expect(stored).toBeGreaterThan(GRID_KW / 4); // > 2.75: no longer throttled by the grid
    expect(stored).toBeLessThanOrEqual(BATTERY_MAX_KW / 4 + 1e-6); // still ≤ 3.84
  });

  it('export stays bounded by the GRID cap even when the battery could deliver more', async () => {
    const { optimizeDispatch, BATTERY_KWH, GRID_KW } = await loadOptimizer(BIG_BATTERY);
    const slots = [
      {
        startTime: '2026-06-15T12:00:00',
        buyPrice: 300,
        sellPrice: 250, // worth dumping everything the meter will pass
        solarKwh: 10,
        consumptionKwh: 0,
      },
    ];

    const result = optimizeDispatch(slots, BATTERY_KWH);

    expect(-result[0].gridKwh).toBeLessThanOrEqual(GRID_KW / 4 + 1e-6);
    expect(result[0].solarExportKwh).toBeLessThanOrEqual(GRID_KW / 4 + 1e-6);
  });
});
