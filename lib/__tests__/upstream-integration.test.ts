import { describe, it, expect } from 'vitest';
import { optimizeDispatch, evaluateDispatch, refundEarnedOre, BATTERY_MIN_SOC_KWH } from '../optimizer';
import type { DispatchSlot, OptimizerSlot } from '../optimizer';
import { energyTaxRefundOre, EFF_ONE_WAY } from '../constants';

const slot = (i: number, overrides: Partial<OptimizerSlot> = {}): OptimizerSlot => ({
  startTime: `2026-10-19T${String(Math.floor(i / 4)).padStart(2, '0')}:${String(i % 4 * 15).padStart(2, '0')}:00`,
  buyPrice: 157,
  sellPrice: 62,
  solarKwh: 0,
  ...overrides,
});

const purchased = (d: DispatchSlot[]) => d.reduce((sum, s) => sum + s.gridToBatteryKwh, 0);

describe('origin/main optimization features on the Sundsvall model', () => {
  it('new risk heuristics are inert when their respective rates are zero', () => {
    const slots = Array.from({ length: 24 }, (_, i) => slot(i, {
      buyPrice: i < 6 ? 80 : 140,
      consumptionKwh: 0.5,
      solarKwh: i >= 12 ? 0.3 : 0,
    }));
    const plain = optimizeDispatch(slots, BATTERY_MIN_SOC_KWH);
    expect(optimizeDispatch(slots, BATTERY_MIN_SOC_KWH, {
      solarRiskSurvival: true, solarRiskPremiumOre: 0,
    })).toEqual(plain);
    expect(optimizeDispatch(slots, BATTERY_MIN_SOC_KWH, {
      deferralUntilNeeded: true, deferralRateOrePerKwhHour: 0,
    })).toEqual(plain);
  });

  it('sunlight in the immediately following slots leaves the survival form unchanged', () => {
    const slots = Array.from({ length: 36 }, (_, i) => slot(i, {
      buyPrice: i < 24 ? 86 : 190,
      sellPrice: i < 24 ? 6 : 98,
      consumptionKwh: i < 24 ? 0.25 : 0,
      solarKwh: i < 24 ? 0.25 : 0,
    }));
    expect(optimizeDispatch(slots, 14, {
      solarRiskPremiumOre: 20, solarRiskSurvival: true,
    })).toEqual(optimizeDispatch(slots, 14, { solarRiskPremiumOre: 20 }));
  });

  it('risk candidate scoring can include the reserve without changing honest valuation', () => {
    const slots = Array.from({ length: 8 }, (_, i) => slot(i, {
      consumptionKwh: 0.1,
      consumptionReserveKwh: 1,
      buyPrice: i < 4 ? 85 : 180,
    }));
    const plan = optimizeDispatch(slots, BATTERY_MIN_SOC_KWH, {
      loadFactor: 1.15,
      solarRiskPremiumOre: 20,
    });
    const honest = evaluateDispatch(slots, plan, BATTERY_MIN_SOC_KWH, { loadFactor: 1.15 });
    const effective = evaluateDispatch(slots, plan, BATTERY_MIN_SOC_KWH, {
      loadFactor: 1.15, useConsumptionReserve: true,
    });
    expect(effective.valueOre).toBeLessThan(honest.valueOre);
  });

  it('refund-aware planning is opt-in, excludes solar slots, and never credits unknown initial energy', () => {
    const slots = Array.from({ length: 24 }, (_, i) => slot(i, {
      buyPrice: i < 16 ? 157 : 295,
      sellPrice: i < 16 ? 62 : 166,
    }));
    const plain = optimizeDispatch(slots, BATTERY_MIN_SOC_KWH);
    const credited = optimizeDispatch(slots, BATTERY_MIN_SOC_KWH, { refundCreditOre: () => 36 });
    expect(purchased(credited)).toBeGreaterThan(purchased(plain));
    expect(refundEarnedOre(credited, slots.map(() => 36))).toBeGreaterThan(0);
    expect(optimizeDispatch(slots, BATTERY_MIN_SOC_KWH, { refundCreditOre: () => 0 })).toEqual(plain);
    expect(optimizeDispatch(slots.map((s) => ({ ...s, solarKwh: 0.5 })),
      BATTERY_MIN_SOC_KWH, { refundCreditOre: () => 36 })).toEqual(
      optimizeDispatch(slots.map((s) => ({ ...s, solarKwh: 0.5 })), BATTERY_MIN_SOC_KWH),
    );
    expect(energyTaxRefundOre('2031-01-01T03:00:00')).toBe(0);
  });

  it('refund attribution excludes conversion loss and grid energy consumed by the house', () => {
    const row = (gridToBatteryKwh: number, batteryToLoadKwh: number, batteryToGridKwh: number, socAfter: number): DispatchSlot => ({
      startTime: '', action: 'idle', gridKwh: 0, solarExportKwh: 0, gridToBatteryKwh,
      batteryToLoadKwh, batteryToGridKwh, loadFromGridKwh: 0, socAfter,
    });
    const noExport = [row(1, 0, 0, BATTERY_MIN_SOC_KWH + EFF_ONE_WAY),
      row(0, EFF_ONE_WAY * EFF_ONE_WAY, 2, BATTERY_MIN_SOC_KWH)];
    expect(refundEarnedOre(noExport, [36, 36])).toBe(0);
    const exportOnly = [row(1, 0, 0, BATTERY_MIN_SOC_KWH + EFF_ONE_WAY),
      row(0, 0, EFF_ONE_WAY * EFF_ONE_WAY, BATTERY_MIN_SOC_KWH)];
    expect(refundEarnedOre(exportOnly, [36, 36])).toBeCloseTo(36 * EFF_ONE_WAY * EFF_ONE_WAY, 5);
    expect(refundEarnedOre(exportOnly, [36, 0])).toBe(0);
  });
});
