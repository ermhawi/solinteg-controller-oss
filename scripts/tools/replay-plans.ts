// Replay logged optimizer runs against what ACTUALLY happened, under alternative planning options.
//
// This is the harness the risk knobs should be tuned with, and its absence is why several of them
// were "placed estimates" rather than fits: DEFERRAL_RATE_ORE_PER_KWH_HOUR, DEFERRAL_HORIZON_HOURS,
// SOLAR_RISK_PREMIUM_ORE_PER_KWH, FORECAST_RISK_PREMIUM_ORE, LOAD_FORECAST_MARGIN and
// TERMINAL_VALUE_FACTOR all change dispatch, and until now the only way to judge a change was to
// reason about it. Reasoning found real bugs, but it cannot tell you whether 0.4 beats 0.2.
//
// WHAT IT DOES, per logged plan:
//   1. take the plan's own inputs (optimizer_runs.inputs_json — prices, forecast solar, forecast
//      load) and its start SoC;
//   2. re-run optimizeDispatch over them with each candidate option set;
//   3. score every resulting trajectory against ACTUAL solar and load for the same slots, using
//      evaluateDispatch — the DP's own arithmetic, so a trajectory's value means the same thing
//      here as it does inside the optimizer.
//
// The key point, and the reason this is not circular: the PLAN is built from forecasts, the SCORE
// is computed against actuals. An option set that looks better only because it trusts the forecast
// more will show up as worse here, which is exactly the question the risk knobs exist to answer.
//
// WHAT IT IS NOT:
//   * not the hindsight oracle. The oracle asks "what was the best possible dispatch?" and reports
//     regret. This asks the narrower and more actionable "would THIS setting have done better than
//     THAT one, on the days we actually saw?".
//   * not an execution model. It scores the PLAN, not what the inverter did with it — guard vetoes,
//     divergence skips and manual intervention are invisible here. Use guard-rates.py alongside it.
//   * not a significance test. A handful of days of öre-scale differences is noise; look for a
//     consistent sign across many days before moving a constant.
//
// Usage:
//   TELEMETRY_DB_PATH=/path/to/telemetry.db npx tsx scripts/tools/replay-plans.ts [--days 30]
//     [--per-day]
import { DatabaseSync } from 'node:sqlite';
import { optimizeDispatch, evaluateDispatch, terminalEnergyValueOre } from '../../lib/optimizer';
import type { OptimizerSlot } from '../../lib/optimizer';
import { bucketActuals } from '../../lib/oracle';
import type { OracleReadingRow } from '../../lib/oracle';
import { stockholmMidnightUtc } from '../../lib/prices';
import {
  LOAD_FORECAST_MARGIN,
  DEFERRAL_RATE_ORE_PER_KWH_HOUR,
  DEFERRAL_HORIZON_HOURS,
  SOLAR_RISK_PREMIUM_ORE_PER_KWH,
  FORECAST_RISK_PREMIUM_ORE,
  TERMINAL_VALUE_FACTOR,
} from '../../lib/constants';

const DB_PATH = process.env.TELEMETRY_DB_PATH ?? '/opt/solinteg/telemetry.db';
const args = process.argv.slice(2);
const days = Number(args[args.indexOf('--days') + 1]) || 30;
const perDay = args.includes('--per-day');

type Candidate = { name: string; opts: Parameters<typeof optimizeDispatch>[2] };

/** Production settings, then one-knob-at-a-time variants. Each row answers "what does THIS knob
 *  buy or cost?", which is the only comparison a reader can act on — a grid search over all of
 *  them at once produces a winner nobody can explain or defend. */
const CANDIDATES: Candidate[] = [
  { name: 'production', opts: {
    loadFactor: LOAD_FORECAST_MARGIN,
    deferralRateOrePerKwhHour: DEFERRAL_RATE_ORE_PER_KWH_HOUR,
    deferralHorizonHours: DEFERRAL_HORIZON_HOURS,
    solarRiskPremiumOre: SOLAR_RISK_PREMIUM_ORE_PER_KWH,
    forecastRiskPremiumOre: FORECAST_RISK_PREMIUM_ORE,
  } },
  { name: 'no risk knobs', opts: { loadFactor: LOAD_FORECAST_MARGIN } },
  { name: 'no deferral', opts: {
    loadFactor: LOAD_FORECAST_MARGIN,
    solarRiskPremiumOre: SOLAR_RISK_PREMIUM_ORE_PER_KWH,
    forecastRiskPremiumOre: FORECAST_RISK_PREMIUM_ORE,
  } },
  { name: 'no solar premium', opts: {
    loadFactor: LOAD_FORECAST_MARGIN,
    deferralRateOrePerKwhHour: DEFERRAL_RATE_ORE_PER_KWH_HOUR,
    deferralHorizonHours: DEFERRAL_HORIZON_HOURS,
    forecastRiskPremiumOre: FORECAST_RISK_PREMIUM_ORE,
  } },
  { name: 'no forecast premium', opts: {
    loadFactor: LOAD_FORECAST_MARGIN,
    deferralRateOrePerKwhHour: DEFERRAL_RATE_ORE_PER_KWH_HOUR,
    deferralHorizonHours: DEFERRAL_HORIZON_HOURS,
    solarRiskPremiumOre: SOLAR_RISK_PREMIUM_ORE_PER_KWH,
  } },
  { name: 'no load margin', opts: {
    loadFactor: 1,
    deferralRateOrePerKwhHour: DEFERRAL_RATE_ORE_PER_KWH_HOUR,
    deferralHorizonHours: DEFERRAL_HORIZON_HOURS,
    solarRiskPremiumOre: SOLAR_RISK_PREMIUM_ORE_PER_KWH,
    forecastRiskPremiumOre: FORECAST_RISK_PREMIUM_ORE,
  } },
];

let db: DatabaseSync;
try {
  db = new DatabaseSync(DB_PATH, { readOnly: true });
} catch {
  // A raw ERR_SQLITE_ERROR stack says nothing about the one thing that is almost always wrong.
  console.error(
    `Cannot open ${DB_PATH} read-only.\n` +
      'Set TELEMETRY_DB_PATH to your telemetry.db (the default lives on the controller box and is ' +
      'not readable from a dev checkout — copy it over, or run this there).',
  );
  process.exit(1);
}

const since = new Date(Date.now() - days * 86_400_000).toISOString();
const runs = db
  .prepare(
    `SELECT price_date, logged_at, start_soc_kwh, inputs_json, dispatch_json FROM optimizer_runs
     WHERE logged_at >= ? ORDER BY logged_at`,
  )
  .all(since) as { price_date: string; logged_at: string; start_soc_kwh: number; inputs_json: string; dispatch_json: string }[];

if (runs.length === 0) {
  console.error(`No optimizer_runs in the last ${days} days at ${DB_PATH}.`);
  process.exit(1);
}

// One plan per day: the LAST of the day, which had the most information and is the one whose
// choices the evening actually followed. Scoring every replan would weight busy days more heavily
// for no reason — they are re-solving the same day, not making independent decisions.
const byDate = new Map<string, (typeof runs)[number]>();
for (const run of runs) byDate.set(run.price_date, run);

/** Readings for the window a plan covers, in the oracle's row shape. */
function readingsFor(startIso: string, endIso: string): OracleReadingRow[] {
  return db
    .prepare(
      `SELECT timestamp, pv_w, house_load_w, soc_kwh, grid_w FROM readings
       WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp`,
    )
    .all(startIso, endIso) as unknown as OracleReadingRow[];
}

type Totals = { valueOre: number; gridToBatteryKwh: number; endSocKwh: number; days: number };
const totals = new Map<string, Totals>(
  CANDIDATES.map((c) => [c.name, { valueOre: 0, gridToBatteryKwh: 0, endSocKwh: 0, days: 0 }]),
);

let scoredDays = 0;
let skippedThinCoverage = 0;

for (const [date, run] of [...byDate.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
  const inputs = JSON.parse(run.inputs_json) as OptimizerSlot[];
  if (inputs.length === 0) continue;

  // The plan's own first slot is where it starts (plan.ts slices at "now" before solving), so the
  // actuals window is anchored there — not at midnight, which would misalign every mid-day replan.
  const firstLocal = inputs[0].startTime;
  const [y, m, d] = firstLocal.slice(0, 10).split('-').map(Number);
  const midnight = stockholmMidnightUtc(y, m - 1, d).getTime();
  const minutes = Number(firstLocal.slice(11, 13)) * 60 + Number(firstLocal.slice(14, 16));
  const windowStartMs = midnight + minutes * 60_000;
  const windowEndMs = windowStartMs + inputs.length * 900_000;

  const readings = readingsFor(new Date(windowStartMs).toISOString(), new Date(windowEndMs).toISOString());
  const actuals = bucketActuals(readings, windowStartMs, inputs.length);
  // A day the poller mostly missed cannot score anything: the "actual" it would be measured
  // against is interpolation. Better to drop it loudly than to average it in quietly.
  if (actuals.coverage < 0.9) {
    skippedThinCoverage++;
    continue;
  }

  // What really happened, priced with the DP's own arithmetic. The forecast in inputs_json is what
  // the plan SAW; this is what it should have been judged against.
  const actualSlots: OptimizerSlot[] = inputs.map((slot, i) => ({
    ...slot,
    solarKwh: actuals.solarKwh[i],
    consumptionKwh: actuals.loadKwh[i],
    consumptionReserveKwh: undefined, // a reserve is a planning input, never part of the truth
  }));

  const row: string[] = [date];
  for (const candidate of CANDIDATES) {
    const terminalValueOrePerKwh = terminalEnergyValueOre(inputs) * TERMINAL_VALUE_FACTOR;
    const dispatch = optimizeDispatch(inputs, run.start_soc_kwh, {
      ...candidate.opts,
      terminalValueOrePerKwh,
    });
    // Scored WITHOUT the load margin and WITHOUT a terminal credit: against actuals there is no
    // forecast to hedge and no future to value — only the cash the day really produced.
    const econ = evaluateDispatch(actualSlots, dispatch, run.start_soc_kwh);
    const t = totals.get(candidate.name)!;
    t.valueOre += econ.valueOre;
    t.gridToBatteryKwh += dispatch.reduce((sum, s) => sum + s.gridToBatteryKwh, 0);
    t.endSocKwh += dispatch[dispatch.length - 1].socAfter;
    t.days++;
    row.push((econ.valueOre / 100).toFixed(2));
  }
  scoredDays++;
  if (perDay) console.log(row.join('\t'));
}

db.close();

if (scoredDays === 0) {
  console.error(`No day had enough reading coverage to score (${skippedThinCoverage} skipped).`);
  process.exit(1);
}

console.log(`\nReplayed ${scoredDays} day(s) of logged plans against measured solar and load.`);
if (skippedThinCoverage > 0) {
  console.log(`${skippedThinCoverage} day(s) skipped for reading coverage below 90%.`);
}
console.log('Plans are built from the FORECAST the plan saw; scores are against what HAPPENED.\n');

const baseline = totals.get('production')!;
console.log(
  `${'candidate'.padEnd(22)}${'value (kr)'.padStart(12)}${'vs production'.padStart(15)}` +
    `${'kr/day'.padStart(10)}${'grid->batt'.padStart(12)}${'mean endSoC'.padStart(13)}`,
);
console.log('─'.repeat(84));
for (const candidate of CANDIDATES) {
  const t = totals.get(candidate.name)!;
  const delta = (t.valueOre - baseline.valueOre) / 100;
  console.log(
    candidate.name.padEnd(22) +
      (t.valueOre / 100).toFixed(2).padStart(12) +
      (candidate.name === 'production' ? '—' : (delta >= 0 ? '+' : '') + delta.toFixed(2)).padStart(15) +
      (t.valueOre / 100 / t.days).toFixed(2).padStart(10) +
      t.gridToBatteryKwh.toFixed(1).padStart(12) +
      (t.endSocKwh / t.days).toFixed(2).padStart(13),
  );
}

console.log(
  '\nA POSITIVE "vs production" means that variant would have earned more over these days — i.e.\n' +
    'the knob it removes cost money on the days we saw. Before acting on one: check the sign is\n' +
    'consistent across days (--per-day), that the window covers the season you care about, and\n' +
    'remember this scores PLANS, not execution — pair it with guard-rates.py.',
);
