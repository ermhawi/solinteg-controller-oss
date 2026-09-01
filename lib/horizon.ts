import type { OptimizerSlot } from './optimizer';

/**
 * Keeps the current day and complete future calendar days in an optimizer horizon.
 * A partial future day must not be the DP's terminal point: with zero terminal value the
 * optimizer would spend the battery at the artificial end of the available data.
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
    if (byDate.get(date)!.length !== 96) break;
    keptDates.add(date);
  }
  return slots.filter((slot) => keptDates.has(slot.startTime.slice(0, 10)));
}
