import { describe, it, expect } from 'vitest';
import { trimIncompleteTerminalDays } from '../horizon';
import { slotsInStockholmDay } from '../prices';

/** Naive Stockholm local labels for `count` contiguous slots starting at `startSlot` of `date`. */
function day(date: string, count: number, startSlot = 0) {
  return Array.from({ length: count }, (_, i) => {
    const slot = startSlot + i;
    const hh = String(Math.floor(slot / 4)).padStart(2, '0');
    const mm = String((slot % 4) * 15).padStart(2, '0');
    return { startTime: `${date}T${hh}:${mm}:00` };
  });
}

describe('slotsInStockholmDay', () => {
  it('is 96 on an ordinary day', () => {
    expect(slotsInStockholmDay('2026-09-02')).toBe(96);
    expect(slotsInStockholmDay('2026-01-15')).toBe(96);
  });

  // The price feed really delivers 92/100 slots on these two days (see lib/prices.ts's
  // currentSlotIndexInPrices), so a fixed 96 is wrong twice a year, not merely imprecise.
  it('is 92 on the spring-forward day (23 h)', () => {
    expect(slotsInStockholmDay('2026-03-29')).toBe(92);
  });

  it('is 100 on the fall-back day (25 h)', () => {
    expect(slotsInStockholmDay('2026-10-25')).toBe(100);
  });

  it('handles the month and year boundary', () => {
    expect(slotsInStockholmDay('2026-01-31')).toBe(96);
    expect(slotsInStockholmDay('2026-12-31')).toBe(96);
  });
});

describe('trimIncompleteTerminalDays', () => {
  it('keeps today and complete future days but drops a partial terminal day', () => {
    const slots = [
      ...day('2026-09-01', 2, 90), // today, already partly elapsed
      ...day('2026-09-02', 96),
      ...day('2026-09-03', 4), // rolling-horizon stub
    ];
    expect(trimIncompleteTerminalDays(slots, '2026-09-01')).toHaveLength(98);
  });

  it('keeps a full spring-forward day, which is 92 slots and not partial', () => {
    // The old `length !== 96` test dropped this day and everything after it, collapsing the
    // horizon on exactly the day the DP would otherwise plan across. See OPTIMIZER-AUDIT.md P0-3.
    const slots = [...day('2026-03-28', 96), ...day('2026-03-29', 92)];
    expect(trimIncompleteTerminalDays(slots, '2026-03-28')).toHaveLength(188);
  });

  it('keeps a full fall-back day, which is 100 slots', () => {
    // The repeated 02:xx hour makes two slots share one naive label; the contiguity check has to
    // tolerate that single step backwards without accepting a genuine hole.
    const fallBack = [
      ...day('2026-10-25', 12), // 00:00-02:45
      ...day('2026-10-25', 4, 8), // the repeated 02:00-02:45
      ...day('2026-10-25', 84, 12), // 03:00 onwards
    ];
    expect(fallBack).toHaveLength(100);
    const slots = [...day('2026-10-24', 96), ...fallBack];
    expect(trimIncompleteTerminalDays(slots, '2026-10-24')).toHaveLength(196);
  });

  it('rejects a future day with a hole even when the count looks right', () => {
    // dispatch_loop.py indexes the published plan by elapsed time from its first slot, so a gap in
    // the middle shifts the executor's idea of "now" for everything after it. Better to shorten
    // the horizon than to publish a plan the executor will read off by one hour.
    const holed = [...day('2026-09-02', 40), ...day('2026-09-02', 56, 41)]; // 96 slots, one gap
    expect(holed).toHaveLength(96);
    const slots = [...day('2026-09-01', 96), ...holed];
    expect(trimIncompleteTerminalDays(slots, '2026-09-01')).toHaveLength(96);
  });

  it('stops at the first incomplete day rather than skipping over it', () => {
    const slots = [...day('2026-09-01', 96), ...day('2026-09-02', 80), ...day('2026-09-03', 96)];
    expect(trimIncompleteTerminalDays(slots, '2026-09-01')).toHaveLength(96);
  });

  it('drops days before today', () => {
    const slots = [...day('2026-08-31', 96), ...day('2026-09-01', 96)];
    expect(trimIncompleteTerminalDays(slots, '2026-09-01')).toHaveLength(96);
  });

  it('never drops today, however partial', () => {
    const slots = day('2026-09-01', 1, 95);
    expect(trimIncompleteTerminalDays(slots, '2026-09-01')).toHaveLength(1);
  });
});
