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

  it('gates writes on explicit local opt-in, not an extra admin secret', () => {
    delete process.env.EV_SETTINGS_WRITES_ENABLED;
    delete process.env.EV_SHADOW_SETTINGS_WRITE_TOKEN;
    expect(evSettingsWriteEnabled()).toBe(false);
    process.env.EV_SETTINGS_WRITES_ENABLED = '1';
    expect(evSettingsWriteEnabled()).toBe(true);
    process.env.EV_SETTINGS_WRITES_ENABLED = '0';
    expect(evSettingsWriteEnabled()).toBe(false);
  });

  it('provides display-only fallback when no reader or EVCC is running', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'ev-ui-missing-'));
    try {
      process.env.EV_SETTINGS_PATH = join(dir, 'ev-settings.json');
      process.env.EV_STATUS_PATH = join(dir, 'status.json');
      process.env.EVCC_SHADOW_PATH = join(dir, 'shadow.json');
      process.env.EV_SHADOW_PLAN_PATH = join(dir, 'price-shadow.json');
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
      process.env.EV_SHADOW_PLAN_PATH = join(dir, 'price-shadow.json');
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
      const original = JSON.parse(await readFile(process.env.EV_SETTINGS_PATH, 'utf8'));
      expect(original).toMatchObject(value);
      expect(original.revision).toMatch(/^[0-9a-f-]{36}$/);
      await saveEvSettings({ ...value, charge_mode: 'deadline', revision: original.revision });
      const mode = JSON.parse(await readFile(process.env.EV_SETTINGS_PATH, 'utf8'));
      expect(mode.charge_mode).toBe('deadline');
      expect(mode.revision).not.toBe(original.revision);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('refuses stale browser revisions and preserves the newer settings', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'ev-ui-version-'));
    try {
      process.env.EV_SETTINGS_PATH = join(dir, 'settings.json');
      const first = await saveEvSettings({
        charge_mode: 'economy', departure_local_time: '07:00',
        max_buy_ore_per_kwh: null,
      });
      await expect(saveEvSettings({
        charge_mode: 'deadline', departure_local_time: '08:00',
        max_buy_ore_per_kwh: null,
      })).rejects.toThrow(/annan flik/);
      expect((await readEvOverview()).settings.revision).toBe(first.revision);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('rejects live mode changes while a verified price pause exists', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'ev-ui-pause-'));
    try {
      const now = new Date().toISOString();
      process.env.EV_LIVE_SETTINGS_ENABLED = '1';
      process.env.EV_SETTINGS_PATH = join(dir, 'settings.json');
      process.env.EV_PRICE_STATUS_PATH = join(dir, 'control.json');
      process.env.EVCC_READONLY_PATH = join(dir, 'evcc.json');
      await writeFile(process.env.EV_PRICE_STATUS_PATH, JSON.stringify({
        schema_version: 1, source: 'evcc_price_controller_status',
        timestamp: now, control_enabled: true, state: 'no_change',
        reason: '', writes: 0, owner_state: 'owned',
      }));
      await writeFile(process.env.EVCC_READONLY_PATH, JSON.stringify({
        source: 'waveshare_evcc_fc03_readonly', timestamp: now,
        read_ok: true, writes: 0, physical_control_eligible: false,
        state: 'B2', configured_pwm_raw: 1000, pilot_pwm_raw: 267,
        channel_currents_a: [0, 0.1, 0],
      }));
      const candidate = { charge_mode: 'economy' as const,
        departure_local_time: '07:00', max_buy_ore_per_kwh: null };
      await expect(saveEvSettings(candidate)).rejects.toThrow(/pausstatus/);
      expect((await readEvOverview()).settings).toEqual(DEFAULT_EV_SETTINGS);
      await writeFile(process.env.EV_PRICE_STATUS_PATH, JSON.stringify({
        schema_version: 1, source: 'evcc_price_controller_status',
        timestamp: now, control_enabled: true, state: 'no_change',
        reason: '', writes: 0, owner_state: 'released',
      }));
      await expect(saveEvSettings(candidate)).rejects.toThrow(/pausad/);
      await writeFile(process.env.EVCC_READONLY_PATH, JSON.stringify({
        source: 'waveshare_evcc_fc03_readonly', timestamp: now,
        read_ok: true, writes: 0, physical_control_eligible: false,
        state: 'C2', configured_pwm_raw: 100, pilot_pwm_raw: 100,
        channel_currents_a: [0, 6, 0],
      }));
      const saved = await saveEvSettings(candidate);
      expect(saved.charge_mode).toBe('economy');
      expect(saved.revision).toMatch(/^[a-f0-9-]{36}$/);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('prefers HA price shadow while keeping independently observed API and last-change times', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'ev-ui-scenario-'));
    try {
      process.env.EV_SETTINGS_PATH = join(dir, 'settings.json');
      process.env.EV_STATUS_PATH = join(dir, 'status.json');
      process.env.EVCC_SHADOW_PATH = join(dir, 'evcc.json');
      process.env.EV_SHADOW_PLAN_PATH = join(dir, 'price-shadow.json');
      const checked = new Date().toISOString();
      const changed = new Date(Date.now() - 2 * 3600 * 1000).toISOString();
      await writeFile(process.env.EV_STATUS_PATH, JSON.stringify({
        source: 'home_assistant', checked_at_utc: checked,
        valid_for_planning: false, shadow_observation_usable: true,
        shadow_estimate_ready: true, observation_quality: 'api_recent_field_change_unverified',
        battery_soc_pct: 63, battery_reported_at_utc: changed,
        energy_api_received_at_utc: checked,
        connected: true, remaining_soc_pct: 17,
        remaining_ac_kwh: 14.733,
        assumed_usable_capacity_kwh: 78,
        assumed_ac_efficiency: 0.9,
        energy_estimate_unverified: true,
      }));
      await writeFile(process.env.EVCC_SHADOW_PATH, JSON.stringify({
        mode: 'shadow', timestamp: checked, source: 'evcc_poll',
        charging_suggested_a: 0, charging_permitted_a: 0,
      }));
      await writeFile(process.env.EV_SHADOW_PLAN_PATH, JSON.stringify({
        mode: 'shadow', timestamp: checked, source: 'ha_price_scenario_no_evcc',
        charge_mode: 'economy', charging_suggested_a: 6,
        charging_permitted_a: 6, // Tampered: UI may NEVER echo this.
        price_buy_ore_kwh: 25, energy_estimate_unverified: true,
        observation_quality: 'api_recent_field_change_unverified',
      }));
      const overview = await readEvOverview();
      expect(overview.statusFresh).toBe(true);
      expect(overview.status?.shadow_observation_usable).toBe(true);
      expect(overview.status?.energy_api_received_at_utc).toBe(checked);
      expect(overview.status?.battery_reported_at_utc).toBe(changed);
      expect(overview.status?.remaining_soc_pct).toBe(17);
      expect(overview.status?.energy_estimate_unverified).toBe(true);
      expect(overview.shadow?.source).toBe('ha_price_scenario_no_evcc');
      expect(overview.shadow?.charging_suggested_a).toBe(6);
      expect(overview.shadow?.charging_permitted_a).toBe(0);
      expect(overview.shadow?.energy_estimate_unverified).toBe(true);
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
      process.env.EV_SHADOW_PLAN_PATH = join(dir, 'price-shadow.json');
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
