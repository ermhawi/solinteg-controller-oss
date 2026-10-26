'use client';

import { useMemo } from 'react';
import {
  buildActionBands,
  buildChartData,
  buildChartGeometry,
  buildTimeIndex,
  buildXTicks,
  computePriceMax,
  computePriceMin,
  computeSolarMax,
  limitPricesToThreeDays,
} from '@/lib/chart-utils';
import type { PriceData } from '@/lib/prices';
import type { DispatchSlot } from '@/lib/optimizer';
import type { ActualSlotFlows } from '@/lib/actual-flows';

export function useChartData(
  data: PriceData,
  solarProfiles: Record<number, number[]>,
  solarForecast: Record<string, number[]> | null | undefined,
  dispatchSchedule: DispatchSlot[] | null | undefined,
  batteryKwh: number,
  skattOverforing: number,
  batteryFloorKwh: number,
  actualSocByTime: Record<string, number> = {},
  pastDispatchSlots: DispatchSlot[] | null | undefined = [],
  pastPlannedSocKwhByTime: Record<string, number> = {},
  actualFlowsByTime: Record<string, ActualSlotFlows> = {},
  interventionsByTime: Record<string, string[]> = {},
  startSocKwh = batteryKwh / 2,
) {
  const chartPrices = useMemo(
    () => limitPricesToThreeDays(data.prices, data.today),
    [data.prices, data.today],
  );

  // Past slots first, live schedule second: on the one key both could theoretically share (the
  // live plan's first slot vs a past run's last, which readPastDispatchSlots' cutoff already
  // excludes), the live plan wins — it's the more current view of that slot's decision.
  const dispatchByTime = useMemo(() => {
    return Object.fromEntries(
      [...(pastDispatchSlots ?? []), ...(dispatchSchedule ?? [])].map((d) => [d.startTime, d]),
    );
  }, [pastDispatchSlots, dispatchSchedule]);

  // Historical bands (already-elapsed slots, reconstructed from past plans — see
  // lib/telemetry readPastDispatchSlots) come strictly before dispatchSchedule's own slots, so
  // concatenating keeps the array time-ordered and buildActionBands' run-collapsing works
  // unchanged across the past/live boundary.
  const actionBands = useMemo(
    () => buildActionBands([...(pastDispatchSlots ?? []), ...(dispatchSchedule ?? [])], batteryFloorKwh),
    [pastDispatchSlots, dispatchSchedule, batteryFloorKwh],
  );

  const plannedSocKwhByTime = useMemo(() => {
    const out = { ...pastPlannedSocKwhByTime };
    let soc = startSocKwh;
    for (const dispatch of [...(dispatchSchedule ?? [])].sort((a, b) => a.startTime.localeCompare(b.startTime))) {
      out[dispatch.startTime] = soc;
      soc = dispatch.socAfter;
    }
    return out;
  }, [pastPlannedSocKwhByTime, dispatchSchedule, startSocKwh]);

  const chartData = useMemo(
    () =>
      buildChartData(
        chartPrices,
        solarForecast,
        solarProfiles,
        dispatchByTime,
        batteryKwh,
        skattOverforing,
        batteryFloorKwh,
        actualSocByTime,
        actualFlowsByTime,
        interventionsByTime,
        startSocKwh,
        plannedSocKwhByTime,
      ),
    [
      chartPrices,
      solarForecast,
      solarProfiles,
      dispatchByTime,
      batteryKwh,
      skattOverforing,
      batteryFloorKwh,
      actualSocByTime,
      actualFlowsByTime,
      interventionsByTime,
      startSocKwh,
      plannedSocKwhByTime,
    ],
  );

  const xTicks = useMemo(() => buildXTicks(chartPrices), [chartPrices]);

  const tomorrowIdx = useMemo(
    () => chartPrices.findIndex((p) => p.startTime.startsWith(data.tomorrow)),
    [chartPrices, data.tomorrow],
  );

  const firstTime = chartData[0]?.time ?? null;
  const lastTime = chartData[chartData.length - 1]?.time ?? null;
  const tomorrowTime = tomorrowIdx > 0 ? (chartData[tomorrowIdx]?.time ?? null) : null;
  const beforeTomorrowTime = tomorrowIdx > 0 ? (chartData[tomorrowIdx - 1]?.time ?? null) : null;

  // SVG chart view-model: geometry is a fixed constant, but timeIndex/priceMax/solarMax depend
  // on the actual (variable-length, variable-range) chartData. Height is taller than the
  // mockup's own 388 default so the card has more visual weight next to PowerFlowCard.
  const geometry = useMemo(() => buildChartGeometry(1040, 410), []);
  const timeIndex = useMemo(() => buildTimeIndex(chartData), [chartData]);
  const priceMax = useMemo(() => computePriceMax(chartData), [chartData]);
  const priceMin = useMemo(() => computePriceMin(chartData), [chartData]);
  const solarMax = useMemo(() => computeSolarMax(chartData), [chartData]);

  return {
    actionBands,
    chartData,
    xTicks,
    firstTime,
    lastTime,
    tomorrowTime,
    beforeTomorrowTime,
    geometry,
    timeIndex,
    priceMax,
    priceMin,
    solarMax,
  };
}
