import { cacheLife } from 'next/cache';
import { ghiToKwh } from './solar';
import { slotIndex } from './slot-utils';
import { solarCalibrationByMonth } from './consumption-data';
import { FETCH_TIMEOUT_MS, SITE_LATITUDE, SITE_LONGITUDE, SOLAR_ARRAYS, SOLAR_FORECAST_MODEL } from './constants';

const SOLAR_ARRAY = SOLAR_ARRAYS[0];
const SOLAR_FORECAST_MODELS = ['metno_nordic', 'ecmwf_ifs025', 'icon_seamless'] as const;
export type SolarForecastSource = (typeof SOLAR_FORECAST_MODELS)[number] | 'openmeteo_best_match' | 'forecast_solar';
export type SolarForecastSources = Partial<Record<SolarForecastSource, Record<string, number[]>>>;
const FORECAST_URL =
  `https://api.open-meteo.com/v1/forecast?latitude=${SITE_LATITUDE}&longitude=${SITE_LONGITUDE}` +
  (SOLAR_FORECAST_MODEL
    ? `&hourly=global_tilted_irradiance&tilt=${SOLAR_ARRAY.tiltDeg}&azimuth=${SOLAR_ARRAY.azimuthDeg}&models=${SOLAR_FORECAST_MODEL}`
    : `&minutely_15=global_tilted_irradiance&tilt=${SOLAR_ARRAY.tiltDeg}&azimuth=${SOLAR_ARRAY.azimuthDeg}`) +
  '&forecast_days=2&timezone=Europe%2FStockholm';
const FORECAST_SOLAR_URL =
  `https://api.forecast.solar/estimate/${SITE_LATITUDE}/${SITE_LONGITUDE}/${SOLAR_ARRAY.tiltDeg}/${SOLAR_ARRAY.azimuthDeg}/${SOLAR_ARRAY.kWp}`;

const TEMP_URL =
  `https://api.open-meteo.com/v1/forecast?latitude=${SITE_LATITUDE}&longitude=${SITE_LONGITUDE}` +
  '&daily=temperature_2m_mean&forecast_days=2&timezone=Europe%2FStockholm';

// Returns date → 96-element array of kWh per 15-min slot. Open-Meteo's panel-plane irradiance
// is converted with the configured array and performance ratio; hourly values are spread across
// four slots after shifting the preceding-interval label back one hour.
function parseOpenMeteo(data: { hourly?: { time: string[]; global_tilted_irradiance: number[] } }): Record<string, number[]> {
  const result: Record<string, number[]> = {};
  const times = data.hourly?.time ?? [];
  const values = data.hourly?.global_tilted_irradiance ?? [];
  for (let i = 0; i < times.length; i++) {
    const labelHour = parseInt(times[i].slice(11, 13), 10);
    if (labelHour === 0) continue;
    const date = times[i].slice(0, 10);
    const hour = labelHour - 1;
    const cal = solarCalibrationByMonth[parseInt(date.slice(5, 7), 10) - 1];
    const kwhPerSlot = Math.round((ghiToKwh(values[i]) * cal / 4) * 100) / 100;
    if (!result[date]) result[date] = new Array(96).fill(0);
    for (let minute = 0; minute < 60; minute += 15) result[date][slotIndex(hour, minute)] = kwhPerSlot;
  }
  return result;
}

/** Forecast.Solar returns hourly watt-hours for the preceding interval. */
function parseForecastSolar(data: { result?: { watt_hours_period?: Record<string, number> } }): Record<string, number[]> {
  const result: Record<string, number[]> = {};
  for (const [time, wattHours] of Object.entries(data.result?.watt_hours_period ?? {})) {
    const labelHour = parseInt(time.slice(11, 13), 10);
    if (labelHour === 0) continue;
    const date = time.slice(0, 10);
    const hour = labelHour - 1;
    const kwhPerSlot = Math.round((wattHours / 1000 / 4) * 100) / 100;
    if (!result[date]) result[date] = new Array(96).fill(0);
    for (let minute = 0; minute < 60; minute += 15) result[date][slotIndex(hour, minute)] = kwhPerSlot;
  }
  return result;
}

function parseOpenMeteoMinutely(data: { minutely_15?: { time: string[]; global_tilted_irradiance: number[] } }): Record<string, number[]> {
  const result: Record<string, number[]> = {};
  const times = data.minutely_15?.time ?? [];
  const values = data.minutely_15?.global_tilted_irradiance ?? [];
  for (let i = 0; i < times.length; i++) {
    const labelIdx = slotIndex(parseInt(times[i].slice(11, 13), 10), parseInt(times[i].slice(14, 16), 10));
    if (labelIdx === 0) continue;
    const date = times[i].slice(0, 10);
    const idx = labelIdx - 1;
    const cal = solarCalibrationByMonth[parseInt(date.slice(5, 7), 10) - 1];
    if (!result[date]) result[date] = new Array(96).fill(0);
    result[date][idx] = Math.round((ghiToKwh(values[i]) * cal / 4) * 100) / 100;
  }
  return result;
}

/** Median ensemble per slot; missing providers are ignored rather than treated as zero. */
export function combineSolarForecasts(sources: SolarForecastSources): Record<string, number[]> {
  const dates = new Set(Object.values(sources).flatMap((forecast) => Object.keys(forecast ?? {})));
  const result: Record<string, number[]> = {};
  for (const date of dates) {
    result[date] = new Array(96).fill(0).map((_, index) => {
      const values = Object.values(sources)
        .map((forecast) => forecast?.[date]?.[index])
        .filter((value): value is number => value != null && Number.isFinite(value));
      if (values.length === 0) return 0;
      values.sort((a, b) => a - b);
      const middle = Math.floor(values.length / 2);
      return Math.round((values.length % 2 ? values[middle] : (values[middle - 1] + values[middle]) / 2) * 100) / 100;
    });
  }
  return result;
}

export async function fetchSolarForecastSources(): Promise<SolarForecastSources> {
  'use cache';
  cacheLife({ revalidate: 3600, expire: 8 * 3600 });
  const requests = [
    ...SOLAR_FORECAST_MODELS.map(async (model) => {
      const url = `https://api.open-meteo.com/v1/forecast?latitude=${SITE_LATITUDE}&longitude=${SITE_LONGITUDE}&hourly=global_tilted_irradiance&tilt=${SOLAR_ARRAY.tiltDeg}&azimuth=${SOLAR_ARRAY.azimuthDeg}&models=${model}&forecast_days=2&timezone=Europe%2FStockholm`;
      const res = await fetch(url, { signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
      if (!res.ok) throw new Error(`Open-Meteo ${model} failed: ${res.status}`);
      return [model, parseOpenMeteo(await res.json())] as const;
    }),
    (async () => {
      const url = `https://api.open-meteo.com/v1/forecast?latitude=${SITE_LATITUDE}&longitude=${SITE_LONGITUDE}&minutely_15=global_tilted_irradiance&tilt=${SOLAR_ARRAY.tiltDeg}&azimuth=${SOLAR_ARRAY.azimuthDeg}&forecast_days=2&timezone=Europe%2FStockholm`;
      const res = await fetch(url, { signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
      if (!res.ok) throw new Error(`Open-Meteo best_match failed: ${res.status}`);
      return ['openmeteo_best_match', parseOpenMeteoMinutely(await res.json())] as const;
    })(),
    (async () => {
      const res = await fetch(FORECAST_SOLAR_URL, { signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
      if (!res.ok) throw new Error(`Forecast.Solar failed: ${res.status}`);
      return ['forecast_solar', parseForecastSolar(await res.json())] as const;
    })(),
  ];
  const settled = await Promise.allSettled(requests);
  const sources: SolarForecastSources = {};
  for (const result of settled) if (result.status === 'fulfilled') sources[result.value[0]] = result.value[1];
  if (Object.keys(sources).length === 0) {
    const failures = settled
      .filter((result): result is PromiseRejectedResult => result.status === 'rejected')
      .map((result) => String(result.reason?.message ?? result.reason))
      .join('; ');
    throw new Error(`all solar forecast sources failed: ${failures}`);
  }
  return sources;
}

export async function fetchSolarForecast(): Promise<Record<string, number[]>> {
  const sources = await fetchSolarForecastSources();
  return combineSolarForecasts(sources);
}

// Returns date → forecast daily mean outdoor temperature (°C), for today and tomorrow.
// Drives the weather-aware load model (see lib/load.ts). Same Open-Meteo endpoint,
// daily aggregation; cached on the same 1 h / 8 h schedule as the solar forecast.
export async function fetchDailyMeanTemp(): Promise<Record<string, number>> {
  'use cache';
  cacheLife({ revalidate: 3600, expire: 8 * 3600 });

  const res = await fetch(TEMP_URL, { signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
  if (!res.ok) throw new Error(`Open Meteo temp fetch failed: ${res.status}`);
  const data = await res.json();

  const times: string[] = data.daily.time;
  const temps: number[] = data.daily.temperature_2m_mean;
  const result: Record<string, number> = {};
  times.forEach((date, i) => {
    if (temps[i] != null) result[date] = temps[i];
  });
  return result;
}

/**
 * The forecast panel-plane irradiance (W/m²) for the CURRENT Stockholm hour — for the Väder card, which shows it
 * next to the live station reading so forecast error is visible at a glance. Display-only:
 * planning uses fetchSolarForecast() above, never this.
 *
 * Handles both response shapes FORECAST_URL can produce. With SOLAR_FORECAST_MODEL set (the
 * default) Open-Meteo returns `hourly`; with it cleared it returns `minutely_15`, and reading
 * only `hourly` would leave this silently null for anyone on the default blend. Both label
 * intervals by their END, so the same shift-back-one-interval alignment as fetchSolarForecast
 * applies; the 15-min variant averages the four intervals inside the hour.
 *
 * Returns null on any failure — the card renders the live reading alone.
 */
export async function fetchCurrentHourGhiForecast(): Promise<number | null> {
  'use cache';
  cacheLife({ revalidate: 3600, expire: 8 * 3600 });

  try {
    const res = await fetch(FORECAST_URL, { signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
    if (!res.ok) return null;
    const data = await res.json();

    const parts = new Intl.DateTimeFormat('sv-SE', {
      timeZone: 'Europe/Stockholm',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      hour12: false,
    }).formatToParts(new Date());
    const get = (t: string) => parts.find((p) => p.type === t)?.value ?? '';
    const currentDate = `${get('year')}-${get('month')}-${get('day')}`;
    const currentHour = get('hour') === '24' ? 0 : parseInt(get('hour'), 10);

    if (data.hourly?.time) {
      const times: string[] = data.hourly.time;
      const values: number[] = data.hourly.global_tilted_irradiance;
      for (let i = 0; i < times.length; i++) {
        const labelHour = parseInt(times[i].slice(11, 13), 10);
        if (labelHour === 0) continue; // belongs to the previous day's 23:00
        if (times[i].slice(0, 10) === currentDate && labelHour - 1 === currentHour) {
          return values[i] ?? null;
        }
      }
      return null;
    }

    if (data.minutely_15?.time) {
      const times: string[] = data.minutely_15.time;
      const values: number[] = data.minutely_15.global_tilted_irradiance;
      const inHour: number[] = [];
      for (let i = 0; i < times.length; i++) {
        // Shift back one 15-min interval so the label refers to the interval it covers.
        const t = new Date(`${times[i]}:00`).getTime() - 15 * 60_000;
        const d = new Date(t);
        const label = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
        if (label === currentDate && d.getHours() === currentHour && typeof values[i] === 'number') {
          inHour.push(values[i]);
        }
      }
      if (inHour.length === 0) return null;
      return inHour.reduce((a, b) => a + b, 0) / inHour.length;
    }

    return null;
  } catch {
    return null;
  }
}
