import { afterEach, describe, expect, it } from 'vitest';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { GET as summaryGET } from '../../app/api/ev/cost/summary/route';
import { GET as drivingGET } from '../../app/api/ev/cost/driving/route';

const original = { ...process.env };
const token = 'a-private-home-assistant-read-only-token-123456789';
afterEach(() => { process.env = { ...original }; });

function request(endpoint: string, query = '', bearer = token) {
  return new Request('http://localhost/api/ev/cost/' + endpoint + query, {
    headers: bearer ? { Authorization: 'Bearer ' + bearer } : {},
  });
}

const snapshot = {
  schema_version: 1,
  source: 'ev_cost_ledger',
  as_of_utc: '2026-10-28T12:00:00+00:00',
  ledger_revision: 'a'.repeat(64),
  model: 'weighted_average_stored_dc_v1',
  inventory: {
    stored_dc_kwh: 25.6,
    known_value_ore: 4352,
    inventory_value_ore: 4352,
    unpriced_dc_kwh: 0,
    average_ore_per_dc_kwh: 170,
    quality: 'estimated',
  },
  summary: {
    distance_km: 30,
    consumed_dc_kwh: 6.6,
    known_cost_ore: 1122,
    cost_ore: 1122,
    unpriced_dc_kwh: 0,
    kr_per_mil: 3.74,
    quality: 'estimated',
  },
  charged_ac_kwh_by_source: { grid: 20, solar: 10, public: 0, unknown: 0 },
  aux_consumed_dc_kwh: 1,
  direct_aux_ac_kwh: 2,
  direct_aux_known_cost_ore: 200,
  direct_aux_cost_ore: 200,
  direct_aux_unpriced_ac_kwh: 0,
  event_count: 5,
  driving: [
    {
      event_id: 'trip-1', started_at_utc: '2026-10-28T09:30:00+00:00',
      occurred_at_utc: '2026-10-28T10:00:00+00:00',
      distance_km: 10, consumed_dc_kwh: 2.2, known_cost_ore: 374,
      cost_ore: 374, unpriced_dc_kwh: 0, kr_per_mil: 3.74,
      quality: 'estimated',
    },
    {
      event_id: 'trip-2', started_at_utc: '2026-10-28T11:00:00+00:00',
      occurred_at_utc: '2026-10-28T11:30:00+00:00',
      distance_km: 20, consumed_dc_kwh: 4.4, known_cost_ore: 748,
      cost_ore: 748, unpriced_dc_kwh: 0, kr_per_mil: 3.74,
      quality: 'estimated',
    },
  ],
  writes_to_devices: 0,
};

describe('private EV cost read-only API', () => {
  it('fails closed when not configured', async () => {
    delete process.env.EV_COST_READ_TOKEN;
    delete process.env.EV_COST_REPORT_PATH;
    expect((await summaryGET(request('summary'))).status).toBe(503);
    expect((await drivingGET(request('driving'))).status).toBe(503);
  });

  it('rejects missing and wrong Bearer tokens', async () => {
    process.env.EV_COST_READ_TOKEN = token;
    process.env.EV_COST_REPORT_PATH = '/nonexistent.json';
    expect((await summaryGET(request('summary', '', ''))).status).toBe(401);
    expect((await summaryGET(request('summary', '', 'wrong'))).status).toBe(401);
  });

  it('returns no data if snapshot is absent, malformed or schema changed', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'ev-cost-route-'));
    try {
      process.env.EV_COST_READ_TOKEN = token;
      process.env.EV_COST_REPORT_PATH = join(dir, 'report.json');
      expect((await summaryGET(request('summary'))).status).toBe(503);
      await writeFile(process.env.EV_COST_REPORT_PATH, 'not-json');
      expect((await summaryGET(request('summary'))).status).toBe(503);
      await writeFile(process.env.EV_COST_REPORT_PATH,
        JSON.stringify({ ...snapshot, schema_version: 2 }));
      expect((await summaryGET(request('summary'))).status).toBe(503);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('reports stored price and distance-weighted driving cost without writes', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'ev-cost-route-'));
    try {
      process.env.EV_COST_READ_TOKEN = token;
      process.env.EV_COST_REPORT_PATH = join(dir, 'report.json');
      await writeFile(process.env.EV_COST_REPORT_PATH, JSON.stringify(snapshot));
      const result = await summaryGET(request('summary'));
      expect(result.status).toBe(200);
      expect(result.headers.get('cache-control')).toContain('no-store');
      const body = await result.json();
      expect(body.summary).toMatchObject({ kr_per_mil: 3.74, quality: 'estimated' });
      expect(body.inventory.average_ore_per_dc_kwh).toBe(170);
      expect(body.direct_aux_cost_ore).toBe(200);
      expect(body.writes_to_devices).toBe(0);
      const trip = await drivingGET(request('driving',
        '?from=2026-10-28T11:00:00Z&to=2026-10-29T00:00:00Z'));
      expect(trip.status).toBe(200);
      const filtered = await trip.json();
      expect(filtered.driving).toHaveLength(1);
      expect(filtered.summary).toMatchObject({
        distance_km: 20, consumed_dc_kwh: 4.4, cost_ore: 748, kr_per_mil: 3.74,
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('refuses ambiguous local time, duplicate params and invalid windows', async () => {
    process.env.EV_COST_READ_TOKEN = token;
    process.env.EV_COST_REPORT_PATH = '/nonexistent.json';
    for (const query of [
      '?from=2026-10-25T02:15:00',
      '?from=2026-10-28T12:00:00Z&to=2026-10-28T11:00:00Z',
      '?from=2026-10-28T11:00:00Z&from=2026-10-28T11:01:00Z',
      '?ignore=1',
    ]) {
      expect((await drivingGET(request('driving', query))).status).toBe(400);
    }
  });

  it('does not report unknown per-mile cost as zero when energy is unpriced', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'ev-cost-route-'));
    try {
      process.env.EV_COST_READ_TOKEN = token;
      process.env.EV_COST_REPORT_PATH = join(dir, 'report.json');
      const partial = structuredClone(snapshot);
      partial.driving[0].cost_ore = null as never;
      partial.driving[0].unpriced_dc_kwh = 1;
      partial.driving[0].kr_per_mil = null as never;
      partial.driving[0].quality = 'partial';
      await writeFile(process.env.EV_COST_REPORT_PATH, JSON.stringify(partial));
      const result = await drivingGET(request('driving'));
      expect(result.status).toBe(200);
      const body = await result.json();
      expect(body.summary).toMatchObject({
        quality: 'partial', cost_ore: null, kr_per_mil: null,
      });
      expect(body.summary.known_cost_ore).toBe(1122);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});
