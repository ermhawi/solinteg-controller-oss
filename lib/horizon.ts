import type { OptimizerSlot } from './optimizer';
import { slotsInStockholmDay } from './prices';

/**
 * Keeps the current day and complete future calendar days in an optimizer horizon.
 *
 * A partial future day must not be the DP's terminal point: with zero terminal value the optimizer
 * spends the battery down to the floor at the end of whatever horizon it is given (measured: every
 * scenario, every season — see OPTIMIZER-AUDIT.md P0-3), so an artificially short horizon is not a
 * slightly-worse plan, it is a plan that deliberately empties the battery tonight.
 *
 * Which makes the acceptance test load-bearing in the other direction too, and it used to be
 * `length !== 96`:
 *   - that rejected the two DST-transition days outright, truncating the horizon at them twice a
 *     year for no reason (they are complete days, they just aren't 96 slots long);
 *   - and it rejected a day missing a single forecast slot, dropping ~96 slots of horizon over one
 *     hole. Holes are now filled upstream instead (`fillPriceGaps` in lib/prices.ts), so by the
 *     time a day reaches here it is either genuinely complete or genuinely partial.
 *
 * Contiguity is checked as well as count, because dispatch_loop.py indexes the published plan by
 * ELAPSED TIME from its first slot (`slot_index_for_instant`): a gap in the middle would silently
 * shift the executor's idea of "now" for every slot after it. A day with the right count but a hole
 * in it is therefore rejected rather than trusted.
 */
export function trimIncompleteTerminalDays<T extends Pick<OptimizerSlot, 'startTime'>>(
  slots: T[],
  today: string,
): T[] {
  const byDate = new Map<string, T[]>();
  for (const slot of slots) {
    const date = slot.startTime.slice(0, 10);
    const day = byDate.get(date) ?? [];
    day.push(slot);
    byDate.set(date, day);
  }

  const dates = [...byDate.keys()].sort();
  const keptDates = new Set<string>();
  for (const date of dates) {
    if (date < today) continue;
    if (date === today) {
      keptDates.add(date);
      continue;
    }
    const day = byDate.get(date)!;
    if (day.length !== slotsInStockholmDay(date) || !isContiguous(day)) break;
    keptDates.add(date);
  }
  return slots.filter((slot) => keptDates.has(slot.startTime.slice(0, 10)));
}

/** True when every consecutive pair of naive local timestamps is exactly one slot apart.
 *  Compared as strings-to-minutes rather than Date objects: these are naive Stockholm labels, and
 *  on the fall-back day two different slots legitimately share one label — which shows up here as
 *  a zero-minute step, i.e. correctly not contiguous, so that day is only accepted when the feed
 *  really delivered its 100 distinct slots. */
function isContiguous(day: Pick<OptimizerSlot, 'startTime'>[]): boolean {
  for (let i = 1; i < day.length; i++) {
    if (minutesOfDay(day[i].startTime) - minutesOfDay(day[i - 1].startTime) !== 15) {
      const previous = day[i - 1].startTime.slice(11, 16);
      const current = day[i].startTime.slice(11, 16);
      // These are the only valid local-label discontinuities: the fall-back duplicate
      // 02:45 → 02:00 and spring-forward skip 01:45 → 03:00.
      if ((previous === '02:45' && current === '02:00') || (previous === '01:45' && current === '03:00')) continue;
      return false;
    }
  }
  return true;
}

function minutesOfDay(startTime: string): number {
  return parseInt(startTime.slice(11, 13), 10) * 60 + parseInt(startTime.slice(14, 16), 10);
}
