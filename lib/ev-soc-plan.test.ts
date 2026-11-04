import { describe, expect, it } from 'vitest';
import { buildEvSocProjection } from './ev-soc-plan';
import type { EvSocScenario } from './ev-soc-state';
import type { ChartPoint } from './chart-utils';

const NOW = Date.parse('2026-11-04T13:00:00Z');
const SESSION: EvSocScenario = {
  socPct: 50, targetPct: 80,
  assumedUsableKwh: 80,
  assumedEfficiency: 1,
  chargeMode: 'economy',
  departureLocalTime: '07:00',
  maxBuyOre: null,
  reportedAtUtc: '2026-11-04T12:59:30Z',
  receivedAtUtc: '2026-11-04T13:00:00Z',
};

function slots(values: number[], forecastFrom = Infinity) {
  const start = Date.parse('2026-11-04T14:00:00Z');
  return values.map((buy, i) => {
    const time = new Date(start + i * 900_000).toISOString().slice(0, 19);
    return {
      time, buy, priceSource: i >= forecastFrom ? 'forecast' : 'actual',
    };
  }) as Pick<ChartPoint, 'time' | 'buy' | 'priceSource'>[];
}

describe('EV SoC price projection, explicitly illustrative', () => {
  it('economy projects only cheap 6 A slots from reported Volvo SoC', () => {
    const p = slots([...Array(8).fill(40), ...Array(24).fill(130)]);
    const plan = buildEvSocProjection(p, p[0].time, SESSION, 10, NOW);
    expect(plan?.mode).toBe('economy');
    expect(plan?.windows.filter((s) => s.charging)).toHaveLength(8);
    expect(plan?.estimatedKwh).toBe(2.76);
    expect(plan?.endSocPct).toBe(53.4);
    expect(plan?.targetReached).toBe(false);
    expect(plan?.slots[0].socPct).toBe(50);
    expect(plan?.slots[8].socPct).toBe(53.4);
  });

  it('deadline chooses cheapest feasible slots, including expensive if needed', () => {
    // Next departure is 07:00 on Nov 5 = 68 slots from 14:00.
    const p = slots([...Array(12).fill(200), ...Array(60).fill(20)]);
    const session = { ...SESSION, chargeMode: 'deadline' as const, socPct: 65 };
    const plan = buildEvSocProjection(p, p[0].time, session, 10, NOW);
    expect(plan?.departureKey).toBe('2026-11-05T07:00:00');
    expect(plan?.windows.filter((s) => s.charging && s.priceOre === 200)).toHaveLength(0);
    expect(plan?.targetReached).toBe(true);
    expect(plan?.endSocPct).toBe(80);
    expect(plan?.slots.at(-1)?.time).toBe('2026-11-05T07:00:00');
  });

  it('does not promise the deadline if even continuous 6 A is insufficient', () => {
    const p = slots(Array(72).fill(160));
    const session = { ...SESSION, chargeMode: 'deadline' as const, socPct: 50 };
    const plan = buildEvSocProjection(p, p[0].time, session, 10, NOW);
    expect(plan?.targetReached).toBe(false);
    expect(plan?.deadlineCovered).toBe(true);
    expect(plan?.windows.filter((s) => s.charging)).toHaveLength(68);
    expect(plan?.endSocPct).toBeLessThan(80);
  });

  it('economy never claims to reach 80 on a flat expensive horizon', () => {
    const p = slots(Array(40).fill(170));
    const plan = buildEvSocProjection(p, p[0].time, SESSION, 10, NOW);
    expect(plan?.windows.every((s) => !s.charging)).toBe(true);
    expect(plan?.endSocPct).toBe(50);
  });

  it('uses the configured full-buy price cap and forecast risk', () => {
    const p = slots([...Array(8).fill(20), ...Array(16).fill(50), ...Array(8).fill(40)], 24);
    const session = { ...SESSION, maxBuyOre: 35 };
    const plan = buildEvSocProjection(p, p[0].time, session, 10, NOW);
    expect(plan?.windows.filter((s) => s.charging)).toHaveLength(8);
    expect(plan?.windows[24].priceOre).toBe(50);
    expect(plan?.windows[24].forecast).toBe(true);
  });

  it('caps projected SoC at 80 and stops suggested future charging', () => {
    const p = slots(Array(100).fill(-10));
    const session = { ...SESSION, socPct: 79.7 };
    const plan = buildEvSocProjection(p, p[0].time, session, 10, NOW);
    expect(plan?.targetReached).toBe(true);
    expect(plan?.endSocPct).toBe(80);
    expect(plan?.windows.filter((s) => s.charging)).toHaveLength(1);
  });

  it('withholds projected SoC without a recent API session', () => {
    const p = slots(Array(40).fill(10));
    expect(buildEvSocProjection(p, p[0].time, null, 10, NOW)).toBeNull();
    expect(buildEvSocProjection(p, p[0].time, SESSION, 10, NOW + 11 * 60_000)).toBeNull();
    expect(buildEvSocProjection(p, 'missing', SESSION, 10, NOW)).toBeNull();
  });

  it('prevents an ambiguous repeated autumn 02:xx departure claim', () => {
    const p = slots(Array(60).fill(10));
    p[1].time = '2026-10-25T02:30:00#2';
    const session = { ...SESSION, chargeMode: 'deadline' as const, departureLocalTime: '02:30' };
    // Current local slot precedes the October fallback day in the synthetic timeline.
    p[0].time = '2026-10-25T00:15:00';
    expect(buildEvSocProjection(p, p[0].time, session, 10, NOW)).toBeNull();
  });
});
