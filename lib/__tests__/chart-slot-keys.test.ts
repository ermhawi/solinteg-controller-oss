import { describe, it, expect } from 'vitest';
import { chartSlotKeys, chartSlotsForDisplay } from '../chart-slot-keys';

const date = '2026-10-25';
const time = (hhmm: string) => `${date}T${hhmm}:00`;
const price = (hhmm: string) => ({ startTime: time(hhmm), price: 1 });
const plan = (hhmm: string) => ({ startTime: time(hhmm), socAfter: 1 });

const prices = ['01:45', '02:00', '02:15', '02:30', '02:45',
  '02:00', '02:15', '02:30', '02:45', '03:00'].map(price);

describe('chartSlotsForDisplay', () => {
  it('does not alter normal-day input or optimizer labels', () => {
    const ordinary = [price('04:00'), price('04:15')];
    const future = [plan('04:00'), plan('04:15')];
    const view = chartSlotsForDisplay(ordinary, [], future);
    expect(view.prices).toBe(ordinary);
    expect(view.future).toBe(future);
    expect(chartSlotKeys(ordinary)).toEqual(ordinary.map((p) => p.startTime));
  });

  it('keeps both 02:xx hours distinct and in elapsed-time order', () => {
    const view = chartSlotsForDisplay(prices, [], ['01:45', '02:00', '02:15', '02:30',
      '02:45', '02:00', '02:15', '02:30', '02:45', '03:00'].map(plan));
    expect(view.prices.map((p) => p.startTime)).toEqual([
      time('01:45'), time('02:00'), time('02:15'), time('02:30'), time('02:45'),
      `${time('02:00')}#2`, `${time('02:15')}#2`, `${time('02:30')}#2`,
      `${time('02:45')}#2`, time('03:00'),
    ]);
    expect(view.future.map((p) => p.startTime)).toEqual(view.prices.map((p) => p.startTime));
    expect(new Set(view.prices.map((p) => p.startTime)).size).toBe(prices.length);
    expect(prices[5].startTime).toBe(time('02:00')); // source unchanged
  });

  it('attaches a plan starting during the SECOND 02-hour to CET, not CEST', () => {
    const view = chartSlotsForDisplay(prices, [], ['02:15', '02:30', '02:45', '03:00'].map(plan));
    expect(view.future.map((p) => p.startTime)).toEqual([
      `${time('02:15')}#2`, `${time('02:30')}#2`,
      `${time('02:45')}#2`, time('03:00'),
    ]);
  });

  it('matches sparse past snapshots backwards before the forward plan', () => {
    const view = chartSlotsForDisplay(prices, [
      plan('02:00'), plan('02:30'), plan('02:00'), plan('02:30'),
    ], [plan('03:00')]);
    expect(view.past.map((p) => p.startTime)).toEqual([
      time('02:00'), time('02:30'), `${time('02:00')}#2`, `${time('02:30')}#2`,
    ]);
  });
});
