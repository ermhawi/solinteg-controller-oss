import { describe, it, expect } from 'vitest';
import { assessPublishableHorizon } from '../horizon-safety';

describe('short-horizon safety preflight (O04)', () => {
  it('preserves existing behavior when disabled by default', () => {
    expect(assessPublishableHorizon(8, undefined)).toMatchObject({
      publishable: true, enabled: false, reason: 'disabled',
    });
    expect(assessPublishableHorizon(8, '0').publishable).toBe(true);
  });

  it('rejects two-hour forecast for explicitly requested 12 hours', () => {
    expect(assessPublishableHorizon(8, '12')).toMatchObject({
      publishable: false, enabled: true, reason: 'short',
      requiredHours: 12, availableHours: 2,
    });
    expect(assessPublishableHorizon(48, '12').publishable).toBe(true);
  });

  it('allows legitimate 23h and 25h DST days under a 12h policy', () => {
    expect(assessPublishableHorizon(92, '12').publishable).toBe(true);
    expect(assessPublishableHorizon(100, '12').publishable).toBe(true);
  });

  it('invalid explicit configuration fails closed, not silently disabled', () => {
    for (const value of ['-1', 'NaN', 'Infinity', '73', '1e3', 'true', ' 12', '']) {
      const result = assessPublishableHorizon(96, value);
      if (value === '') {
        expect(result.reason).toBe('disabled');
      } else {
        expect(result).toMatchObject({
          publishable: false, reason: 'invalid-configuration',
        });
      }
    }
  });

  it('invalid/unsafe slot counts are rejected even with guard disabled', () => {
    for (const n of [NaN, Infinity, -1, 1.5, Number.MAX_SAFE_INTEGER + 1]) {
      expect(assessPublishableHorizon(n, undefined).reason).toBe('invalid-horizon');
      expect(assessPublishableHorizon(n, '0').publishable).toBe(false);
    }
  });
});
