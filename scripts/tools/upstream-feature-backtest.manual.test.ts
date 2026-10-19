/**
 * Manual, read-only replay of the two opt-in upstream risk refinements.
 *
 * Export representative optimizer_runs and actual readings to a private temporary SQLite
 * database first. NEVER use this test against the live writeable database as a shortcut.
 *
 * SOLINTEG_BACKTEST=1 TELEMETRY_DB_PATH=/tmp/...db
 *   npm test -- scripts/tools/upstream-feature-backtest.manual.test.ts --testTimeout=600000
 *
 * Why manual? 65 days x 3 replan windows x 4 DP candidates is too expensive for CI.
 * Our measured PV/load is a counterfactual input to the pricing model; it is NOT simulated
 * firmware execution. Paths overlap, so summed run deltas are NOT realized savings.
 */
import { DatabaseSync } from 'node:sqlite';
import { writeFileSync } from 'node:fs';
import { describe, it, expect } from 'vitest';
import { optimizeDispatch, evaluateDispatch, terminalEnergyValueOre } from '../../lib/optimizer';
import type { OptimizerSlot, DispatchSlot } from '../../lib/optimizer';
import { bucketActuals } from '../../lib/oracle';
import type { OracleReadingRow } from '../../lib/oracle';
import {
  LOAD_FORECAST_MARGIN,
  DEFERRAL_RATE_ORE_PER_KWH_HOUR,
  DEFERRAL_HORIZON_HOURS,
  SOLAR_RISK_PREMIUM_ORE_PER_KWH,
  SOLAR_RISK_LOOKAHEAD_HOURS,
  FORECAST_RISK_PREMIUM_ORE,
  BATTERY_SAVE_FORECAST_DRAIN_W,
  TERMINAL_VALUE_FACTOR,
} from '../../lib/constants';

type LoggedRun = {
  id: number;
  logged_at: string;
  price_date: string;
  start_soc_kwh: number;
  inputs_json: string;
  dispatch_json: string;
};

const names = ['current', 'solar-survival', 'deferral-need-window', 'both'] as const;
type Name = (typeof names)[number];
const switches: Record<Name, { solarRiskSurvival: boolean; deferralUntilNeeded: boolean }> = {
  current: { solarRiskSurvival: false, deferralUntilNeeded: false },
  'solar-survival': { solarRiskSurvival: true, deferralUntilNeeded: false },
  'deferral-need-window': { solarRiskSurvival: false, deferralUntilNeeded: true },
  both: { solarRiskSurvival: true, deferralUntilNeeded: true },
};
type Snapshot = {
  id: number;
  date: string;
  hour: number;
  n: number;
  coverage: number;
  forecastSolarKwh: number;
  actualSolarKwh: number;
  forecastLoadKwh: number;
  actualLoadKwh: number;
  fidelityFirst24: number;
  results: Record<Name, {
    valueOre: number;
    cashOre: number;
    wearOre: number;
    terminalValueOre: number;
    endSocKwh: number;
    gridToBatteryKwh: number;
    batteryDischargedKwh: number;
    changedFirst24Actions: number;
    changedFirstAction: boolean;
  }>;
};

function quantile(values: number[], q: number): number {
  const sorted = [...values].sort((a, b) => a - b);
  if (!sorted.length) return NaN;
  return sorted[Math.max(0, Math.min(sorted.length - 1, Math.floor((sorted.length - 1) * q)))];
}
function mean(values: number[]): number {
  return values.reduce((a, b) => a + b, 0) / Math.max(1, values.length);
}
function deltaStats(rows: Snapshot[], name: Name) {
  const deltas = rows.map(r => (r.results[name].valueOre - r.results.current.valueOre) / 100);
  const cashDeltas = rows.map(r => (r.results[name].cashOre - r.results.current.cashOre) / 100);
  const wearDeltas = rows.map(r => (r.results[name].wearOre - r.results.current.wearOre) / 100);
  const socDeltas = rows.map(r => r.results[name].endSocKwh - r.results.current.endSocKwh);
  const purchaseDeltas = rows.map(r => r.results[name].gridToBatteryKwh - r.results.current.gridToBatteryKwh);
  const positive = deltas.filter(x => x > 0.005).length;
  const negative = deltas.filter(x => x < -0.005).length;
  const changed = rows.filter(r => r.results[name].changedFirst24Actions > 0).length;
  const changedFirst = rows.filter(r => r.results[name].changedFirstAction).length;

  // Day is the unit, not 100 consecutive replans of the same future slots.
  const byDay = new Map<string, number[]>();
  for (let i = 0; i < rows.length; i++) {
    if (!byDay.has(rows[i].date)) byDay.set(rows[i].date, []);
    byDay.get(rows[i].date)!.push(deltas[i]);
  }
  const dayDeltas = [...byDay.values()].map(mean);
  const days = [...byDay.keys()].sort();
  // Conservative, reproducible block bootstrap of contiguous THREE-day blocks, to partially
  // account for the price horizon and forecasts overlapping adjacent calendar dates.
  const blockData = new Map<number, number[]>();
  for (let i = 0; i < days.length; i++) {
    const block = Math.floor(i / 3);
    if (!blockData.has(block)) blockData.set(block, []);
    blockData.get(block)!.push(dayDeltas[i]);
  }
  const blocks = [...blockData.values()].map(mean);
  let seed = 0x51f15e;
  const rand = () => {
    seed ^= seed << 13; seed ^= seed >>> 17; seed ^= seed << 5;
    return (seed >>> 0) / 4294967296;
  };
  const bootstrap = Array.from({ length: 1500 }, () =>
    mean(Array.from({ length: blocks.length }, () => blocks[Math.floor(rand() * blocks.length)])),
  );
  const dayMean = mean(dayDeltas);
  const ci = [quantile(bootstrap, 0.025), quantile(bootstrap, 0.975)];
  return {
    samples: deltas.length,
    calendarDays: days.length,
    effectiveThreeDayBlocks: blocks.length,
    meanDeltaKrPerReplay: mean(deltas),
    medianDeltaKrPerReplay: quantile(deltas, .5),
    p10DeltaKr: quantile(deltas, .1),
    p90DeltaKr: quantile(deltas, .9),
    meanDeltaKrPerDayRepresentativePlan: dayMean,
    bootstrap95PctMeanDeltaKrPerRepresentativePlan: ci,
    positiveRuns: positive,
    negativeRuns: negative,
    tiedRuns: deltas.length - positive - negative,
    positiveDays: dayDeltas.filter(x => x > 0.005).length,
    negativeDays: dayDeltas.filter(x => x < -0.005).length,
    changedFirst24hRuns: changed,
    changedImmediateActionRuns: changedFirst,
    meanCashDeltaKr: mean(cashDeltas),
    meanWearDeltaKr: mean(wearDeltas),
    meanEndSocDeltaKwh: mean(socDeltas),
    meanBatteryBuyDeltaKwh: mean(purchaseDeltas),
    byHour: [6, 14, 22].map(hour => {
      const a = rows.filter(r => r.hour === hour);
      const ds = a.map(r => (r.results[name].valueOre - r.results.current.valueOre) / 100);
      return { hour, samples: ds.length, meanDeltaKr: mean(ds), positive: ds.filter(x => x > 0.005).length, negative: ds.filter(x => x < -0.005).length, meanBatteryBuyDeltaKwh: mean(a.map(r => r.results[name].gridToBatteryKwh - r.results.current.gridToBatteryKwh)) };
    }),
  };
}

describe.runIf(process.env.SOLINTEG_BACKTEST === '1')('Sundsvall opt-in risk-model retrospective backtest', () => {
  it('scores representative historical plans against subsequent actual solar and house demand', () => {
    const path = process.env.TELEMETRY_DB_PATH;
    if (!path) throw new Error('Specify read-only TELEMETRY_DB_PATH for offline backtest');
    const db = new DatabaseSync(path, { readOnly: true });
    const logged = db.prepare('SELECT id, logged_at, price_date, start_soc_kwh, inputs_json, dispatch_json FROM optimizer_runs ORDER BY logged_at').all() as unknown as LoggedRun[];
    const latestReading = db.prepare('SELECT max(timestamp) as t FROM readings').get() as { t: string };
    const lastMs = Date.parse(latestReading.t);
    const query = db.prepare('SELECT timestamp,pv_w,house_load_w,soc_kwh,grid_w FROM readings WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp');

    const eligible: Snapshot[] = [];
    const skipped: Record<string, number> = {};
    const skip = (reason: string) => { skipped[reason] = (skipped[reason] ?? 0) + 1; };
    const limit = Math.max(1, Number(process.env.SOLINTEG_BACKTEST_LIMIT ?? logged.length));
    const maxSlots = Math.max(1, Number(process.env.SOLINTEG_BACKTEST_SLOTS ?? 1000));
    const ordered = logged.slice(0, limit);

    for (const run of ordered) {
      const inputs = JSON.parse(run.inputs_json) as OptimizerSlot[];
      const original = JSON.parse(run.dispatch_json) as DispatchSlot[];
      if (inputs.length < 48 || inputs.length > maxSlots || original.length !== inputs.length) {
        skip('invalid-horizon'); continue;
      }
      const localDate = inputs[0].startTime.slice(0, 10);
      const localHour = Number(inputs[0].startTime.slice(11, 13));
      // Anchor by the REAL UTC timestamp of the logged replan, not by adding local-wall
      // minutes to Stockholm midnight. That fails at DST transitions. Reject stale/misaligned
      // plan payloads: one Sept 1 run still contained Aug 31 slots, which must not be replayed
      // as if its observations came from an Aug 31 forecast.
      const loggedMs = Date.parse(run.logged_at);
      const slotMs = 15 * 60_000;
      const observedQuarter = Math.floor(loggedMs / slotMs) * slotMs;
      const timeLabel = (ms: number) => {
        const fmt = new Intl.DateTimeFormat('en-GB', {
          timeZone: 'Europe/Stockholm', year: 'numeric', month: '2-digit',
          day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
        });
        const p = Object.fromEntries(fmt.formatToParts(new Date(ms)).map(x => [x.type, x.value]));
        return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
      };
      const expectedFirstLabel = inputs[0].startTime.slice(0, 16);
      const candidates = [observedQuarter, observedQuarter + slotMs, observedQuarter - slotMs]
        .filter(ms => timeLabel(ms) === expectedFirstLabel)
        .sort((a, b) => Math.abs(a - loggedMs) - Math.abs(b - loggedMs));
      if (candidates.length === 0) { skip('stale-or-misaligned-plan-time'); continue; }
      const startMs = candidates[0];
      const endMs = startMs + inputs.length * slotMs;
      if (endMs > lastMs - 60_000) { skip('future-slots'); continue; }
      const readings = query.all(new Date(startMs).toISOString(), new Date(endMs).toISOString()) as unknown as OracleReadingRow[];
      const actual = bucketActuals(readings, startMs, inputs.length);
      if (actual.coverage < 0.98 || actual.zeroFilledSlots > 0) { skip('insufficient-actual-coverage'); continue; }

      const actualInputs: OptimizerSlot[] = inputs.map((s, i) => ({
        ...s,
        solarKwh: actual.solarKwh[i],
        consumptionKwh: actual.loadKwh[i],
        consumptionReserveKwh: undefined,
      }));

      const t = terminalEnergyValueOre(inputs) * TERMINAL_VALUE_FACTOR;
      const base = {
        loadFactor: LOAD_FORECAST_MARGIN,
        deferralRateOrePerKwhHour: DEFERRAL_RATE_ORE_PER_KWH_HOUR,
        deferralHorizonHours: DEFERRAL_HORIZON_HOURS,
        solarRiskPremiumOre: SOLAR_RISK_PREMIUM_ORE_PER_KWH,
        solarRiskLookaheadHours: SOLAR_RISK_LOOKAHEAD_HOURS,
        forecastRiskPremiumOre: FORECAST_RISK_PREMIUM_ORE,
        batterySaveDriftW: BATTERY_SAVE_FORECAST_DRAIN_W,
        terminalValueOrePerKwh: t,
        holdEnabled: false,
      };
      const evaluated = {} as Snapshot['results'];
      let current: DispatchSlot[] | undefined;
      for (const name of names) {
        const d = optimizeDispatch(inputs, run.start_soc_kwh, { ...base, ...switches[name] });
        if (name === 'current') current = d;
        if (!current || d.length !== inputs.length) throw new Error('Optimizer returned an incomplete trajectory');
        const econ = evaluateDispatch(actualInputs, d, run.start_soc_kwh, { terminalValueOrePerKwh: t });
        // Energy at horizon end is valued the SAME way for all candidates; otherwise an early
        // sale versus retained SoC reads as a fictitious cash gain.
        evaluated[name] = {
          valueOre: econ.valueOre,
          cashOre: econ.cashOre,
          wearOre: econ.wearOre,
          terminalValueOre: econ.valueOre - econ.cashOre + econ.wearOre,
          endSocKwh: d.at(-1)!.socAfter,
          gridToBatteryKwh: d.reduce((sum, s) => sum + s.gridToBatteryKwh, 0),
          batteryDischargedKwh: d.reduce((sum, s) => sum + s.batteryToLoadKwh + s.batteryToGridKwh, 0),
          changedFirst24Actions: d.slice(0, 96).filter((s, i) => s.action !== current![i].action).length,
          changedFirstAction: d[0].action !== current[0].action,
        };
      }
      const fidelityFirst24 = mean(original.slice(0, 96).map((s, i) => +(s.action === current![i].action)));
      eligible.push({
        id: run.id, date: localDate, hour: localHour, n: inputs.length, coverage: actual.coverage,
        forecastSolarKwh: inputs.reduce((sum, s) => sum + s.solarKwh, 0),
        actualSolarKwh: actual.solarKwh.reduce((a, b) => a + b, 0),
        forecastLoadKwh: inputs.reduce((sum, s) => sum + (s.consumptionKwh ?? 0), 0),
        actualLoadKwh: actual.loadKwh.reduce((a, b) => a + b, 0),
        fidelityFirst24,
        results: evaluated,
      });
    }
    db.close();
    expect(eligible.length).toBeGreaterThan(0);

    const summary = {
      description: 'Retrospective shadow replay, not physical realized performance. All plan horizons overlap.',
      generatedAt: new Date().toISOString(),
      gitRef: process.env.SOLINTEG_BACKTEST_GIT_REF ?? 'local working tree',
      selectedRuns: ordered.length, eligibleRuns: eligible.length, skipped,
      days: new Set(eligible.map(r => r.date)).size,
      meanCoverage: mean(eligible.map(r => r.coverage)),
      meanFirst24hActionFidelityVsRecorded: mean(eligible.map(r => r.fidelityFirst24)),
      meanForecastSolarKwh: mean(eligible.map(r => r.forecastSolarKwh)),
      meanActualSolarKwh: mean(eligible.map(r => r.actualSolarKwh)),
      meanForecastLoadKwh: mean(eligible.map(r => r.forecastLoadKwh)),
      meanActualLoadKwh: mean(eligible.map(r => r.actualLoadKwh)),
      variants: Object.fromEntries(names.filter(n => n !== 'current').map(n => [n, deltaStats(eligible, n)])),
      perRun: eligible.map(r => ({
        id: r.id, date: r.date, hour: r.hour, fidelityFirst24: r.fidelityFirst24,
        firstAction: Object.fromEntries(names.map(n => [n, r.results[n].changedFirstAction])),
        deltaKr: Object.fromEntries(names.filter(n => n !== 'current').map(n => [n, (r.results[n].valueOre - r.results.current.valueOre) / 100])),
      })),
    };
    const report = process.env.SOLINTEG_BACKTEST_REPORT_PATH ?? '/tmp/solinteg-feature-backtest-20261019-result.json';
    writeFileSync(report, JSON.stringify(summary, null, 2));
    console.log('Sundsvall offline backtest:', JSON.stringify({
      selectedRuns: summary.selectedRuns, eligibleRuns: summary.eligibleRuns, days: summary.days,
      skipped, meanCoverage: summary.meanCoverage,
      meanFirst24hActionFidelityVsRecorded: summary.meanFirst24hActionFidelityVsRecorded,
      variants: summary.variants,
      report,
    }, null, 2));
  }, 600_000);
});
