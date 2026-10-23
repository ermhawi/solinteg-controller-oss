import { describe, expect, it } from 'vitest';
import { BATTERY_KWH, BATTERY_MIN_SOC_KWH } from '../constants';
import { optimizeDispatch } from '../optimizer';
import type { OptimizerSlot } from '../optimizer';

function deficitSlots(n: number): OptimizerSlot[] {
  return Array.from({ length: n }, (_, i) => ({
    startTime: `2026-10-22T${String(i).padStart(2, '0')}:00:00`,
    buyPrice: 160,
    sellPrice: 20,
    solarKwh: 0,
    consumptionKwh: 0.25,
  }));
}

describe('independent planning reserve', () => {
  const reserve = BATTERY_KWH * 0.17;

  it('never intentionally discharges below a higher economic reserve', () => {
    const start = reserve + 1.5;
    const plans = optimizeDispatch(deficitSlots(8), start, { minSocKwh: reserve });
    expect(plans.length).toBe(8);
    for (const slot of plans) expect(slot.socAfter).toBeGreaterThanOrEqual(reserve - 1e-6);
    expect(plans.at(-1)!.socAfter).toBeGreaterThanOrEqual(reserve - 1e-6);
  });

  it('can start below reserve without inventing energy or discharging lower', () => {
    const start = BATTERY_KWH * 0.15;
    const plans = optimizeDispatch(deficitSlots(4), start, { minSocKwh: reserve });
    let soc = start;
    for (const slot of plans) {
      expect(slot.socAfter).toBeGreaterThanOrEqual(Math.min(soc, reserve) - 1e-6);
      soc = slot.socAfter;
    }
  });

  it('leaves physical safety floor and default planner floor unchanged', () => {
    const start = BATTERY_MIN_SOC_KWH + 1.5;
    expect(optimizeDispatch(deficitSlots(6), start)).toEqual(
      optimizeDispatch(deficitSlots(6), start, { minSocKwh: BATTERY_MIN_SOC_KWH }),
    );
  });

  it('does not allow a caller to set a reserve below the safety floor', () => {
    const plan = optimizeDispatch(deficitSlots(6), BATTERY_MIN_SOC_KWH + 1, { minSocKwh: 0 });
    expect(plan.every((x) => x.socAfter >= BATTERY_MIN_SOC_KWH - 1e-6)).toBe(true);
  });
});
