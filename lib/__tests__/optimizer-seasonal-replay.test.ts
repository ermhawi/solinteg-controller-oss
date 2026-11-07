/** O04: deterministic, offline seasonal price/PV/EV dispatch replay.
 * No Next requests, telemetry DB, external forecasts or live inverter/EVCC.
 */
import { describe, it, expect } from 'vitest';
import {
  optimizeDispatch, BATTERY_KWH, BATTERY_MIN_SOC_KWH, BATTERY_MAX_KW, GRID_KW,
} from '../optimizer';
import type { OptimizerSlot } from '../optimizer';

function scenario(
  timestampUtc: string, count: number, opts: {
    pv?: number; house?: number; cheap?: number; expensive?: number;
    split?: number; ev?: number; missing?: number; negative?: boolean;
  } = {},
): OptimizerSlot[] {
  const begin = new Date(timestampUtc).getTime();
  return Array.from({ length: count }, (_, i) => {
    const daytime = i >= 36 && i < 68;
    const buy = opts.negative && i < 16 ? -10 : i < (opts.split ?? count / 2)
      ? (opts.cheap ?? 30) : (opts.expensive ?? 180);
    const solar = i === opts.missing ? 0 : daytime ? (opts.pv ?? 0.5) : 0;
    return {
      startTime: new Date(begin + i * 15 * 60_000).toISOString().slice(0, 19),
      buyPrice: buy,
      sellPrice: Math.min(50, buy - 60),
      solarKwh: solar,
      consumptionKwh: (opts.house ?? 0.55) + (daytime && opts.ev ? opts.ev : 0),
    };
  });
}

function summarize(slots: OptimizerSlot[], startSoc: number) {
  const out = optimizeDispatch(slots, startSoc);
  expect(out).toHaveLength(slots.length);
  const eps = 1e-5;
  let prior = startSoc;
  for (const [i, row] of out.entries()) {
    expect(row.startTime).toBe(slots[i].startTime);
    expect(Number.isFinite(row.socAfter)).toBe(true);
    expect(row.socAfter).toBeGreaterThanOrEqual(BATTERY_MIN_SOC_KWH - eps);
    expect(row.socAfter).toBeLessThanOrEqual(BATTERY_KWH + eps);
    expect(Math.abs(row.socAfter - prior)).toBeLessThanOrEqual(BATTERY_MAX_KW / 4 + eps);
    expect(row.gridKwh).toBeLessThanOrEqual(GRID_KW / 4 + eps);
    prior = row.socAfter;
  }
  return { out, minSoc: Math.min(...out.map(x => x.socAfter)),
    endSoc: out.at(-1)?.socAfter ?? startSoc };
}

describe('seasonal and DST optimizer replay (O04)', () => {
  const baselineSoc = BATTERY_MIN_SOC_KWH + (BATTERY_KWH - BATTERY_MIN_SOC_KWH) * 0.6;

  it.each([
    ['winter ordinary day', '2026-01-15T00:00:00Z', 96, { pv: 0.12 }],
    ['summer ordinary day', '2026-06-15T00:00:00Z', 96, { pv: 1.8 }],
    ['spring DST shortened day', '2026-03-29T00:00:00Z', 92, { pv: 0.6 }],
    ['autumn DST extended day', '2026-10-25T00:00:00Z', 100, { pv: 0.4 }],
    ['short data horizon', '2026-01-15T00:00:00Z', 8, { pv: 0 }],
    ['one forecast missing slot', '2026-06-15T00:00:00Z', 96, { pv: 0.7, missing: 49 }],
    ['price rises and later falls', '2026-01-15T00:00:00Z', 96, { cheap: 200, expensive: 20 }],
    ['negative spot / expensive grid later', '2026-01-15T00:00:00Z', 96,
      { cheap: -20, expensive: 330, negative: true }],
    ['EV load alongside solar production', '2026-06-15T00:00:00Z', 96,
      { pv: 1.2, ev: 0.7 }],
  ] as const)('%s stays within optimizer SoC, phase-throughput and grid bounds', (_, date, count, opts) => {
    const first = summarize(scenario(date, count, opts), baselineSoc);
    const second = summarize(scenario(date, count, opts), baselineSoc);
    expect(first).toEqual(second);
  });

  it('quantifies terminal SoC divergence induced only by a truncated horizon', () => {
    const long = summarize(scenario('2026-01-15T00:00:00Z', 96,
      { pv: 0, cheap: 120, expensive: 150 }), baselineSoc);
    const short = summarize(scenario('2026-01-15T00:00:00Z', 8,
      { pv: 0, cheap: 120, expensive: 150 }), baselineSoc);
    const lastOnShortHorizon = long.out[7].socAfter;
    const divergenceKwh = short.endSoc - lastOnShortHorizon;
    // Characterization, NOT safety approval: the collapsed horizon chooses a
    // radically lower SoC even though the start SoC and first two hours match.
    // Keep this reproducible failure mode explicit until O04 is mitigated.
    expect(Number.isFinite(divergenceKwh)).toBe(true);
    expect(divergenceKwh).toBeLessThan(-10);
  });
});
