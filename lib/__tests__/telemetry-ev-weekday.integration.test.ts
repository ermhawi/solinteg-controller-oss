import { describe, it, expect, beforeAll, afterAll, vi } from 'vitest';
import { DatabaseSync } from 'node:sqlite';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { writeFileSync, rmSync } from 'node:fs';

const root = join(tmpdir(), `ev-weekday-profile-${process.pid}-${Date.now()}`);
const dbPath = root + '.sqlite';
const evPath = root + '-hours.json';
const now = new Date('2026-07-15T00:00:00Z');
const evHour = '2026-07-14T08:00:00Z'; // Tuesday 10:00 Stockholm
process.env.TELEMETRY_DB_PATH = dbPath;
process.env.EV_LOAD_HOURLY_PATH = evPath;

let telemetry: typeof import('../telemetry');
let load: typeof import('../load');

beforeAll(async () => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(now);
  const db = new DatabaseSync(dbPath);
  db.exec(`CREATE TABLE readings (
    id INTEGER PRIMARY KEY, timestamp TEXT, soc_pct REAL, soc_kwh REAL,
    pv_w INTEGER, grid_w INTEGER, battery_w INTEGER, inverter_ac_w INTEGER,
    house_load_w INTEGER, work_mode TEXT, work_mode_raw INTEGER)`);
  const stmt = db.prepare('INSERT INTO readings (timestamp, house_load_w) VALUES (?, ?)');
  const start = Date.parse('2026-07-01T00:00:00Z');
  db.exec('BEGIN');
  for (let day = 0; day < 14; day++) {
    for (let hour = 0; hour < 24; hour++) {
      const stamp = new Date(start + day * 86_400_000 + hour * 3_600_000);
      const weekend = [0, 6].includes(stamp.getUTCDay());
      const normal = hour === 8 ? (weekend ? 3800 : 900) : 600;
      const watts = stamp.toISOString().slice(0, 13) === evHour.slice(0, 13) ? 2280 : normal;
      stmt.run(stamp.toISOString(), watts);
      stmt.run(new Date(stamp.getTime() + 1_800_000).toISOString(), watts);
    }
  }
  db.exec('COMMIT');
  db.close();
  writeFileSync(evPath, JSON.stringify({
    version: 1,
    source: 'waveshare_evcc_fc03_ict2',
    quality: 'full_hour_measured_current_nominal_230v',
    generatedAtUtc: '2026-07-14T23:58:00Z',
    hours: [{ utcHour: evHour, evAvgW: 1380, samples: 120 }],
  }));
  telemetry = await import('../telemetry');
  load = await import('../load');
});

afterAll(() => {
  vi.useRealTimers();
  delete process.env.EV_LOAD_HOURLY_PATH;
  rmSync(dbPath, { force: true });
  rmSync(evPath, { force: true });
});

describe('weekday/weekend household load minus verified EVCC history', () => {
  it('does not mistake weekend peaks or measured vehicle charging for weekday reserve', () => {
    const profile = telemetry.readTrailingLoadProfile(14, 5)!;
    expect(profile).not.toBeNull();
    expect(profile.evCorrectedHours).toBe(1);
    expect(profile.weekdayHourKwh?.[10]).toBeCloseTo(.9, 3);
    expect(profile.weekdayHourHighKwh?.[10]).toBeCloseTo(.9, 3);
    expect(profile.weekendHourKwh?.[10]).toBeCloseTo(3.8, 3);
    expect(profile.weekendHourHighKwh?.[10]).toBeCloseTo(3.8, 3);
    expect(profile.hourHighKwh?.[10]).toBeCloseTo(3.8, 3);
    expect(load.slotConsumptionFromLive(profile, '2026-07-16T10:00:00', null))
      .toBeCloseTo(.9 / 4, 3); // Thursday
    expect(load.slotConsumptionHighFromLive(profile, '2026-07-16T10:00:00', null))
      .toBeCloseTo(.9 / 4, 3);
    expect(load.slotConsumptionHighFromLive(profile, '2026-07-18T10:00:00', null))
      .toBeCloseTo(3.8 / 4, 3); // Saturday
  });

  it('reverts to original raw history if evidence disappears, without touching readings', () => {
    rmSync(evPath, { force: true });
    const profile = telemetry.readTrailingLoadProfile(14, 5)!;
    expect(profile.evCorrectedHours).toBe(0);
    expect(profile.weekdayHourKwh?.[10]).toBeGreaterThan(1.0);
    const db = new DatabaseSync(dbPath);
    expect(db.prepare('SELECT house_load_w FROM readings WHERE timestamp=?')
      .get(evHour.replace('Z', '.000Z'))).toMatchObject({ house_load_w: 2280 });
    db.close();
  });
});
