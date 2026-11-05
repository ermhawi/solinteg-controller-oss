import { afterEach, describe, expect, it } from 'vitest';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { PUT } from '../../app/api/ev/settings/route';

const original = { ...process.env };
afterEach(() => { process.env = { ...original }; });

function request(data: unknown, headers: Record<string, string> = {}) {
  return new Request('http://localhost/api/ev/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...headers },
    body: JSON.stringify(data),
  });
}

const VALID = {
  charge_mode: 'economy',
  departure_local_time: '07:00',
  max_buy_ore_per_kwh: 86,
};

describe('EV settings API (GitHub OAuth2 at public reverse proxy)', () => {
  it('rejects PUT by default, even from the internal network', async () => {
    process.env.EV_SETTINGS_WRITES_ENABLED = '0';
    const result = await PUT(request(VALID));
    expect(result.status).toBe(403);
  });

  it('allows an internal request without any extra admin key after explicit opt-in', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'ev-auth-route-'));
    try {
      process.env.EV_SETTINGS_WRITES_ENABLED = '1';
      delete process.env.EV_SHADOW_SETTINGS_WRITE_TOKEN;
      process.env.EV_SETTINGS_PATH = join(dir, 'ev-settings.json');
      process.env.EV_STATUS_PATH = join(dir, 'no-status.json');
      process.env.EVCC_SHADOW_PATH = join(dir, 'no-shadow.json');
      expect((await PUT(request({ ...VALID, charge_mode: 'unknown' }))).status)
        .toBe(400);
      const response = await PUT(request(VALID));
      expect(response.status).toBe(200);
      const returned = await response.json();
      expect(returned).toMatchObject({ mode: 'shadow', deviceWrites: 0 });
      const saved = JSON.parse(await readFile(process.env.EV_SETTINGS_PATH, 'utf8'));
      expect(saved).toMatchObject(VALID);
      expect(saved.revision).toMatch(/^[a-f0-9-]{36}$/);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('accepts same-origin browser requests via the reverse proxy', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'ev-origin-route-'));
    try {
      process.env.EV_SETTINGS_WRITES_ENABLED = '1';
      process.env.EV_SETTINGS_PATH = join(dir, 'settings.json');
      const response = await PUT(request(VALID, {
        host: 'internal-backend:3007',
        'x-forwarded-host': 'ermhawi.com',
        origin: 'https://ermhawi.com',
        'sec-fetch-site': 'same-origin',
      }));
      expect(response.status).toBe(200);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('rejects cross-site and mismatched browser origins', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'ev-csrf-route-'));
    try {
      process.env.EV_SETTINGS_WRITES_ENABLED = '1';
      process.env.EV_SETTINGS_PATH = join(dir, 'settings.json');
      const origins: Record<string, string>[] = [
        { host: 'ermhawi.com', origin: 'https://evil.example' },
        { host: 'ermhawi.com', origin: 'https://ermhawi.com',
          'sec-fetch-site': 'cross-site' },
        { host: 'ermhawi.com', origin: 'null' },
      ];
      for (const headers of origins) {
        expect((await PUT(request(VALID, headers))).status).toBe(403);
      }
      await expect(readFile(process.env.EV_SETTINGS_PATH, 'utf8'))
        .rejects.toMatchObject({ code: 'ENOENT' });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});
