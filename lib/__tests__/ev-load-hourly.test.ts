import { describe, expect, it } from 'vitest';
import { validateEvHourlyLoad } from '../ev-load-hourly';

const NOW = Date.parse('2026-11-04T16:15:00Z');
const valid = {
  version: 1,
  source: 'waveshare_evcc_fc03_ict2',
  quality: 'full_hour_measured_current_nominal_230v',
  generatedAtUtc: '2026-11-04T16:12:00Z',
  hours: [{ utcHour: '2026-11-04T14:00:00Z', evAvgW: 1380, samples: 119 }],
};

describe('EVCC-derived hourly forecast corrections', () => {
  it('accepts only full-hour independently measured EVCC samples', () => {
    expect(validateEvHourlyLoad(valid, NOW)).toEqual(valid.hours);
    expect(validateEvHourlyLoad({ ...valid, hours: [{ ...valid.hours[0], samples: 80 }] }, NOW)).toEqual([]);
    expect(validateEvHourlyLoad({ ...valid, hours: [{ ...valid.hours[0], evAvgW: -1 }] }, NOW)).toEqual([]);
    expect(validateEvHourlyLoad({ ...valid, hours: [{ ...valid.hours[0], evAvgW: 4200 }] }, NOW)).toEqual([]);
  });

  it('rejects stale, incomplete, duplicated, in-progress and spoofed exports', () => {
    expect(validateEvHourlyLoad({ ...valid, generatedAtUtc: '2026-11-04T15:59:00Z' }, NOW)).toEqual([]);
    expect(validateEvHourlyLoad({ ...valid, source: 'unknown' }, NOW)).toEqual([]);
    expect(validateEvHourlyLoad({ ...valid, quality: 'estimated' }, NOW)).toEqual([]);
    expect(validateEvHourlyLoad({ ...valid, hours: [...valid.hours, ...valid.hours] }, NOW)).toEqual([]);
    expect(validateEvHourlyLoad({ ...valid, hours: [{ ...valid.hours[0], utcHour: '2026-11-04T16:00:00Z' }] }, NOW)).toEqual([]);
    expect(validateEvHourlyLoad({ ...valid, hours: [{ ...valid.hours[0], utcHour: '2026-10-01T12:00:00Z' }] }, NOW)).toEqual([]);
  });
});
