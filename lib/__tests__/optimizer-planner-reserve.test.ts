import { afterEach, describe, expect, it, vi } from 'vitest';
import type { OptimizerSlot } from '../optimizer';

const batteryKwh = 15.055;
const physicalFloorKwh = batteryKwh * 0.12;
const reserveKwh = batteryKwh * 0.2;

async function siteOptimizer(reservePct?: string) {
  vi.resetModules();
  vi.stubEnv('SOLINTEG_BATTERY_KWH', String(batteryKwh));
  vi.stubEnv('SOLINTEG_BATTERY_MAX_W', '7500');
  vi.stubEnv('SOLINTEG_GRID_CAP_W', '10000');
  vi.stubEnv('SOLINTEG_SOC_FLOOR_PCT', '12');
  vi.stubEnv('SOLINTEG_PLANNER_RESERVE_PCT', reservePct ?? '');
  const optimizer = await import('../optimizer');
  const constants = await import('../constants');
  return { optimizer, constants };
}

function slot(i: number, buy: number, consumption = 0.3): OptimizerSlot {
  const h = String(Math.floor(i / 4)).padStart(2, '0');
  const m = String((i % 4) * 15).padStart(2, '0');
  return {
    startTime: '2026-10-27T' + h + ':' + m + ':00',
    buyPrice: buy,
    sellPrice: 10,
    solarKwh: 0,
    consumptionKwh: consumption,
    priceSource: 'actual',
  };
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe('separate Sundsvall planner reserve from physical BMS floor', () => {
  it('keeps hard floor 12%, planning at 20%', async () => {
    const { optimizer, constants } = await siteOptimizer('20');
    expect(constants.BATTERY_MIN_SOC_KWH).toBeCloseTo(physicalFloorKwh, 6);
    expect(constants.BATTERY_PLANNER_RESERVE_KWH).toBeCloseTo(reserveKwh, 6);
    const slots = Array.from({ length: 24 }, (_, i) => slot(i, 92));
    const result = optimizer.optimizeDispatch(slots, reserveKwh + 0.7);
    expect(Math.min(...result.map((x) => x.socAfter))).toBeGreaterThanOrEqual(reserveKwh - 0.000001);
    expect(result.at(-1)!.socAfter).toBeGreaterThanOrEqual(reserveKwh - 0.000001);
  });

  it('replenishes a battery that starts below 20% instead of leaving it at 12%', async () => {
    const { optimizer } = await siteOptimizer('20');
    const slots = Array.from({ length: 12 }, (_, i) => slot(i, 91));
    const result = optimizer.optimizeDispatch(slots, physicalFloorKwh + 0.3);
    expect(result[0].action).toBe('charge');
    expect(result[0].socAfter).toBeGreaterThanOrEqual(reserveKwh - 0.000001);
    expect(result.every((x) => x.socAfter >= reserveKwh - 0.000001)).toBe(true);
  });

  it('still charges in cheap hours and discharges for dear household use', async () => {
    const { optimizer } = await siteOptimizer('20');
    const slots = Array.from({ length: 20 }, (_, i) =>
      slot(i, i < 8 ? 60 : 220, i < 8 ? 0.1 : 0.5),
    );
    const result = optimizer.optimizeDispatch(slots, reserveKwh);
    expect(result.slice(0, 8).some((x) => x.action === 'charge')).toBe(true);
    expect(result.slice(8).some((x) => x.action === 'discharge')).toBe(true);
    expect(result.every((x) => x.socAfter >= reserveKwh - 0.000001)).toBe(true);
  });

  it('does not force full charging on a flat-price day', async () => {
    const { optimizer } = await siteOptimizer('20');
    const result = optimizer.optimizeDispatch(
      Array.from({ length: 40 }, (_, i) => slot(i, 95)), reserveKwh,
    );
    expect(result.some((x) => x.gridToBatteryKwh > 0.1)).toBe(false);
    expect(result.at(-1)!.socAfter).toBeLessThan(reserveKwh + 0.2);
  });

  it('defaults planner reserve to physical floor on other installations', async () => {
    const { constants } = await siteOptimizer();
    expect(constants.BATTERY_PLANNER_RESERVE_KWH).toBeCloseTo(physicalFloorKwh, 6);
  });
});
