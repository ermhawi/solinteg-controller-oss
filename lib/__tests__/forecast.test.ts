import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ghiToKwh, calibrateForecastKwh, apiAzimuth } from '../solar';
import { SOLAR_FORECAST_BIAS_FACTOR } from '../constants';
import { slotIndex } from '../slot-utils';
import { solarCalibrationByMonth } from '../consumption-data';

describe('solar forecast calibration', () => {
  it('applies the configured flat bias factor', () => {
    expect(calibrateForecastKwh(1)).toBeCloseTo(SOLAR_FORECAST_BIAS_FACTOR, 6);
  });

  // The factor shipped at 1.75 purely to mask the azimuth-convention bug (see apiAzimuth and
  // OPTIMIZER-AUDIT.md P0-1). With the geometry right there is nothing left for a flat,
  // all-season multiplier to correct, so 1 is the honest default — pinned here so a future
  // "calibration" cannot quietly reintroduce an unexplained site-wide fudge.
  it('defaults to no correction now that the forecast geometry is right', () => {
    expect(SOLAR_FORECAST_BIAS_FACTOR).toBe(1);
  });
});

describe('apiAzimuth (0 = north stored -> 0 = south as every irradiance API wants)', () => {
  it('converts this site\'s SSE plane to the API convention', () => {
    expect(apiAzimuth(153)).toBe(-27);
  });

  it('maps the cardinal directions', () => {
    expect(apiAzimuth(180)).toBe(0); // south
    expect(apiAzimuth(90)).toBe(-90); // east
    expect(apiAzimuth(270)).toBe(90); // west
    expect(apiAzimuth(0)).toBe(-180); // north
  });

  it('converts the original reference site\'s three arrays', () => {
    expect(apiAzimuth(123)).toBe(-57); // array A, SE
    expect(apiAzimuth(303)).toBe(123); // array B, NW
    expect(apiAzimuth(214)).toBe(34); // array C, SW
  });

  it('always lands in the API\'s (-180, 180] range', () => {
    for (let a = 0; a < 360; a++) {
      const converted = apiAzimuth(a);
      expect(converted).toBeGreaterThan(-181);
      expect(converted).toBeLessThanOrEqual(180);
    }
  });
});


// hit Open-Meteo directly — untested until now. This is the exact file that shipped the
// hour-label-shift bug fixed 2026-07-20: Open-Meteo's hourly label is the average of the
// PRECEDING interval, so a naive same-hour mapping silently misattributes every slot by an
// hour (or, at 15-min granularity below, by one slot). 'next/cache' is mocked since cacheLife()
// is only meaningful inside a real Next.js request/build; global.fetch is mocked to avoid any
// real network call.
//
// fetchSolarForecast() branches on SOLAR_FORECAST_MODEL (lib/constants.ts, default
// 'metno_nordic'): a truthy model fetches hourly data (shift back one HOUR); '' falls back to
// Open-Meteo's default minutely_15 blend (shift back one 15-min SLOT instead). SOLAR_FORECAST_MODEL
// is a module-level const read from process.env at import time, so the minutely_15 branch is
// tested via vi.stubEnv + vi.resetModules() + a fresh dynamic import — the default-model tests
// use the normal static import.
vi.mock('next/cache', () => ({ cacheLife: vi.fn() }));

// solarCalibrationByMonth ships as uniform 1.0s for this site (no production history yet), which
// would make the month-discrimination assertion below trivially true. Override it here with
// DISTINCT per-month values so forecast.ts's "use the slot's own month" calibration indexing
// stays under test regardless of the shipped constants.
vi.mock('../consumption-data', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../consumption-data')>()),
  solarCalibrationByMonth: Array.from({ length: 12 }, (_, i) => 1 + (i + 1) / 100),
}));

import { fetchSolarForecast, fetchDailyMeanTemp, combineSolarForecasts } from '../forecast';

function openMeteoHourlyResponse(hourlyTimes: string[], irradiance: number[]) {
  return { hourly: { time: hourlyTimes, global_tilted_irradiance: irradiance } };
}

function openMeteoMinutely15Response(times: string[], irradiance: number[]) {
  return { minutely_15: { time: times, global_tilted_irradiance: irradiance } };
}

function expectedKwhPerSlot(ghiWm2: number, month: number): number {
  const cal = solarCalibrationByMonth[month - 1];
  return Math.round((calibrateForecastKwh(ghiToKwh(ghiWm2) * cal) / 4) * 100) / 100;
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.unstubAllEnvs();
});

describe('combineSolarForecasts', () => {
  it('uses the median per slot and ignores missing providers', () => {
    const result = combineSolarForecasts({
      metno_nordic: { '2026-08-23': Array(96).fill(1) },
      ecmwf_ifs025: { '2026-08-23': Array(96).fill(3) },
      icon_seamless: { '2026-08-23': Array(96).fill(9) },
      forecast_solar: { '2026-08-23': Array(96).fill(5) },
    });
    expect(result['2026-08-23'][0]).toBe(4);
  });
});

describe('fetchSolarForecast — hourly model (default SOLAR_FORECAST_MODEL=metno_nordic)', () => {
  it('requests panel-plane irradiance with the configured tilt and azimuth', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      json: async () => openMeteoHourlyResponse([], []),
    } as Response);

    await fetchSolarForecast();

    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('hourly=global_tilted_irradiance'),
      expect.objectContaining({ signal: expect.anything() }),
    );
    // -27, NOT the stored 153: Open-Meteo's azimuth is 0 = south. Sending 153 raw pointed the
    // model 27 degrees east of NORTH and put the annual forecast 2.13x low (OPTIMIZER-AUDIT.md P0-1).
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('tilt=45&azimuth=-27'),
      expect.anything(),
    );
    expect(fetch).not.toHaveBeenCalledWith(
      expect.stringContaining('azimuth=153'),
      expect.anything(),
    );
    // forecast.solar takes the azimuth as a PATH segment in the same 0 = south convention, so it
    // was wrong the same way and is easy to miss when only the query-string form is asserted.
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('api.forecast.solar/estimate/'),
      expect.anything(),
    );
    expect(fetch).not.toHaveBeenCalledWith(
      expect.stringContaining('/45/153/'),
      expect.anything(),
    );
  });

  it('shifts each hourly label back one hour, since Open-Meteo labels an hour by its END', async () => {
    // label "13:00" describes the 12:00-13:00 average -> must land on hour=12's four slots,
    // not hour=13's. This is the exact bug fixed 2026-07-20 (see the module's own comment).
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      json: async () => openMeteoHourlyResponse(['2026-01-15T13:00'], [200]),
    } as Response);

    const result = await fetchSolarForecast();
    const expected = expectedKwhPerSlot(200, 1);
    expect(result['2026-01-15'][slotIndex(12, 0)]).toBe(expected);
    expect(result['2026-01-15'][slotIndex(13, 0)]).toBe(0); // NOT the mislabeled hour
  });

  it('repeats the same hourly value across all four 15-min slots of that hour', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      json: async () => openMeteoHourlyResponse(['2026-01-15T13:00'], [200]),
    } as Response);

    const result = await fetchSolarForecast();
    const expected = expectedKwhPerSlot(200, 1);
    for (const minute of [0, 15, 30, 45]) {
      expect(result['2026-01-15'][slotIndex(12, minute)]).toBe(expected);
    }
  });

  it('skips label hour 0 (its true hour is the previous, already-elapsed day)', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      json: async () => openMeteoHourlyResponse(['2026-01-15T00:00', '2026-01-15T01:00'], [50, 100]),
    } as Response);

    const result = await fetchSolarForecast();
    expect(Object.keys(result)).toEqual(['2026-01-15']);
    expect(result['2026-01-15'][slotIndex(0, 0)]).toBe(expectedKwhPerSlot(100, 1));
  });

  it('applies the calibration factor for the slot\'s own month, not the fetch month', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      json: async () => openMeteoHourlyResponse(['2026-06-01T13:00'], [300]),
    } as Response);

    const result = await fetchSolarForecast();
    expect(result['2026-06-01'][slotIndex(12, 0)]).toBe(expectedKwhPerSlot(300, 6));
    expect(expectedKwhPerSlot(300, 6)).not.toBe(expectedKwhPerSlot(300, 1)); // sanity: months differ (calibration mocked distinct at top; shipped values are uniform 1.0)
  });

  it('throws when Open-Meteo responds with a non-ok status', async () => {
    vi.mocked(fetch).mockResolvedValue({ ok: false, status: 503 } as Response);
    await expect(fetchSolarForecast()).rejects.toThrow(/503/);
  });
});

describe('fetchSolarForecast — default minutely_15 blend (SOLAR_FORECAST_MODEL="")', () => {
  it('shifts each 15-min label back one slot, at 15-min granularity rather than a full hour', async () => {
    vi.stubEnv('SOLAR_FORECAST_MODEL', '');
    vi.resetModules();
    const fresh = await import('../forecast');

    // label 13:15 describes the preceding 13:00-13:15 interval -> must land on slotIndex(13, 0),
    // not slotIndex(13, 15).
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      json: async () => openMeteoMinutely15Response(['2026-01-15T13:15'], [200]),
    } as Response);

    const result = await fresh.fetchSolarForecast();
    const expected = expectedKwhPerSlot(200, 1);
    expect(result['2026-01-15'][slotIndex(13, 0)]).toBe(expected);
    expect(result['2026-01-15'][slotIndex(13, 15)]).toBe(0); // NOT the mislabeled slot
  });

  it('skips label slot 0 (its true slot is the previous, already-elapsed day)', async () => {
    vi.stubEnv('SOLAR_FORECAST_MODEL', '');
    vi.resetModules();
    const fresh = await import('../forecast');

    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      json: async () => openMeteoMinutely15Response(['2026-01-15T00:00', '2026-01-15T00:15'], [50, 100]),
    } as Response);

    const result = await fresh.fetchSolarForecast();
    expect(Object.keys(result)).toEqual(['2026-01-15']);
    expect(result['2026-01-15'][slotIndex(0, 0)]).toBe(expectedKwhPerSlot(100, 1));
  });
});

describe('fetchDailyMeanTemp', () => {
  it('maps each daily date to its mean temperature', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      json: async () => ({
        daily: { time: ['2026-01-15', '2026-01-16'], temperature_2m_mean: [-3.2, 1.5] },
      }),
    } as Response);

    await expect(fetchDailyMeanTemp()).resolves.toEqual({
      '2026-01-15': -3.2,
      '2026-01-16': 1.5,
    });
  });

  it('omits a date whose temperature reading is null', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      json: async () => ({
        daily: { time: ['2026-01-15', '2026-01-16'], temperature_2m_mean: [-3.2, null] },
      }),
    } as Response);

    await expect(fetchDailyMeanTemp()).resolves.toEqual({ '2026-01-15': -3.2 });
  });

  it('throws when Open-Meteo responds with a non-ok status', async () => {
    vi.mocked(fetch).mockResolvedValue({ ok: false, status: 502 } as Response);
    await expect(fetchDailyMeanTemp()).rejects.toThrow(/502/);
  });
});
