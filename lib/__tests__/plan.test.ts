import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { PriceData } from '../prices';
import type { OptimizerSlot, DispatchSlot } from '../optimizer';

// producePlan() is the single function every dashboard render AND every triggered replan
// (app/api/replan -> dispatch_loop.py's maybe_request_replan) goes through — see its own
// module docstring. Its own logic is orchestration: three network-fetch fallback chains, the
// nowSlotIdx clamp/slice that anchors the optimizer to a live mid-day SoC reading, and wiring
// the risk-aware planning params into optimizeDispatch. None of that is covered by unit tests
// of optimizer.ts/pipeline.ts in isolation (they test the pieces, not this file's wiring), so a
// typo'd option name or a broken fallback chain here wouldn't be caught anywhere else.
//
// These tests run under the default SOLAR_FORECAST_MODEL ('metno_nordic'), where the solar/temp
// fallback chains behave unconditionally, same as this project's reference deployment. If you've
// set SOLAR_FORECAST_MODEL to something else, producePlan() skips the direct-MET-Norway leg
// entirely (see plan.ts's own SOLAR_FORECAST_MODEL check) — not covered here.
//
// Every dependency is mocked (module-level, not real network/Next.js/telemetry) — this is the
// first file in this repo to use vi.mock, since it's the first case where the module under
// test is pure orchestration around I/O rather than pure computation or a real sqlite temp db.
// producePlan() awaits connection() to declare itself request-time (it reads the clock, live
// SoC and writes telemetry, so it can never be prerendered). Outside a Next request scope that
// throws, so it is stubbed — this suite is about orchestration, not the runtime.
vi.mock('next/server', () => ({ connection: vi.fn(async () => undefined) }));
// Partial mock: only the two request-time fetches/clock reads are stubbed. stockholmMidnightUtc
// is real pure date arithmetic that lib/horizon.ts depends on for DST-aware day lengths, and
// stubbing it away would make trimIncompleteTerminalDays silently accept nothing.
vi.mock('../prices', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../prices')>()),
  fetchPricesUncached: vi.fn(),
  currentSlotIndexInPrices: vi.fn(),
}));
vi.mock('../forecast', () => ({
  fetchSolarForecastSources: vi.fn(),
  combineSolarForecasts: (sources: Record<string, unknown>) => sources.metno_nordic,
  fetchDailyMeanTemp: vi.fn(),
}));
vi.mock('../metno-thredds', () => ({
  fetchSolarForecastDirect: vi.fn(),
  fetchDailyMeanTempDirect: vi.fn(),
}));
vi.mock('../solar-forecast-cache', () => ({
  readFreshSolarForecast: vi.fn(),
  saveSolarForecast: vi.fn(),
}));
vi.mock('../pipeline', () => ({
  buildSolarProfiles: vi.fn(),
  buildOptimizerSlots: vi.fn(),
}));
// Partial mock: only optimizeDispatch is stubbed (these tests are about the WIRING into it).
// terminalEnergyValueOre is pure arithmetic over the slots and producePlan calls it to build the
// argument being asserted on, so it stays real.
vi.mock('../optimizer', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../optimizer')>()),
  optimizeDispatch: vi.fn(),
}));
vi.mock('../inverter', () => ({
  readLiveInverterData: vi.fn(),
  socKwhOrDefault: vi.fn(),
}));
vi.mock('../telemetry', () => ({
  logPriceSnapshot: vi.fn(),
  logOptimizerRun: vi.fn(),
  logSolarForecasts: vi.fn(),
  readTrailingLoadProfile: vi.fn(),
  readTodayFlowRows: vi.fn(() => []),
}));

import { fetchPricesUncached, currentSlotIndexInPrices } from '../prices';
import { fetchSolarForecastSources, fetchDailyMeanTemp } from '../forecast';
import { fetchSolarForecastDirect, fetchDailyMeanTempDirect } from '../metno-thredds';
import { readFreshSolarForecast, saveSolarForecast } from '../solar-forecast-cache';
import { buildSolarProfiles, buildOptimizerSlots } from '../pipeline';
import { optimizeDispatch, terminalEnergyValueOre } from '../optimizer';
import { readLiveInverterData, socKwhOrDefault } from '../inverter';
import { logPriceSnapshot, logOptimizerRun, readTrailingLoadProfile } from '../telemetry';
import {
  LOAD_FORECAST_MARGIN,
  DEFERRAL_RATE_ORE_PER_KWH_HOUR,
  SOLAR_RISK_PREMIUM_ORE_PER_KWH,
  SOLAR_RISK_SURVIVAL,
  DEFERRAL_UNTIL_NEEDED,
  FORECAST_RISK_PREMIUM_ORE,
  BATTERY_SAVE_FORECAST_DRAIN_W,
  LIVE_LOAD_PROFILE_DAYS,
  TERMINAL_VALUE_FACTOR,
} from '../constants';
import { producePlan } from '../plan';

const PRICE_DATA: PriceData = {
  today: '2026-01-15',
  tomorrow: '2026-01-16',
  hasTomorrow: false,
  maxForMonth: 100,
  minForMonth: 10,
  maxAge: 3600,
  prices: [
    { startTime: '2026-01-15T00:00:00', endTime: '2026-01-15T00:15:00', price: 50, priceIncludingTaxAndSurcharge: 80 },
    { startTime: '2026-01-15T00:15:00', endTime: '2026-01-15T00:30:00', price: 55, priceIncludingTaxAndSurcharge: 85 },
    { startTime: '2026-01-15T00:30:00', endTime: '2026-01-15T00:45:00', price: 52, priceIncludingTaxAndSurcharge: 82 },
    { startTime: '2026-01-15T00:45:00', endTime: '2026-01-15T01:00:00', price: 51, priceIncludingTaxAndSurcharge: 81 },
  ],
};

const ALL_SLOTS: OptimizerSlot[] = PRICE_DATA.prices.map((p, i) => ({
  startTime: p.startTime,
  buyPrice: p.priceIncludingTaxAndSurcharge,
  sellPrice: p.price,
  solarKwh: i,
  consumptionKwh: 0.1,
}));

const DISPATCH_FIXTURE: DispatchSlot[] = [
  {
    startTime: '2026-01-15T00:15:00',
    action: 'idle',
    gridKwh: 0,
    solarExportKwh: 0,
    batteryToGridKwh: 0,
    gridToBatteryKwh: 0,
    batteryToLoadKwh: 0,
    loadFromGridKwh: 0,
    socAfter: 5,
  },
];

const FORECAST = { metno_nordic: { A: [1, 2, 3] } };
const TEMP_BY_DATE = { '2026-01-15': -2 };
const PROFILES = { 1: [0.1, 0.2] };
const LIVE_LOAD = { hourly: [1, 2, 3] } as unknown as ReturnType<typeof readTrailingLoadProfile>;
const INVERTER_DATA = { soc_kwh: 7.5 } as unknown as Awaited<ReturnType<typeof readLiveInverterData>>;

function silenceConsoleError() {
  return vi.spyOn(console, 'error').mockImplementation(() => {});
}

beforeEach(() => {
  vi.mocked(fetchPricesUncached).mockReset().mockResolvedValue(PRICE_DATA);
  vi.mocked(currentSlotIndexInPrices).mockReset().mockReturnValue(0);
  vi.mocked(fetchSolarForecastSources).mockReset().mockResolvedValue(FORECAST);
  vi.mocked(fetchDailyMeanTemp).mockReset().mockResolvedValue(TEMP_BY_DATE);
  // Baseline only — the happy path never reaches the direct MET Norway fallback (Open-Meteo
  // resolves first), and the tests that do exercise it override with a real value or a
  // rejection below. `{}` not `null`: both functions return a non-nullable Record, so a mock
  // resolving to null describes something the real code cannot do. plan.ts only ever sees null
  // from its own .catch() around these calls.
  vi.mocked(fetchSolarForecastDirect).mockReset().mockResolvedValue({});
  vi.mocked(fetchDailyMeanTempDirect).mockReset().mockResolvedValue({});
  vi.mocked(buildSolarProfiles).mockReset().mockReturnValue(PROFILES);
  vi.mocked(buildOptimizerSlots).mockReset().mockReturnValue(ALL_SLOTS);
  vi.mocked(readFreshSolarForecast).mockReset().mockResolvedValue(null);
  vi.mocked(saveSolarForecast).mockReset().mockResolvedValue(undefined);
  vi.mocked(optimizeDispatch).mockReset().mockReturnValue(DISPATCH_FIXTURE);
  vi.mocked(readLiveInverterData).mockReset().mockResolvedValue(INVERTER_DATA);
  vi.mocked(socKwhOrDefault).mockReset().mockReturnValue(7.5);
  vi.mocked(readTrailingLoadProfile).mockReset().mockReturnValue(LIVE_LOAD);
  vi.mocked(logPriceSnapshot).mockReset();
  vi.mocked(logOptimizerRun).mockReset();
});

describe('producePlan — happy path wiring', () => {
  it('threads live SoC, live load, and the risk-aware params through to the optimizer', async () => {
    vi.mocked(currentSlotIndexInPrices).mockReturnValue(1);

    const result = await producePlan();

    expect(result.data).toBe(PRICE_DATA);
    expect(result.solarProfiles).toBe(PROFILES);
    expect(result.solarForecast).toBe(FORECAST.metno_nordic);
    expect(result.startSoc).toBe(7.5);
    expect(result.socIsLive).toBe(true);
    expect(result.inverterData).toBe(INVERTER_DATA);
    expect(result.dispatchSchedule).toBe(DISPATCH_FIXTURE);

    expect(buildOptimizerSlots).toHaveBeenCalledWith(
      PRICE_DATA, FORECAST.metno_nordic, PROFILES, TEMP_BY_DATE, LIVE_LOAD, false,
    );
    expect(readTrailingLoadProfile).toHaveBeenCalledWith(LIVE_LOAD_PROFILE_DAYS);

    // The single most important wiring assertion: a typo'd option name here would silently
    // disable risk-aware planning without any optimizer-level test ever catching it.
    // holdEnabled: false is asserted exactly (not omitted) — the default-off promise is that
    // an install that never set SOLINTEG_HOLD_ENABLED gets a bit-identical DP.
    expect(optimizeDispatch).toHaveBeenCalledWith(ALL_SLOTS.slice(1), 7.5, {
      loadFactor: LOAD_FORECAST_MARGIN,
      deferralRateOrePerKwhHour: DEFERRAL_RATE_ORE_PER_KWH_HOUR,
      solarRiskPremiumOre: SOLAR_RISK_PREMIUM_ORE_PER_KWH,
      solarRiskSurvival: SOLAR_RISK_SURVIVAL,
      deferralUntilNeeded: DEFERRAL_UNTIL_NEEDED,
      holdEnabled: false,
      forecastRiskPremiumOre: FORECAST_RISK_PREMIUM_ORE,
      batterySaveDriftW: BATTERY_SAVE_FORECAST_DRAIN_W,
      // Derived from the sliced horizon's own prices, then scaled by the kill-switch factor —
      // asserted through the real helper rather than a literal, so this pins the WIRING (the
      // optimizer gets a terminal value at all, computed from the slots it is given) without
      // duplicating the formula. The formula itself is pinned in optimizer.test.ts.
      terminalValueOrePerKwh:
        terminalEnergyValueOre(ALL_SLOTS.slice(1)) * TERMINAL_VALUE_FACTOR,
    });

    expect(logPriceSnapshot).toHaveBeenCalledWith(PRICE_DATA);
    // Logged inputs must be the HONEST forecast slots (pre-loadFactor), the same reference fed
    // to the optimizer — never a load-inflated copy, or a forecast-vs-actual check would be
    // measuring the deliberate margin instead of the model.
    expect(logOptimizerRun).toHaveBeenCalledWith(
      PRICE_DATA.today,
      PRICE_DATA.hasTomorrow,
      7.5,
      ALL_SLOTS.slice(1),
      DISPATCH_FIXTURE,
      true,
    );
  });

  it('reports a fallback-anchored SoC as not live', async () => {
    vi.mocked(readLiveInverterData).mockResolvedValue(null);
    vi.mocked(socKwhOrDefault).mockReturnValue(12.8);

    const result = await producePlan();

    expect(result.socIsLive).toBe(false);
    expect(result.startSoc).toBe(12.8);
    expect(logOptimizerRun).toHaveBeenCalledWith(
      expect.anything(), expect.anything(), 12.8, expect.anything(), expect.anything(), false,
    );
  });
});

describe('producePlan — hold-mode gating', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('enables hold when SOLINTEG_HOLD_ENABLED=1 on a disarmed (shadow) install', async () => {
    vi.stubEnv('SOLINTEG_HOLD_ENABLED', '1');

    await producePlan();

    expect(optimizeDispatch).toHaveBeenCalledWith(
      expect.anything(),
      expect.anything(),
      expect.objectContaining({ holdEnabled: true }),
    );
  });

  it('forces hold OFF while control is armed, even when explicitly enabled', async () => {
    // The hardware cannot express hold — dispatch_loop maps it to return_to_auto, which
    // charges the surplus (the opposite). Armed installs must never see hold slots.
    vi.stubEnv('SOLINTEG_HOLD_ENABLED', '1');
    vi.stubEnv('SOLINTEG_CONTROL_ARMED', '1');

    await producePlan();

    expect(optimizeDispatch).toHaveBeenCalledWith(
      expect.anything(),
      expect.anything(),
      expect.objectContaining({ holdEnabled: false }),
    );
  });
});

describe('producePlan — full forecast price-aware physical dispatch', () => {
  afterEach(() => vi.unstubAllEnvs());

  it('publishes the full 3-day forecast-aware economic plan when armed', async () => {
    vi.stubEnv('SOLINTEG_CONTROL_ARMED', '1');
    const makeDay = (date: string) => Array.from({ length: 96 }, (_, i): OptimizerSlot => ({
      ...ALL_SLOTS[0],
      startTime: `${date}T${String(Math.floor(i / 4)).padStart(2, '0')}:${String((i % 4) * 15).padStart(2, '0')}:00`,
      priceSource: 'forecast',
    }));
    const actualToday = ALL_SLOTS.map((s) => ({ ...s, priceSource: 'actual' as const }));
    const threeDayInputs = [...actualToday, ...makeDay('2026-01-16'), ...makeDay('2026-01-17')];
    vi.mocked(buildOptimizerSlots).mockReturnValue([...threeDayInputs, ...makeDay('2026-01-18').slice(0, 4)]);
    const result = await producePlan();
    expect(result.data).toBe(PRICE_DATA);
    expect(optimizeDispatch).toHaveBeenCalledTimes(1);
    expect(optimizeDispatch).toHaveBeenCalledWith(threeDayInputs, 7.5, expect.anything());
    expect(logOptimizerRun).toHaveBeenCalledWith(
      PRICE_DATA.today, PRICE_DATA.hasTomorrow, 7.5, threeDayInputs, DISPATCH_FIXTURE, true,
    );
    expect(result.dispatchSchedule).toBe(DISPATCH_FIXTURE); // same 3-day physically published plan
    expect(result.controlPlanEndTime).toBe(DISPATCH_FIXTURE.at(-1)?.startTime);
  });

  it('also allows 3-day forecast planning for an explicitly disarmed shadow instance', async () => {
    vi.stubEnv('SOLINTEG_CONTROL_ARMED', '0');
    const makeDay = (date: string) => Array.from({ length: 96 }, (_, i): OptimizerSlot => ({
      ...ALL_SLOTS[0],
      startTime: `${date}T${String(Math.floor(i / 4)).padStart(2, '0')}:${String((i % 4) * 15).padStart(2, '0')}:00`,
      priceSource: 'forecast',
    }));
    const expanded = [...ALL_SLOTS, ...makeDay('2026-01-16'), ...makeDay('2026-01-17')];
    vi.mocked(buildOptimizerSlots).mockReturnValue(expanded);
    await producePlan();
    expect(optimizeDispatch).toHaveBeenCalledWith(expanded, 7.5, expect.anything());
  });
});

describe('producePlan — slot-index clamping', () => {
  it('clamps a negative slot index to the start of the array rather than dropping everything', async () => {
    vi.mocked(currentSlotIndexInPrices).mockReturnValue(-3);

    await producePlan();

    expect(optimizeDispatch).toHaveBeenCalledWith(ALL_SLOTS, 7.5, expect.anything());
  });

  it('a slot index past the end of the loaded prices yields an empty optimizer window, not a crash', async () => {
    vi.mocked(currentSlotIndexInPrices).mockReturnValue(ALL_SLOTS.length + 5);

    const result = await producePlan();

    expect(optimizeDispatch).toHaveBeenCalledWith([], 7.5, expect.anything());
    expect(result.dispatchSchedule).toBe(DISPATCH_FIXTURE);
  });
});

describe('producePlan — prices outage', () => {
  it('degrades to a priceless plan without ever touching the optimizer', async () => {
    const errSpy = silenceConsoleError();
    vi.mocked(fetchPricesUncached).mockRejectedValue(new Error('elprisetjustnu 503'));

    const result = await producePlan();

    expect(result.data).toBeNull();
    expect(result.dispatchSchedule).toBeNull();
    expect(result.solarProfiles).toEqual({});
    expect(buildOptimizerSlots).not.toHaveBeenCalled();
    expect(optimizeDispatch).not.toHaveBeenCalled();
    expect(logPriceSnapshot).not.toHaveBeenCalled();
    expect(logOptimizerRun).not.toHaveBeenCalled();
    errSpy.mockRestore();
  });
});

describe('producePlan — solar forecast fallback chain (SOLAR_FORECAST_MODEL=metno_nordic)', () => {
  it('falls back to a direct MET Norway fetch when Open-Meteo fails', async () => {
    const errSpy = silenceConsoleError();
    vi.mocked(fetchSolarForecastSources).mockRejectedValue(new Error('open-meteo 502'));
    vi.mocked(fetchSolarForecastDirect).mockResolvedValue({ direct: [9] });

    const result = await producePlan();

    expect(fetchSolarForecastDirect).toHaveBeenCalled();
    expect(result.solarForecast?.direct?.[0]).toBe(9);
    errSpy.mockRestore();
  });

  it('falls back to climatology (null) when both solar forecast sources fail, but still plans', async () => {
    const errSpy = silenceConsoleError();
    vi.mocked(fetchSolarForecastSources).mockRejectedValue(new Error('open-meteo 502'));
    vi.mocked(fetchSolarForecastDirect).mockRejectedValue(new Error('metno-thredds timeout'));

    const result = await producePlan();

    expect(result.solarForecast).toBeNull();
    expect(result.dispatchSchedule).toBe(DISPATCH_FIXTURE); // data still present -> plan still runs
    errSpy.mockRestore();
  });
});

describe('producePlan — persisted last-good solar forecast', () => {
  const STALE_FORECAST = { '2026-01-15': new Array(96).fill(0.4) };

  it('persists a live forecast so a later outage has something better than climatology', async () => {
    await producePlan();
    expect(saveSolarForecast).toHaveBeenCalledWith(FORECAST.metno_nordic);
  });

  it('uses the persisted forecast when every live source is down, and tags the slots stale', async () => {
    const errSpy = silenceConsoleError();
    vi.mocked(fetchSolarForecastSources).mockRejectedValue(new Error('open-meteo 502'));
    vi.mocked(fetchSolarForecastDirect).mockRejectedValue(new Error('met.no thredds fetch failed: 400'));
    vi.mocked(readFreshSolarForecast).mockResolvedValue(STALE_FORECAST);

    const result = await producePlan();

    // Hours-old real weather, not a decades-long monthly average — and flagged as such, so a
    // degraded plan is visible in telemetry rather than passing for a healthy one.
    expect(buildOptimizerSlots).toHaveBeenCalledWith(
      PRICE_DATA, STALE_FORECAST, PROFILES, TEMP_BY_DATE, LIVE_LOAD, true,
    );
    expect(result.solarForecast).toBe(STALE_FORECAST);
    errSpy.mockRestore();
  });

  it('never re-persists a forecast that came off disk (that would refresh its own timestamp)', async () => {
    const errSpy = silenceConsoleError();
    vi.mocked(fetchSolarForecastSources).mockRejectedValue(new Error('open-meteo 502'));
    vi.mocked(fetchSolarForecastDirect).mockRejectedValue(new Error('thredds 400'));
    vi.mocked(readFreshSolarForecast).mockResolvedValue(STALE_FORECAST);

    await producePlan();

    // Without this, one stale file would keep re-stamping itself and never age out.
    expect(saveSolarForecast).not.toHaveBeenCalled();
    errSpy.mockRestore();
  });

  it('falls through to climatology when there is no fresh-enough persisted forecast', async () => {
    const errSpy = silenceConsoleError();
    vi.mocked(fetchSolarForecastSources).mockRejectedValue(new Error('open-meteo 502'));
    vi.mocked(fetchSolarForecastDirect).mockRejectedValue(new Error('thredds 400'));
    vi.mocked(readFreshSolarForecast).mockResolvedValue(null);

    const result = await producePlan();

    // null forecast -> slotSolarKwh tags every slot 'typical'; NOT flagged stale, because
    // "flying blind" and "degraded but weather-aware" must stay distinguishable.
    expect(buildOptimizerSlots).toHaveBeenCalledWith(
      PRICE_DATA, null, PROFILES, TEMP_BY_DATE, LIVE_LOAD, false,
    );
    expect(result.solarForecast).toBeNull();
    errSpy.mockRestore();
  });
});

describe('producePlan — temperature forecast fallback chain (SOLAR_FORECAST_MODEL=metno_nordic)', () => {
  it('falls back to a direct MET Norway fetch when Open-Meteo fails', async () => {
    const errSpy = silenceConsoleError();
    vi.mocked(fetchDailyMeanTemp).mockRejectedValue(new Error('open-meteo 502'));
    vi.mocked(fetchDailyMeanTempDirect).mockResolvedValue({ '2026-01-15': -5 });

    await producePlan();

    expect(buildOptimizerSlots).toHaveBeenCalledWith(
      PRICE_DATA, FORECAST.metno_nordic, PROFILES, { '2026-01-15': -5 }, LIVE_LOAD, false,
    );
    errSpy.mockRestore();
  });

  it('falls back to the seasonal load model (null tempByDate) when both temp sources fail', async () => {
    const errSpy = silenceConsoleError();
    vi.mocked(fetchDailyMeanTemp).mockRejectedValue(new Error('open-meteo 502'));
    vi.mocked(fetchDailyMeanTempDirect).mockRejectedValue(new Error('metno-thredds timeout'));

    const result = await producePlan();

    expect(buildOptimizerSlots).toHaveBeenCalledWith(
      PRICE_DATA, FORECAST.metno_nordic, PROFILES, null, LIVE_LOAD, false,
    );
    expect(result.dispatchSchedule).toBe(DISPATCH_FIXTURE);
    errSpy.mockRestore();
  });
});

describe('producePlan — optimizer failure is non-fatal', () => {
  it('a throwing optimizer leaves the rest of the plan intact instead of rejecting producePlan', async () => {
    const errSpy = silenceConsoleError();
    vi.mocked(optimizeDispatch).mockImplementation(() => {
      throw new Error('DP blew up');
    });

    const result = await producePlan();

    expect(result.data).toBe(PRICE_DATA);
    expect(result.dispatchSchedule).toBeNull();
    expect(logOptimizerRun).not.toHaveBeenCalled();
    errSpy.mockRestore();
  });
});
