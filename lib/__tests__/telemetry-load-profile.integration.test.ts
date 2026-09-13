import { describe, it, expect, beforeAll, afterAll, vi } from 'vitest';
import { DatabaseSync } from 'node:sqlite';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { rmSync } from 'node:fs';

// Unique per run — see telemetry-economics.integration.test.ts for why (the module-level handle
// stays open through afterAll, so a recycled path can inherit stale tables).
const DB_PATH = join(tmpdir(), `telemetry-load-profile-test-${process.pid}-${Date.now()}.db`);
process.env.TELEMETRY_DB_PATH = DB_PATH;

let telemetry: typeof import('../telemetry');

/**
 * `readTrailingLoadProfile`'s per-hour HIGH quantile (OPTIMIZER-AUDIT.md G4) — the measured
 * replacement for a single flat LOAD_FORECAST_MARGIN guess. The mean was already covered
 * indirectly; the quantile is new, and its whole value is that it differs BY HOUR.
 */
const HOURS_PER_DAY = 24;

/**
 * The clock is PINNED for this file, because the fixture below is anchored to `now` and the
 * profile it exercises buckets by Stockholm-local hour. Against a live clock that combination
 * failed roughly half the time, for two independent reasons — both measured by sweeping the
 * anchor (see the commit that added this):
 *
 *  1. `start` was `Date.now() - days×86 400 000`, so its MINUTE-OF-HOUR was whatever the wall
 *     clock happened to be. The second reading of each hour is written at `t + 30 min`, which
 *     lands in the NEXT UTC hour whenever that minute is ≥ 30 — carrying the previous hour's
 *     value with it. The volatile hour then appears twice: once genuinely, once as a smear.
 *     Failed for anchor minutes 30-59, i.e. 50% of runs. (2026-09-13 it ran at :48.)
 *  2. Even anchored to a whole hour, a trailing window that CONTAINS a DST transition maps one
 *     UTC hour onto two local ones, splitting the volatile hour the same way. Failed on 8 days
 *     of 2026 — the two windows following the March and October transitions.
 *
 * A fixed whole-hour instant in mid-July has neither property, and makes the file deterministic
 * rather than merely lucky. Nothing here is testing DST or clock behaviour; that belongs in
 * prices.test.ts / horizon.test.ts, which cover it deliberately.
 */
const PINNED_NOW = new Date('2026-07-15T00:00:00.000Z');

function seed(days: number, loadForDayHour: (day: number, hourUtc: number) => number) {
  const db = new DatabaseSync(DB_PATH);
  db.exec(`CREATE TABLE readings (
    id INTEGER PRIMARY KEY, timestamp TEXT, soc_pct REAL, soc_kwh REAL,
    pv_w INTEGER, grid_w INTEGER, battery_w INTEGER, inverter_ac_w INTEGER,
    house_load_w INTEGER, work_mode TEXT, work_mode_raw INTEGER)`);
  const ins = db.prepare('INSERT INTO readings (timestamp, house_load_w) VALUES (?, ?)');
  // Anchored a whole number of days back from the pinned instant, which is itself on a whole
  // hour — so every reading lands at :00 or :30 of the hour it belongs to.
  const start = new Date(Date.now() - days * 86_400_000);
  for (let d = 0; d < days; d++) {
    for (let h = 0; h < HOURS_PER_DAY; h++) {
      const t = new Date(start.getTime() + d * 86_400_000 + h * 3_600_000);
      // Two readings per hour, so the SQL's per-bucket AVG is exercised rather than trivial.
      ins.run(t.toISOString(), loadForDayHour(d, t.getUTCHours()));
      ins.run(new Date(t.getTime() + 1_800_000).toISOString(), loadForDayHour(d, t.getUTCHours()));
    }
  }
  db.close();
}

afterAll(() => {
  vi.useRealTimers();
  try {
    rmSync(DB_PATH, { force: true });
  } catch {
    /* Windows keeps the handle; the unique name makes the leftover harmless */
  }
});

/** Stockholm's UTC offset in minutes, computed independently of lib/ so this guard does not
 *  lean on the code it is guarding. */
function stockholmOffsetMinutes(d: Date): number {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Europe/Stockholm',
    timeZoneName: 'longOffset',
  }).formatToParts(d);
  const name = parts.find((p) => p.type === 'timeZoneName')!.value; // e.g. "GMT+02:00"
  const m = name.match(/GMT([+-])(\d{2}):(\d{2})/);
  if (!m) return 0; // "GMT" alone means +00:00
  return (m[1] === '-' ? -1 : 1) * (Number(m[2]) * 60 + Number(m[3]));
}

describe('the pinned instant this file depends on', () => {
  // Both properties are load-bearing (see PINNED_NOW's comment). Asserted rather than trusted,
  // because moving PINNED_NOW to a date that breaks either would surface as the SHAPE assertions
  // below failing — which reads like the quantile regressed, not like the fixture moved.
  it('sits on a whole hour and spans no DST transition', () => {
    expect(PINNED_NOW.getTime() % 3_600_000).toBe(0);
    const windowStart = new Date(PINNED_NOW.getTime() - 14 * 86_400_000);
    expect(stockholmOffsetMinutes(windowStart)).toBe(stockholmOffsetMinutes(PINNED_NOW));
  });
});

describe('readTrailingLoadProfile — per-hour high quantile', () => {
  beforeAll(async () => {
    // Hour 20 is volatile (a big evening load on one day in three), hour 3 is nearly constant.
    // That contrast is the entire point: a flat margin says the same thing about both.
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(PINNED_NOW);
    seed(14, (day, hourUtc) => {
      if (hourUtc === 20) return day % 3 === 0 ? 4000 : 800;
      if (hourUtc === 3) return 300;
      return 600;
    });
    telemetry = await import('../telemetry');
  });

  // Buckets are keyed by LOCAL hour while the fixture seeds by UTC, so the tests locate the hours
  // by their shape rather than by index — which is also the more honest assertion.
  const ratios = () => {
    const profile = telemetry.readTrailingLoadProfile(14, 5)!;
    return profile.hourHighKwh!.map((high, h) => high / profile.hourKwh[h]);
  };

  it('reports a quantile well above the mean exactly where the hour is volatile', () => {
    const profile = telemetry.readTrailingLoadProfile(14, 5);
    expect(profile).not.toBeNull();
    expect(profile!.hourHighKwh).toBeDefined();

    // Exactly one seeded hour is volatile (4000 W on one day in three, 800 W otherwise), and its
    // reserve must exceed what a flat 1.15 margin would have provided.
    const volatile = ratios().filter((r) => r > 1.5);
    expect(volatile).toHaveLength(1);
    expect(volatile[0]).toBeGreaterThan(1.15);
  });

  it('leaves the steady hours at their mean, instead of reserving for a spread they do not have', () => {
    // The over-holding a flat margin causes, and the reason a per-hour figure is worth the code:
    // every other seeded hour is constant, so its reserve must be its mean.
    const steady = ratios().filter((r) => r <= 1.5);
    expect(steady).toHaveLength(23);
    steady.forEach((r) => expect(r).toBeCloseTo(1, 6));
  });

  it('never reports a quantile below the mean', () => {
    const profile = telemetry.readTrailingLoadProfile(14, 5)!;
    profile.hourHighKwh!.forEach((h, i) => expect(h).toBeGreaterThanOrEqual(profile.hourKwh[i] - 1e-9));
  });
});
