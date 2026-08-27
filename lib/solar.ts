import { avgGhiByMonthHour } from './irradiance-data';
import { SOLAR_FORECAST_BIAS_FACTOR, SOLAR_ARRAYS } from './constants';
import { solarCalibrationByMonth } from './consumption-data';

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
