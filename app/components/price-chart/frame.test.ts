import { describe, expect, it } from 'vitest';
import { buildChartGeometry, buildTimeIndex } from '@/lib/chart-utils';
import type { ChartPoint } from '@/lib/chart-utils';
import { buildFrame } from './frame';

function point(time: string): ChartPoint {
  return {
    time,
    buy: 100,
    sell: 60,
    socPct: null,
    actualSocPct: null,
    solarKwh: 0,
    solarSource: 'typical',
    action: 'idle',
    decision: null,
    gridToBatteryKwh: null,
    batteryToGridKwh: null,
    batteryToLoadKwh: null,
    actual: null,
    interventions: [],
  };
}

describe('buildFrame', () => {
  const chartData = Array.from({ length: 8 }, (_, i) => point(`2026-06-28T0${i}:00:00`));
  const geometry = buildChartGeometry();
  const timeIndex = buildTimeIndex(chartData);

  it('maps a zoomed index range to the full plot width', () => {
    const frame = buildFrame(chartData, geometry, timeIndex, 0, 200, 2, 2, 6);

    expect(frame.xAt(2)).toBe(geometry.padL);
    expect(frame.xAt(6)).toBe(geometry.padL + geometry.plotW);
    expect(frame.stepX).toBeCloseTo(geometry.plotW / 4);
  });

  it('clamps off-screen points to the visible plot edges', () => {
    const frame = buildFrame(chartData, geometry, timeIndex, 0, 200, 2, 2, 6);

    expect(frame.xAt(0)).toBe(geometry.padL);
    expect(frame.xAt(7)).toBe(geometry.padL + geometry.plotW);
    expect(frame.tx(chartData[0].time)).toBe(geometry.padL);
    expect(frame.tx(chartData[7].time)).toBe(geometry.padL + geometry.plotW);
  });
});
