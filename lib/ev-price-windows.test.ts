import { describe, expect, it } from 'vitest';
import { buildEvPriceWindows } from './ev-price-windows';
import type { ChartPoint } from './chart-utils';

function slots(values: number[], forecastFrom = Infinity) {
  return values.map((buy, i) => ({
    time: '2026-11-04T' + String(Math.floor(i / 4)).padStart(2, '0') +
      ':' + String((i % 4) * 15).padStart(2, '0') + ':00',
    buy, priceSource: i >= forecastFrom ? 'forecast' : 'actual',
  })) as Pick<ChartPoint, 'time' | 'buy' | 'priceSource'>[];
}

describe('EV cheap-price windows (proposal, never a physical command)', () => {
  it('selects the cheapest quarter by the house full buy price', () => {
    const rows = slots(Array.from({ length: 32 }, (_, i) => i < 8 ? 40 : 100));
    const result = buildEvPriceWindows(rows, rows[0].time, 10);
    expect(result.status).toBe('preview');
    expect(result.thresholdOre).toBe(40);
    expect(result.chargeSlots).toBe(8);
    expect(result.estimatedKwhAt6A).toBeCloseTo(2.76);
    expect(result.slots[8].charging).toBe(false);
  });

  it('does not call a flat expensive horizon cheap', () => {
    const rows = slots(Array(28).fill(145));
    const result = buildEvPriceWindows(rows, rows[0].time, 10);
    expect(result.status).toBe('preview');
    expect(result.chargeSlots).toBe(0);
    expect(result.thresholdOre).toBeLessThan(145);
  });

  it('accepts truly free or negative slots and an explicit cap', () => {
    const zero = slots(Array(32).fill(0));
    expect(buildEvPriceWindows(zero, zero[0].time, 10).chargeSlots).toBe(32);
    const negative = slots(Array.from({ length: 32 }, (_, i) => i < 8 ? -10 : 20));
    expect(buildEvPriceWindows(negative, negative[0].time, 10, -1).chargeSlots).toBe(8);
  });

  it('adds forecast risk and never claims those periods are actual', () => {
    const rows = slots([...Array(8).fill(40), ...Array(8).fill(60), ...Array(16).fill(55)], 16);
    const result = buildEvPriceWindows(rows, rows[0].time, 10);
    expect(result.hasForecast).toBe(true);
    expect(result.chargeSlots).toBe(8); // 55 forecast becomes 65, dearer than 40 actual
    expect(result.slots[16].priceOre).toBe(65);
    expect(result.slots[16].forecast).toBe(true);
  });

  it('bounds a long horizon to 48h and keeps repeated DST slots distinct', () => {
    const rows = slots(Array(220).fill(20));
    rows[9].time = '2026-10-25T02:00:00';
    rows[10].time = '2026-10-25T02:00:00#2';
    const result = buildEvPriceWindows(rows, rows[0].time, 10);
    expect(result.slots).toHaveLength(192);
    expect(result.slots[9].time).not.toBe(result.slots[10].time);
  });

  it('does not invent a plan without enough real prices or a current slot', () => {
    const short = slots(Array(23).fill(10));
    expect(buildEvPriceWindows(short, short[0].time, 10).status).toBe('unavailable');
    const allForecast = slots(Array(30).fill(10), 0);
    expect(buildEvPriceWindows(allForecast, allForecast[0].time, 10).status).toBe('unavailable');
    expect(buildEvPriceWindows(short, null, 10).slots).toEqual([]);
  });

  it('never turns invalid prices or invalid caps into a charging suggestion', () => {
    const rows = slots(Array(36).fill(50));
    rows[3].buy = NaN;
    expect(buildEvPriceWindows(rows, rows[0].time, 10).status).toBe('unavailable');
    rows[3].buy = 50;
    expect(buildEvPriceWindows(rows, rows[0].time, 10, 4500).status).toBe('unavailable');
  });
});
