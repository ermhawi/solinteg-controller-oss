'use client';

import { indexToX } from '@/lib/chart-utils';
import type { ChartGeometry, ChartPoint } from '@/lib/chart-utils';

/**
 * The projection every part of the chart draws against: the data, the box it's drawn in, and
 * the scales that map one onto the other.
 *
 * Bundled rather than passed as eight separate props because PriceChart.tsx was split into
 * layers (bands → axes → series → overlay) on 2026-08-07 and every layer needs most of these.
 * As individual props that split would have traded a 600-line function for four components with
 * eight-argument prop lists, which is not an improvement.
 *
 * `tx` is the reason this is a factory rather than a plain object literal: `timeToX(t,
 * chartData, geo, timeIndex)` was spelled out fourteen times in the old single-file version,
 * three of those arguments identical at every call. Curried once here, the call sites read
 * `frame.tx(time)`.
 */
export interface ChartFrame {
  chartData: ChartPoint[];
  geo: ChartGeometry;
  /** startTime → index. Carried as well as closed over by `tx` because sumActualForBand needs
   *  the map itself, not just the projection. */
  timeIndex: Map<string, number>;
  priceMin: number;
  priceMax: number;
  solarMax: number;
  /** Inclusive absolute chart-data indices currently visible on the x-axis. */
  viewStartIndex: number;
  viewEndIndex: number;
  /** Horizontal position of slot `i`. */
  xAt: (i: number) => number;
  /** Horizontal position of a slot's startTime, or null if it isn't in this chart's data. */
  tx: (time: string) => number | null;
  /** Width of one slot — bands widen by half of this at each end to cover their whole slot. */
  stepX: number;
}

export function buildFrame(
  chartData: ChartPoint[],
  geo: ChartGeometry,
  timeIndex: Map<string, number>,
  priceMin: number,
  priceMax: number,
  solarMax: number,
  viewStartIndex = 0,
  viewEndIndex = Math.max(0, chartData.length - 1),
): ChartFrame {
  const count = chartData.length;
  const start = Math.max(0, Math.min(viewStartIndex, Math.max(0, count - 1)));
  const end = Math.max(start, Math.min(viewEndIndex, Math.max(0, count - 1)));
  const span = Math.max(1, end - start);
  const xForIndex = (index: number) => {
    const clamped = Math.max(start, Math.min(index, end));
    return indexToX((clamped - start) / span * (count - 1), count, geo);
  };
  return {
    chartData,
    geo,
    timeIndex,
    priceMin,
    priceMax,
    solarMax,
    viewStartIndex: start,
    viewEndIndex: end,
    xAt: xForIndex,
    tx: (time: string) => {
      const index = timeIndex.get(time);
      return index == null ? null : xForIndex(index);
    },
    stepX: count > 1 ? geo.plotW / span : 0,
  };
}
