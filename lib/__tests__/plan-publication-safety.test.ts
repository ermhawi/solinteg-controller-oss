import { describe, it, expect } from 'vitest';
import { assessPlanPublication } from '../plan-publication-safety';
import type { OptimizerSlot, DispatchSlot } from '../optimizer';

const NOW = Date.parse('2026-11-07T11:00:00Z');
const LIVE = { timestamp: '2026-11-07T10:59:30Z', soc_pct: 50, soc_kwh: 7.5 };

function slots(times: string[]): OptimizerSlot[] {
  return times.map(startTime => ({
    startTime, buyPrice: 90, sellPrice: -15, solarKwh: 0.4, consumptionKwh: 0.2,
  }));
}
function output(inputs: OptimizerSlot[]): DispatchSlot[] {
  return inputs.map(s => ({
    startTime: s.startTime, action: 'idle', gridKwh: 0,
    solarExportKwh: 0.2, batteryToGridKwh: 0, gridToBatteryKwh: 0,
    batteryToLoadKwh: 0, loadFromGridKwh: 0, socAfter: 7.5,
  }));
}
const START = ['2026-11-07T12:00:00', '2026-11-07T12:15:00'];
function assess({
  configured = '1', live = LIVE, times = START, schemaCompatible = true,
  dispatch,
}: {
  configured?: string;
  live?: typeof LIVE | null;
  times?: string[];
  schemaCompatible?: boolean;
  dispatch?: DispatchSlot[];
} = {}) {
  const inputs = slots(times);
  return assessPlanPublication({
    configured, live, inputs, dispatch: dispatch ?? output(inputs),
    nowMs: NOW, schemaCompatible,
  });
}

describe('O06 opt-in publisher safety gate', () => {
  it('preserves default-off legacy semantics', () => {
    const inputs = slots(START);
    expect(assessPlanPublication({
      configured: undefined, live: null, inputs, dispatch: [], nowMs: NOW,
      schemaCompatible: false,
    })).toMatchObject({ publishable: true, enabled: false, reason: 'disabled' });
    expect(assess({ configured: '0' }).publishable).toBe(true);
  });

  it('accepts finite fresh SoC, matched dispatch and readable schema', () => {
    expect(assess()).toEqual({ publishable: true, enabled: true, reason: 'valid' });
    expect(assess({ live: { ...LIVE, timestamp: '2026-11-07T10:58:00Z' } })
      .publishable).toBe(true);
  });

  it('rejects missing, old, future and timezone-ambiguous SoC', () => {
    expect(assess({ live: null }).reason).toBe('missing-live-soc');
    for (const timestamp of ['2026-11-07T10:57:59Z', '2026-11-07T11:00:06Z',
      '2026-11-07T11:00:00', 'invalid']) {
      expect(assess({ live: { ...LIVE, timestamp } }).reason).toBe('stale-live-soc');
    }
  });

  it('rejects non-finite or out-of-range SoC and missing schema', () => {
    for (const update of [{ soc_pct: NaN }, { soc_pct: 101 }, { soc_kwh: -1 },
      { soc_kwh: Infinity }, { soc_kwh: 100 }]) {
      expect(assess({ live: { ...LIVE, ...update } }).reason).toBe('invalid-soc');
    }
    expect(assess({ schemaCompatible: false }).reason).toBe('incompatible-schema');
  });

  it('rejects malformed or broken 15-minute time axes', () => {
    for (const times of [
      [], ['2026-11-07T12:17:00'], ['2026-11-07T12:00:00', '2026-11-07T12:30:00'],
      ['2026-11-07T12:15:00', '2026-11-07T12:00:00'],
      ['2026-11-07T12:00:00', '2026-11-07T12:00:00'],
      ['2026-11-07T25:00:00'],
    ]) {
      expect(assess({ times }).publishable).toBe(false);
    }
  });

  it('accepts real Stockholm DST transitions and rejects fictitious transitions', () => {
    const spring = ['2026-03-29T01:30:00', '2026-03-29T01:45:00',
      '2026-03-29T03:00:00', '2026-03-29T03:15:00'];
    const autumn = ['2026-10-25T02:30:00', '2026-10-25T02:45:00',
      '2026-10-25T02:00:00', '2026-10-25T02:15:00'];
    expect(assess({ times: spring }).reason).toBe('valid');
    expect(assess({ times: autumn }).reason).toBe('valid');
    const repeatedFallHour = [...autumn, '2026-10-25T02:30:00',
      '2026-10-25T02:45:00', '2026-10-25T02:00:00'];
    expect(assess({ times: repeatedFallHour }).reason).toBe('invalid-time-axis');
    expect(assess({ times: spring.map(x => x.replace('03-29', '03-28')) })
      .reason).toBe('invalid-time-axis');
    expect(assess({ times: autumn.map(x => x.replace('10-25', '10-24')) })
      .reason).toBe('invalid-time-axis');
  });

  it('rejects non-finite forecasts but allows genuinely negative spot and sell prices', () => {
    const inputs = slots(START);
    inputs[0].buyPrice = -80;
    inputs[0].sellPrice = -150;
    expect(assessPlanPublication({
      configured: '1', live: LIVE, nowMs: NOW, schemaCompatible: true,
      inputs, dispatch: output(inputs),
    }).publishable).toBe(true);
    inputs[0].solarKwh = NaN;
    expect(assessPlanPublication({
      configured: '1', live: LIVE, nowMs: NOW, schemaCompatible: true,
      inputs, dispatch: output(inputs),
    }).reason).toBe('invalid-inputs');
  });

  it('rejects truncated, reordered and unsafe DP results', () => {
    const inputs = slots(START);
    const good = output(inputs);
    for (const dispatch of [
      good.slice(0, 1), [...good].reverse(),
      [{ ...good[0], socAfter: NaN }, good[1]],
      [{ ...good[0], gridKwh: Infinity }, good[1]],
    ]) {
      expect(assess({ dispatch }).reason).toBe('invalid-dispatch');
    }
  });

  it('invalid explicit flag is never interpreted as disabled', () => {
    for (const configured of ['yes', ' 1', '', 'NaN']) {
      expect(assess({ configured })).toMatchObject({
        publishable: false, enabled: true, reason: 'invalid-configuration',
      });
    }
  });
});
