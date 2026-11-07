import { describe, it, expect } from 'vitest';
import { buildControlEvidence } from '../control-evidence';

const NOW = Date.parse('2026-11-07T12:00:00.000Z');
const fresh = '2026-11-07T11:59:00.000Z';

describe('control evidence layers', () => {
  it('does not infer physical safety or accepted FC06 from HTTP 200 or applied', () => {
    const evidence = buildControlEvidence({
      action: { timestamp: fresh, outcome: 'applied', armed: true },
      battery: { timestamp: fresh, batteryW: -1000 },
      nowMs: NOW,
    });
    expect(evidence.decision.state).toBe('observed');
    expect(evidence.registerAccepted.state).toBe('unknown');
    expect(evidence.process.state).toBe('unknown');
    expect(evidence.physicalEffect.state).toBe('unknown');
    expect(evidence.measuredBatteryW).toBe(-1000);
    expect(evidence.physicalControlVerified).toBe(false);
  });

  it('keeps a simulated disarmed applied event separate from a physical command', () => {
    const evidence = buildControlEvidence({
      action: { timestamp: fresh, outcome: 'applied', armed: false },
      nowMs: NOW,
    });
    expect(evidence.decision.detail).toContain('avväpnad');
    expect(evidence.registerAccepted.state).toBe('unknown');
  });

  it('marks error_revert_failed as a decision failure even if telemetry is fresh', () => {
    const evidence = buildControlEvidence({
      action: { timestamp: fresh, outcome: 'error_revert_failed', armed: true },
      battery: { timestamp: fresh, batteryW: 500 },
      nowMs: NOW,
    });
    expect(evidence.decision.state).toBe('failed');
    expect(evidence.physicalEffect.state).toBe('unknown');
  });

  it('distinguishes an observed register readback from accepted physical effect', () => {
    const evidence = buildControlEvidence({
      readback: { timestamp: fresh, readbackOk: true },
      battery: { timestamp: fresh, batteryW: 0 },
      nowMs: NOW,
    });
    expect(evidence.readback.state).toBe('observed');
    expect(evidence.registerAccepted.state).toBe('unknown');
    expect(evidence.physicalEffect.state).toBe('unknown');
  });

  it('reports failed, stale, missing and future readbacks independently', () => {
    const failed = buildControlEvidence({
      readback: { timestamp: fresh, readbackOk: false }, nowMs: NOW,
    });
    expect(failed.readback.state).toBe('failed');
    const stale = buildControlEvidence({
      readback: { timestamp: '2026-11-07T11:40:00Z', readbackOk: true },
      action: { timestamp: '2026-11-07T11:40:00Z', outcome: 'applied', armed: true },
      nowMs: NOW,
    });
    expect(stale.readback.state).toBe('stale');
    expect(stale.decision.state).toBe('stale');
    const future = buildControlEvidence({
      readback: { timestamp: '2026-11-07T12:00:05Z', readbackOk: true },
      battery: { timestamp: '2026-11-07T12:00:05Z', batteryW: 100 },
      nowMs: NOW,
    });
    expect(future.readback.state).toBe('stale');
    expect(future.measuredBatteryW).toBeNull();
    expect(buildControlEvidence({ nowMs: NOW }).readback.state).toBe('unknown');
  });

  it('never gives a physical verified result even when all available inputs look good', () => {
    const result = buildControlEvidence({
      action: { timestamp: fresh, outcome: 'applied', armed: true },
      readback: { timestamp: fresh, readbackOk: true },
      battery: { timestamp: fresh, batteryW: -500 },
      nowMs: NOW,
    });
    expect(result.physicalControlVerified).toBe(false);
    expect(result.process.state).toBe('unknown');
    expect(result.physicalEffect.state).toBe('unknown');
  });
});
