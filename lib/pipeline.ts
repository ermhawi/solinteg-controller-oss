import { getSolarProfileByMonth } from './solar';
import { slotSolarKwh } from './slot-utils';
import { slotConsumptionKwh, slotConsumptionFromLive, slotConsumptionHighFromLive } from './load';
import type { TrailingLoadProfile } from './load';
import { SKATT_OVERFÖRING } from './constants';
import type { PriceData } from './prices';
import type { OptimizerSlot } from './optimizer';

/**
 * Builds the monthly irradiance profiles needed for solar fallback estimates.
 * Covers today and tomorrow (which may span a month boundary).
 */
export function buildSolarProfiles(
  data: Pick<PriceData, 'today' | 'tomorrow' | 'prices'>,
): Record<number, number[]> {
  // Every month present in the loaded horizon, not just today's and tomorrow's. The horizon reaches
  // three days (lib/prices.ts merges a forecast horizon), so on a month boundary the third day used
  // to find `profiles[month] === undefined` and fall back to **zero** solar for the whole day
  // whenever the live forecast was missing — a silent, once-a-month, worst-case input. Deriving the
  // set from the prices themselves means it cannot drift from the horizon again.
  // See OPTIMIZER-AUDIT.md G3.
  const months = new Set<number>([
    parseInt(data.today.slice(5, 7), 10),
    parseInt(data.tomorrow.slice(5, 7), 10),
    ...data.prices.map((slot) => parseInt(slot.startTime.slice(5, 7), 10)),
  ]);
  const profiles: Record<number, number[]> = {};
  for (const month of months) profiles[month] = getSolarProfileByMonth(month);
  return profiles;
}

/**
 * Maps raw price slots + solar data into the shape the optimizer expects.
 * Pure function — no I/O, no Next.js dependencies, fully testable.
 *
 * `liveLoad` (optional) is the trailing measured per-hour load profile
 * (lib/telemetry/readings.ts readTrailingLoadProfile) — when present it replaces the static
 * Ellevio-fitted hour shape as the consumption forecast (see lib/load.ts for why),
 * tagged loadSource 'live'. Passing null/undefined keeps the previous behavior exactly.
 */
export function buildOptimizerSlots(
  data: PriceData,
  forecast: Record<string, number[]> | null | undefined,
  profiles: Record<number, number[]>,
  tempByDate?: Record<string, number> | null,
  liveLoad?: TrailingLoadProfile | null,
  /** True when `forecast` came off disk (lib/solar-forecast-cache.ts) because every live source
   *  was down. Re-tags forecast-backed slots as 'stale' — same numbers, weaker provenance — so
   *  "degraded but still weather-aware" is distinguishable from both healthy and blind. */
  forecastFromDisk = false,
): OptimizerSlot[] {
  return data.prices.map((slot) => {
    const { kwh: solarKwh, source: rawSolarSource } = slotSolarKwh(slot.startTime, forecast, profiles);
    const solarSource =
      forecastFromDisk && rawSolarSource === 'forecast' ? ('stale' as const) : rawSolarSource;
    const date = slot.startTime.slice(0, 10);
    const loadSource = liveLoad ? 'live' : tempByDate?.[date] != null ? 'modeled' : 'baseline';
    return {
      startTime: slot.startTime,
      buyPrice: slot.priceIncludingTaxAndSurcharge + SKATT_OVERFÖRING,
      sellPrice: slot.price,
      priceSource: slot.source,
      priceUncertaintyOre: slot.uncertaintyOre,
      solarKwh,
      solarSource,
      consumptionKwh: liveLoad
        ? slotConsumptionFromLive(liveLoad, slot.startTime, tempByDate)
        : slotConsumptionKwh(slot.startTime, tempByDate),
      // The measured high quantile for this hour, when the window supports one. Undefined falls the
      // DP back to the flat LOAD_FORECAST_MARGIN — see consumptionReserveKwh on OptimizerSlot.
      consumptionReserveKwh: liveLoad
        ? slotConsumptionHighFromLive(liveLoad, slot.startTime, tempByDate) ?? undefined
        : undefined,
      loadSource,
    };
  });
}
