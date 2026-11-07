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
        'x-forwarded-proto': 'https',
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

  it('rejects UTF-8 payloads exceeding 2048 bytes even with short JS string length', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'ev-byte-limit-'));
    try {
      process.env.EV_SETTINGS_WRITES_ENABLED = '1';
      process.env.EV_SETTINGS_PATH = join(dir, 'settings.json');
      // Duplicate allowed key: JSON.parse retains the final, valid intention.
      // Before this regression, UTF-16 length <2048 bypassed the byte limit.
      const raw = '{"charge_mode":"' + 'å'.repeat(1040) +
        '","charge_mode":"economy","departure_local_time":"07:00","max_buy_ore_per_kwh":86}';
      expect(raw.length).toBeLessThan(2048);
      expect(new TextEncoder().encode(raw).byteLength).toBeGreaterThan(2048);
      const result = await PUT(new Request('http://localhost/api/ev/settings', {
        method: 'PUT', headers: { 'content-type': 'application/json' }, body: raw,
      }));
      expect(result.status).toBe(413);
      await expect(readFile(process.env.EV_SETTINGS_PATH, 'utf8'))
        .rejects.toMatchObject({ code: 'ENOENT' });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('requires same scheme as the HTTPS forwarded origin', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'ev-origin-scheme-'));
    try {
      process.env.EV_SETTINGS_WRITES_ENABLED = '1';
      process.env.EV_SETTINGS_PATH = join(dir, 'settings.json');
      for (const header of [
        { origin: 'http://ermhawi.com', 'x-forwarded-host': 'ermhawi.com',
          'x-forwarded-proto': 'https' },
        { origin: 'https://ermhawi.com', 'x-forwarded-host': 'ermhawi.com',
          'x-forwarded-proto': 'http' },
        { origin: 'https://ermhawi.com', 'x-forwarded-host': 'ermhawi.com',
          'x-forwarded-proto': 'https,http' },
      ]) {
        expect((await PUT(request(VALID, header))).status).toBe(403);
      }
      await expect(readFile(process.env.EV_SETTINGS_PATH, 'utf8'))
        .rejects.toMatchObject({ code: 'ENOENT' });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('rejects malformed JSON media type and negative content length', async () => {
    process.env.EV_SETTINGS_WRITES_ENABLED = '1';
    expect((await PUT(request(VALID, { 'content-type': 'application/jsonp' }))).status)
      .toBe(415);
    expect((await PUT(request(VALID, { 'content-length': '-3' }))).status)
      .toBe(413);
    expect((await PUT(request(VALID, { 'content-length': '2048.5' }))).status)
      .toBe(413);
  });

  it('bounds a chunked UTF-8 request independently of its declared size', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'ev-stream-limit-'));
    try {
      process.env.EV_SETTINGS_WRITES_ENABLED = '1';
      process.env.EV_SETTINGS_PATH = join(dir, 'settings.json');
      const bytes = new TextEncoder().encode(JSON.stringify({
        ...VALID, charge_mode: 'å'.repeat(1080), revision: undefined,
      }));
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(bytes.subarray(0, 50));
          controller.enqueue(bytes.subarray(50));
          controller.close();
        },
      });
      const response = await PUT(new Request('http://localhost/api/ev/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', 'Content-Length': '2' },
        body: stream,
        duplex: 'half',
      } as RequestInit & { duplex: 'half' }));
      expect(response.status).toBe(413);
      await expect(readFile(process.env.EV_SETTINGS_PATH, 'utf8'))
        .rejects.toMatchObject({ code: 'ENOENT' });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('accepts charset JSON and leaves physical commands to the price controller', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'ev-ui-route-'));
    try {
      process.env.EV_SETTINGS_WRITES_ENABLED = '1';
      process.env.EV_SETTINGS_PATH = join(dir, 'settings.json');
      const result = await PUT(new Request('http://localhost/api/ev/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json; charset=utf-8' },
        body: JSON.stringify(VALID),
      }));
      expect(result.status).toBe(200);
      expect((await result.json()).deviceWrites).toBe(0);
      const ui = await readFile(join(process.cwd(), 'app/components/EvChargingPlanner.tsx'), 'utf8');
      expect(ui).toContain("apiPath('/api/ev/settings')");
      expect(ui).toContain("credentials: 'same-origin'");
      expect(ui).toContain("method: 'PUT'");
      expect(ui).not.toMatch(/set_limit_raw|EvccAscii|write_register|FC10/);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});
