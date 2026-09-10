import {
  avgDailyConsumptionByMonth,
  hddNormalByMonth,
  HDD_T_BASE_C,
  hourShareByMonth,
  LOAD_SLOPE_KWH_PER_HDD,
} from './consumption-data';

const MIN_DAILY_LOAD_KWH = 3; // floor: never predict an implausibly low household day

/**
 * Expected total household consumption for a whole day (kWh).
 *
 * Uses the measured monthly average as the level and adds a within-month heating
 * adjustment driven by the day's mean temperature:
 *
 *   load = avgDailyConsumptionByMonth[m] + slope × (HDD − hddNormal[m]),
 *   HDD  = max(0, HDD_T_BASE_C − dailyMeanTemp).
 *
 * When `dailyMeanTemp` is null (no forecast), falls back to the plain monthly
 * average (adjustment = 0). The adjustment self-zeroes in summer because both
 * HDD and hddNormal are ≈0 there.
 *
 * @param month 1-based month (1 = January)
 */
export function dailyLoadKwh(month: number, dailyMeanTemp: number | null | undefined): number {
  const baseline = avgDailyConsumptionByMonth[month - 1] ?? 0;
  if (dailyMeanTemp == null) return Math.max(MIN_DAILY_LOAD_KWH, baseline);
  const hdd = Math.max(0, HDD_T_BASE_C - dailyMeanTemp);
  const adjusted = baseline + LOAD_SLOPE_KWH_PER_HDD * (hdd - hddNormalByMonth[month - 1]);
  return Math.max(MIN_DAILY_LOAD_KWH, adjusted);
}

/**
 * Expected household consumption for one 15-min slot (kWh).
 *
 * The daily total is distributed by the measured hour-of-day shape
 * (hourShareByMonth — true billing-meter load from each month's PV/battery-clean
 * window, added 2026-07-11; each of an hour's four slots gets a quarter of that
 * hour's share). This replaced a uniform 1/96 split, which under-allocated winter
 * load ~30-40% at exactly the morning/evening price peaks. The pre-2026-07 concern
 * that hourly import was distorted by a night-tariff timer applied to a different,
 * inverter-side series — the DSO meter history this profile is built from measures
 * the whole house.
 *
 * On DST-transition days the 92/100 real slots make the shares sum to ≈±1 h/24 of
 * a day rather than exactly 1 — same accepted noise as slotSolarKwh's fallback.
 *
 * @param startTime ISO-like local timestamp "YYYY-MM-DDTHH:MM:SS"
 * @param tempByDate date → forecast daily mean temperature (°C)
 */
export function slotConsumptionKwh(
  startTime: string,
  tempByDate: Record<string, number> | null | undefined,
): number {
  const date = startTime.slice(0, 10);
  const month = parseInt(startTime.slice(5, 7), 10);
  const hour = parseInt(startTime.slice(11, 13), 10);
  const temp = tempByDate?.[date] ?? null;
  const share = hourShareByMonth[month - 1]?.[hour] ?? 1 / 24;
  return (dailyLoadKwh(month, temp) * share) / 4;
}

/**
 * Per-hour load profile measured from the house's own trailing poller readings
 * (lib/telemetry/readings.ts readTrailingLoadProfile). Exists because the static Ellevio-fitted
 * shape above goes stale: measured 2026-07-18, the 2022-era July shape ran ~25% low
 * overnight and ~40% high at the modeled dinner peak — the household simply doesn't run
 * the way it did pre-PV. A trailing mean of the actual house is the best estimator of
 * next-night load in any season; the static model remains as fallback (no DB, thin data)
 * and as the weather-sensitivity term below.
 */
export interface TrailingLoadProfile {
  /** Mean consumption per local hour-of-day (kWh per hour), index 0-23. */
  hourKwh: number[];
  /**
   * A HIGH quantile of the same per-hour distribution across the window's days (kWh per hour,
   * index 0-23) — what a hot-but-not-freak day looks like at that hour, measured rather than assumed.
   *
   * Exists to replace the single global LOAD_FORECAST_MARGIN guess. The margin exists because the
   * cost of running short is far larger than the cost of over-holding (see that constant), but a flat
   * 1.15 says the same thing about 03:00 and about the dinner peak, and those hours do not have the
   * same spread at all: overnight load is nearly deterministic, evenings are where the variance is.
   * Undefined when the window is too thin to take a quantile from.
   */
  hourHighKwh?: number[];
  /** Mean outdoor temp (°C) over the same trailing window, or null if unavailable. */
  trailingMeanTempC: number | null;
  /** Distinct local days represented — callers already got null below this module's bar. */
  days: number;
}

/**
 * Expected consumption for one 15-min slot (kWh) from the live trailing profile.
 *
 * The trailing mean carries the house's CURRENT level and shape, but it lags weather: a
 * cold snap arriving tomorrow isn't in the last two weeks' average. The static model's
 * HDD term supplies exactly that sensitivity, applied as a ratio so it corrects the live
 * level rather than replacing it:
 *
 *   scale = dailyLoadKwh(month, forecastTemp) / dailyLoadKwh(month, trailingMeanTemp)
 *
 * In summer both HDDs are ≈0 and scale ≈ 1 (the live profile is used as-is); in winter a
 * colder-than-recent forecast scales the whole day up by the fitted kWh/HDD slope. When
 * either temperature is unknown the scale degrades to 1 — still strictly better than the
 * stale static shape.
 */
export function slotConsumptionFromLive(
  profile: TrailingLoadProfile,
  startTime: string,
  tempByDate: Record<string, number> | null | undefined,
): number {
  return scaledSlotLoad(profile, profile.hourKwh, startTime, tempByDate);
}

/**
 * The same slot's load at the profile's HIGH quantile (see `hourHighKwh`) — the figure the DP plans
 * its reserve against, while `slotConsumptionFromLive` stays the honest expectation that gets logged
 * and validated against actuals.
 *
 * Returns null when the profile carries no quantile, so the caller falls back to the flat
 * LOAD_FORECAST_MARGIN it used before.
 */
export function slotConsumptionHighFromLive(
  profile: TrailingLoadProfile,
  startTime: string,
  tempByDate: Record<string, number> | null | undefined,
): number | null {
  if (!profile.hourHighKwh) return null;
  return scaledSlotLoad(profile, profile.hourHighKwh, startTime, tempByDate);
}

/** Shared body: pick the hour's value, quarter it, and apply the weather scale. */
function scaledSlotLoad(
  profile: TrailingLoadProfile,
  hourly: number[],
  startTime: string,
  tempByDate: Record<string, number> | null | undefined,
): number {
  const date = startTime.slice(0, 10);
  const month = parseInt(startTime.slice(5, 7), 10);
  const hour = parseInt(startTime.slice(11, 13), 10);
  const base = (hourly[hour] ?? 0) / 4;
  const forecastTemp = tempByDate?.[date];
  if (forecastTemp == null || profile.trailingMeanTempC == null) return base;
  const scale =
    dailyLoadKwh(month, forecastTemp) / dailyLoadKwh(month, profile.trailingMeanTempC);
  return base * scale;
}
