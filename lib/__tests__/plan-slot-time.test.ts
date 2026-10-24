import { describe, it, expect } from 'vitest';
import { planSlotEpochSeconds } from '../plan-slot-time';

describe('planSlotEpochSeconds', () => {
  it('projects ordinary summer slots with the Stockholm UTC+2 offset', () => {
    expect(planSlotEpochSeconds(
      ['2026-06-28T10:00:00', '2026-06-28T10:15:00'],
      '2026-06-28T08:03:00Z',
    )).toEqual([
      Date.parse('2026-06-28T08:00:00Z') / 1000,
      Date.parse('2026-06-28T08:15:00Z') / 1000,
    ]);
  });

  it('returns distinct monotonic instants for BOTH repeated 02:00 hours', () => {
    const date = '2026-10-25T';
    const times = ['01:45', '02:00', '02:15', '02:30', '02:45',
      '02:00', '02:15', '02:30', '02:45', '03:00'].map((t) => `${date}${t}:00`);
    const secs = planSlotEpochSeconds(times, '2026-10-24T23:49:00Z');
    expect(secs[1]).toBe(Date.parse('2026-10-25T00:00:00Z') / 1000);
    expect(secs[5]).toBe(Date.parse('2026-10-25T01:00:00Z') / 1000);
    expect(secs[9]).toBe(Date.parse('2026-10-25T02:00:00Z') / 1000);
    expect(secs.slice(1).map((v, i) => v - secs[i])).toEqual(Array(9).fill(900));
  });

  it('selects the CET 02:00 fold when the plan starts during the second occurrence', () => {
    expect(planSlotEpochSeconds(
      ['2026-10-25T02:00:00', '2026-10-25T02:15:00', '2026-10-25T03:00:00'],
      '2026-10-25T01:02:00Z',
    ).slice(0, 2)).toEqual([
      Date.parse('2026-10-25T01:00:00Z') / 1000,
      Date.parse('2026-10-25T01:15:00Z') / 1000,
    ]);
  });

  it('crosses the missing spring 02:00 hour without creating a phantom hour', () => {
    expect(planSlotEpochSeconds(
      ['2026-03-29T01:45:00', '2026-03-29T03:00:00', '2026-03-29T03:15:00'],
      '2026-03-29T00:47:00Z',
    )).toEqual([
      Date.parse('2026-03-29T00:45:00Z') / 1000,
      Date.parse('2026-03-29T01:00:00Z') / 1000,
      Date.parse('2026-03-29T01:15:00Z') / 1000,
    ]);
  });
});
