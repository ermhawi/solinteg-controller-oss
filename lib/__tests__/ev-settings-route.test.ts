import { afterEach, describe, expect, it } from 'vitest';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { PUT } from '../../app/api/ev/settings/route';

const original = { ...process.env };
afterEach(() => { process.env = { ...original }; });

function request(secret: string, data: unknown) {
  return new Request('http://localhost/api/ev/settings', {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
      Authorization: 'Bearer ' + secret,
    },
    body: JSON.stringify(data),
  });
}

const VALID = {
  charge_mode: 'economy',
  departure_local_time: '07:00',
  max_buy_ore_per_kwh: 86,
};

describe('EV shadow settings API authorization and fail-closed gates', () => {
  it('rejects any PUT by default, even with a valid-looking bearer', async () => {
    const secret = 'x'.repeat(48);
    process.env.EV_SHADOW_SETTINGS_WRITES_ENABLED = '0';
    process.env.EV_SHADOW_SETTINGS_WRITE_TOKEN = secret;
    const result = await PUT(request(secret, VALID));
    expect(result.status).toBe(403);
  });

  it('rejects a wrong or missing key after explicit opt-in', async () => {
    process.env.EV_SHADOW_SETTINGS_WRITES_ENABLED = '1';
    process.env.EV_SHADOW_SETTINGS_WRITE_TOKEN = 'x'.repeat(48);
    expect((await PUT(request('wrong-key', VALID))).status).toBe(403);
    const noAuth = new Request('http://localhost/api/ev/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(VALID),
    });
    expect((await PUT(noAuth)).status).toBe(403);
  });

  it('persists only valid shadow preferences with explicit credentials', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'ev-auth-route-'));
    try {
      process.env.EV_SHADOW_SETTINGS_WRITES_ENABLED = '1';
      process.env.EV_SHADOW_SETTINGS_WRITE_TOKEN = 'secret-value-for-a-manual-test-1234567890';
      process.env.EV_SETTINGS_PATH = join(dir, 'ev-settings.json');
      process.env.EV_STATUS_PATH = join(dir, 'no-status.json');
      process.env.EVCC_SHADOW_PATH = join(dir, 'no-shadow.json');
      expect((await PUT(request(process.env.EV_SHADOW_SETTINGS_WRITE_TOKEN,
        { ...VALID, charge_mode: 'unknown' }))).status).toBe(400);
      const response = await PUT(request(process.env.EV_SHADOW_SETTINGS_WRITE_TOKEN, VALID));
      expect(response.status).toBe(200);
      const returned = await response.json();
      expect(returned).toMatchObject({ mode: 'shadow', deviceWrites: 0 });
      const saved = JSON.parse(await readFile(process.env.EV_SETTINGS_PATH, 'utf8'));
      expect(saved).toEqual(VALID);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});
