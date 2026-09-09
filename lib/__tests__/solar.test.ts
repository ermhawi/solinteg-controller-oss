import { describe, it, expect } from 'vitest';
import { ghiToKwh, getSolarProfileByMonth, estimateDailyKwh } from '../solar';
import { applyIntradaySolarNowcast } from '../solar-nowcast';

describe('ghiToKwh', () => {
  it('returns 0 for zero irradiance', () => {
    expect(ghiToKwh(0)).toBe(0);
  });

  it('scales linearly with GHI', () => {
    const at100 = ghiToKwh(100);
    const at200 = ghiToKwh(200);
    expect(at200).toBeCloseTo(at100 * 2, 10);
  });

  it('produces a plausible kWh/h for 500 W/m² (typical summer midday)', () => {
    // 5.4 kWp single plane, PR 0.78 → ~2.1 kWh at 500 W/m²
    const kwh = ghiToKwh(500);
    expect(kwh).toBeGreaterThan(1);
    expect(kwh).toBeLessThan(4);
  });

  it('is never negative', () => {
    expect(ghiToKwh(0)).toBeGreaterThanOrEqual(0);
    expect(ghiToKwh(1000)).toBeGreaterThanOrEqual(0);
  });
});

describe('getSolarProfileByMonth', () => {
  it('returns exactly 24 values', () => {
    for (let m = 1; m <= 12; m++) {
      expect(getSolarProfileByMonth(m)).toHaveLength(24);
    }
  });

  it('all values are non-negative', () => {
    for (let m = 1; m <= 12; m++) {
      getSolarProfileByMonth(m).forEach((v) => expect(v).toBeGreaterThanOrEqual(0));
    }
  });

  it('summer peak (June) is higher than winter (December)', () => {
    const junePeak = Math.max(...getSolarProfileByMonth(6));
    const decPeak = Math.max(...getSolarProfileByMonth(12));
    expect(junePeak).toBeGreaterThan(decPeak);
  });

  it('early night hours are near-zero in every month', () => {
    // UTC 00:00–01:00 should be negligible in Gothenburg (sun rises no earlier than ~02:00 UTC in summer)
    // Historical data may have tiny non-zero values due to sensor noise, so use a low threshold.
    for (let m = 1; m <= 12; m++) {
      const profile = getSolarProfileByMonth(m);
      expect(profile[0]).toBeLessThan(0.05);
      expect(profile[1]).toBeLessThan(0.05);
    }
  });
});

describe('estimateDailyKwh', () => {
  it('returns the sum of the hourly profile', () => {
    for (let m = 1; m <= 12; m++) {
      const profile = getSolarProfileByMonth(m);
      const sum = Math.round(profile.reduce((s, v) => s + v, 0) * 10) / 10;
      expect(estimateDailyKwh(m)).toBeCloseTo(sum, 1);
    }
  });

  it('seasonal ordering: Jun > Apr > Jan', () => {
    expect(estimateDailyKwh(6)).toBeGreaterThan(estimateDailyKwh(4));
    expect(estimateDailyKwh(4)).toBeGreaterThan(estimateDailyKwh(1));
  });
});

describe('applyIntradaySolarNowcast', () => {
  it('reduces only the next six hours when measured PV is below forecast', () => {
    const forecast = { '2026-06-27': Array.from({ length: 96 }, () => 1) };
    const readings = [
      ...[0, 15, 30, 45].flatMap((minute) => [0, 30].map((offset) => ({
        timestamp: `2026-06-27T${String(10 + Math.floor((minute + offset) / 60)).padStart(2, '0')}:${String((minute + offset) % 60).padStart(2, '0')}:00.000Z`,
        pv_w: 1000, house_load_w: 500, battery_w: 0,
      }))),
    ];
    const result = applyIntradaySolarNowcast(forecast, readings, new Date('2026-06-27T12:00:00Z'));
    expect(result.applied).toBe(true);
    expect(result.ratio).toBeCloseTo(0.25);
    expect(result.forecast['2026-06-27'][56]).toBe(0.25);
    expect(result.forecast['2026-06-27'][79]).toBe(0.25);
    expect(result.forecast['2026-06-27'][80]).toBe(1);
  });

  it('leaves the forecast unchanged when telemetry is too thin', () => {
    const forecast = { '2026-06-27': Array.from({ length: 96 }, () => 1) };
    const result = applyIntradaySolarNowcast(forecast, [{
      timestamp: '2026-06-27T11:30:00.000Z', pv_w: 0, house_load_w: 500, battery_w: 0,
    }], new Date('2026-06-27T12:00:00Z'));
    expect(result.applied).toBe(false);
    expect(result.forecast).toBe(forecast);
  });
});
