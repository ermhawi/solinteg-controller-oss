import { describe, expect, it } from 'vitest';
import { parseEvSocHistory, parseEvSocScenario } from './ev-soc-state';

const NOW = Date.parse('2026-11-04T14:00:00Z');
const status = {
  source: 'home_assistant',
  shadow_observation_usable: true,
  physical_control_eligible: false,
  energy_estimate_unverified: true,
  checked_at_utc: '2026-11-04T13:59:20Z',
  energy_api_received_at_utc: '2026-11-04T13:59:00Z',
  battery_reported_at_utc: '2026-11-04T13:49:00Z',
  connected: true,
  battery_soc_pct: 54,
  target_soc_pct: 80,
  assumed_usable_capacity_kwh: 81.608,
  assumed_ac_efficiency: .9,
  charge_mode: 'deadline',
  departure_local_time: '07:00',
  max_buy_ore_per_kwh: null,
};

describe('Volvo SoC source validation', () => {
  it('accepts only explicitly unverified, recent API snapshot for a scenario', () => {
    const parsed = parseEvSocScenario(status, NOW);
    expect(parsed).toMatchObject({
      socPct: 54, targetPct: 80,
      chargeMode: 'deadline',
      assumedUsableKwh: 81.608,
      assumedEfficiency: .9,
    });
    expect(parseEvSocScenario(status, NOW + 7 * 60_000)).toBeNull();
  });

  it('rejects missing assumptions, disconnected or fabricated physical verification', () => {
    for (const patch of [
      { assumed_usable_capacity_kwh: null },
      { assumed_ac_efficiency: .5 },
      { shadow_observation_usable: false },
      { physical_control_eligible: true },
      { energy_estimate_unverified: false },
      { connected: false }, { battery_soc_pct: 105 },
      { target_soc_pct: 90 }, { charge_mode: 'other' },
    ]) {
      expect(parseEvSocScenario({ ...status, ...patch }, NOW)).toBeNull();
    }
  });

  it('retains only unique, in-range 15min readings and their two timestamps', () => {
    const good = {
      time: '2026-11-04T14:15:00',
      socPct: 54,
      capturedAtUtc: '2026-11-04T13:15:50Z',
      reportedAtUtc: '2026-11-04T13:12:00Z',
    };
    const raw = {
      version: 1, source: 'volvo_energy_api_observation',
      quality: 'api_reported_not_verified_bms_sample',
      points: [good, good, { ...good, time: 'invalid' },
        { ...good, time: '2026-11-04T14:30:00', socPct: 110 },
        { ...good, time: '2026-11-04T14:45:00', reportedAtUtc: '2026-11-04T13:55:00Z' },
      ],
    };
    expect(parseEvSocHistory(raw)).toEqual([good]);
    expect(parseEvSocHistory({ ...raw, source: 'fake' })).toEqual([]);
  });

  it('supports the second Stockholm DST slot without conflating its key', () => {
    const base = {
      socPct: 50, capturedAtUtc: '2026-10-25T00:15:00Z',
      reportedAtUtc: '2026-10-25T00:14:00Z',
    };
    const result = parseEvSocHistory({
      version: 1, source: 'volvo_energy_api_observation',
      quality: 'api_reported_not_verified_bms_sample',
      points: [{ ...base, time: '2026-10-25T02:15:00' },
        { ...base, time: '2026-10-25T02:15:00#2' }],
    });
    expect(result.map((p) => p.time)).toEqual([
      '2026-10-25T02:15:00', '2026-10-25T02:15:00#2',
    ]);
  });
});
