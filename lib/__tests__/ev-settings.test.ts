import { afterEach, describe, expect, it, vi } from 'vitest';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DEFAULT_EV_SETTINGS, parseEvSettings } from '../ev-settings';
import { evSettingsWriteEnabled, readEvOverview, saveEvSettings } from '../ev-state';

const original = { ...process.env };
afterEach(() => {
  process.env = { ...original };
  vi.restoreAllMocks();
});

describe('shared EV mode configuration', () => {
  it('parses both intentions and an optional genuine price ceiling', () => {
    expect(parseEvSettings({ charge_mode: 'deadline',
      departure_local_time: '07:00', max_buy_ore_per_kwh: null }))
      .toEqual(DEFAULT_EV_SETTINGS);
    expect(parseEvSettings({ charge_mode: 'economy',
      departure_local_time: '10:30', max_buy_ore_per_kwh: 112.75 }))
      .toEqual({
        charge_mode: 'economy',
        departure_local_time: '10:30',
        max_buy_ore_per_kwh: 112.75,
      });
  });

  it('rejects an unsafe mode, invalid time, unknown keys or numeric abuse', () => {
    const malformed = [
      null, [], 'economy',
      { charge_mode: 'fast' },
      { charge_mode: 'economy', target_soc_pct: 90 },
      { departure_local_time: '24:10' },
      { departure_local_time: '10:60' },
      { max_buy_ore_per_kwh: Infinity },
      { max_buy_ore_per_kwh: NaN },
      { max_buy_ore_per_kwh: '50' },
      { max_buy_ore_per_kwh: true },
      { max_buy_ore_per_kwh: -201 },
      { max_buy_ore_per_kwh: 3001 },
    ];
    for (const value of malformed) {
      expect(parseEvSettings(value)).toBeNull();
    }
  });

  it('never enables writes with a missing or short secret', () => {
    process.env.EV_SHADOW_SETTINGS_WRITES_ENABLED = '1';
    delete process.env.EV_SHADOW_SETTINGS_WRITE_TOKEN;
    expect(evSettingsWriteEnabled()).toBe(false);
    process.env.EV_SHADOW_SETTINGS_WRITE_TOKEN = 'short';
    expect(evSettingsWriteEnabled()).toBe(false);
    process.env.EV_SHADOW_SETTINGS_WRITE_TOKEN = 'x'.repeat(32);
    expect(evSettingsWriteEnabled()).toBe(true);
    process.env.EV_SHADOW_SETTINGS_WRITES_ENABLED = '0';
    expect(evSettingsWriteEnabled()).toBe(false);
  });

  it('provides display-only fallback when no reader or EVCC is running', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'ev-ui-missing-'));
    try {
      process.env.EV_SETTINGS_PATH = join(dir, 'ev-settings.json');
      process.env.EV_STATUS_PATH = join(dir, 'status.json');
      process.env.EVCC_SHADOW_PATH = join(dir, 'shadow.json');
      delete process.env.EV_SHADOW_SETTINGS_WRITES_ENABLED;
      const overview = await readEvOverview();
      expect(overview.settings).toEqual(DEFAULT_EV_SETTINGS);
      expect(overview.status).toBeNull();
      expect(overview.shadow).toBeNull();
      expect(overview.statusFresh).toBe(false);
      expect(overview.shadowFresh).toBe(false);
      expect(overview.settingsWriteEnabled).toBe(false);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('does not reinterpret malformed settings as an acceptable saved mode', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'ev-ui-bad-'));
    try {
      process.env.EV_SETTINGS_PATH = join(dir, 'ev-settings.json');
      process.env.EV_STATUS_PATH = join(dir, 'status.json');
      process.env.EVCC_SHADOW_PATH = join(dir, 'shadow.json');
      await writeFile(process.env.EV_SETTINGS_PATH, JSON.stringify({ charge_mode: 'fast' }));
      const overview = await readEvOverview();
      expect(overview.settingsError).toMatch(/ogiltig/i);
      expect(overview.settings).toEqual(DEFAULT_EV_SETTINGS);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('writes only an atomically replaced local settings JSON; never a device action', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'ev-ui-save-'));
    try {
      process.env.EV_SETTINGS_PATH = join(dir, 'ev-settings.json');
      const value = { charge_mode: 'economy' as const,
        departure_local_time: '07:00', max_buy_ore_per_kwh: 89 };
      await saveEvSettings(value);
      expect(JSON.parse(await readFile(process.env.EV_SETTINGS_PATH, 'utf8')))
        .toEqual(value);
      await saveEvSettings({ ...value, charge_mode: 'deadline' });
      const mode = JSON.parse(await readFile(process.env.EV_SETTINGS_PATH, 'utf8'));
      expect(mode.charge_mode).toBe('deadline');
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('reports stale or valid shadow data but never presents it as physical control', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'ev-ui-status-'));
    try {
      process.env.EV_SETTINGS_PATH = join(dir, 'ev-settings.json');
      process.env.EV_STATUS_PATH = join(dir, 'status.json');
      process.env.EVCC_SHADOW_PATH = join(dir, 'shadow.json');
      const now = new Date().toISOString();
      await writeFile(process.env.EV_STATUS_PATH, JSON.stringify({
        source: 'home_assistant', checked_at_utc: now,
        valid_for_planning: false, reason: 'stale sensor',
        timestamp_origin: 'volvo_energy_state_field',
        battery_reported_at_utc: now,
      }));
      await writeFile(process.env.EVCC_SHADOW_PATH, JSON.stringify({
        mode: 'shadow', timestamp: now, charge_mode: 'economy',
        charging_suggested_a: 6, charging_permitted_a: 0,
        grid_topup_suggested_a: 6, writes: 0,
      }));
      const result = await readEvOverview();
      expect(result.statusFresh).toBe(true);
      expect(result.status?.valid_for_planning).toBe(false);
      expect(result.status?.timestamp_origin).toBe('volvo_energy_state_field');
      expect(result.shadowFresh).toBe(true);
      expect(result.shadow?.charging_permitted_a).toBe(0);
      expect(result.shadow?.writes).toBe(0);
      await writeFile(process.env.EV_STATUS_PATH, JSON.stringify({
        source: 'home_assistant',
        checked_at_utc: '2026-01-01T00:00:00Z',
        valid_for_planning: true,
      }));
      expect((await readEvOverview()).statusFresh).toBe(false);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});
