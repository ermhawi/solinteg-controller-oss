import { avgGhiByMonthHour } from './irradiance-data';
import { SOLAR_FORECAST_BIAS_FACTOR, SOLAR_ARRAYS } from './constants';
import { solarCalibrationByMonth } from './consumption-data';

/**
 * Convert this repo's array azimuth (0° = north, 180° = south — the convention
 * `SOLAR_ARRAYS`/DOMAIN.md document) to the convention every irradiance API used here expects
 * (0 = south, −90 = east, +90 = west, ±180 = north): Open-Meteo's `azimuth=` parameter and
 * forecast.solar's `:az` path segment.
 *
 * This exists because the raw value used to be passed through verbatim, which forecast a
 * 153° (SSE) plane as 27° east of NORTH. Measured 2026-09-10 at the reference site (62.44 N,
 * 45° tilt): 7.4 vs 24.2 kWh/day of modelled production, and an ERA5 full-year check put the
 * annual forecast 2.13× low (1.5× in June, 6.2× in December). Three compensating fudge factors
 * had grown on top of it (a flat 1.75 bias multiplier, hand-set 1.4 monthly calibrations for
 * Aug/Sep, and a nowcast ratio clamp) — all removed in the same change. See OPTIMIZER-AUDIT.md
 * P0-1.
 *
 * Verified empirically, not from docs: at 62.44 N / 45° tilt, `azimuth=0` peaks at 13:00 local
 * (true solar noon 12:49 CEST at 17.36 E) and yields the day's highest plane irradiance,
 * `-90` peaks at 11:00, `+90` at 16:00, and `180` yields ~1/6 of `0`.
 */
export function apiAzimuth(azimuthDegFromNorth: number): number {
  return ((azimuthDegFromNorth - 180 + 540) % 360) - 180;
}

/** Convert panel-plane irradiance in W/m² to modeled array energy in kWh. */
export function ghiToKwh(irradianceWm2: number): number {
  return SOLAR_ARRAYS.reduce(
    (sum, { kWp, performanceRatio }) => sum + (irradianceWm2 / 1000) * kWp * performanceRatio,
    0,
  );
}

export function calibrateForecastKwh(kwh: number): number {
  return Math.max(0, kwh * SOLAR_FORECAST_BIAS_FACTOR);
}


// Values are calibrated against 3 years of actual inverter data so that the
// monthly total matches observed production rather than the raw GHI-model estimate.
export function getSolarProfileByMonth(month: number): number[] {
  const hours = avgGhiByMonthHour[month - 1];
  const cal = solarCalibrationByMonth[month - 1];
  return hours.map((ghi) => Math.round(ghiToKwh(ghi) * cal * 100) / 100);
}

export function estimateDailyKwh(month: number): number {
  return Math.round(getSolarProfileByMonth(month).reduce((s, v) => s + v, 0) * 10) / 10;
}
