import { describe, it, expect, beforeAll, afterAll } from 'vitest';
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

function seed(days: number, loadForDayHour: (day: number, hourUtc: number) => number) {
  const db = new DatabaseSync(DB_PATH);
  db.exec(`CREATE TABLE readings (
    id INTEGER PRIMARY KEY, timestamp TEXT, soc_pct REAL, soc_kwh REAL,
    pv_w INTEGER, grid_w INTEGER, battery_w INTEGER, inverter_ac_w INTEGER,
    house_load_w INTEGER, work_mode TEXT, work_mode_raw INTEGER)`);
  const ins = db.prepare('INSERT INTO readings (timestamp, house_load_w) VALUES (?, ?)');
  // Anchored a few days back from now so the whole window falls inside the trailing period.
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
  try {
    rmSync(DB_PATH, { force: true });
  } catch {
    /* Windows keeps the handle; the unique name makes the leftover harmless */
  }
});

describe('readTrailingLoadProfile — per-hour high quantile', () => {
  beforeAll(async () => {
    // Hour 20 is volatile (a big evening load on one day in three), hour 3 is nearly constant.
    // That contrast is the entire point: a flat margin says the same thing about both.
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
