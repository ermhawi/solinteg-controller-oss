import { slotsInStockholmDay, stockholmMidnightUtc, stockholmParts } from './prices';

const SLOT_MS = 15 * 60_000;

function localSlotTime(epochMs: number): string {
  const p = stockholmParts(new Date(epochMs));
  return `${p.dateStr}T${String(p.hour).padStart(2, '0')}:${String(p.minute).padStart(2, '0')}:00`;
}

/**
 * Project an already-computed plan's local labels to distinct Unix instants.
 * Does not alter the dispatch or optimizer. The logged UTC time disambiguates
 * whether its first 02:xx slot was in CEST or CET.
 */
export function planSlotEpochSeconds(startTimes: string[], loggedAt: string): number[] {
  if (!startTimes.length) return [];
  const firstLabel = startTimes[0];
  const [year, month, day] = firstLabel.slice(0, 10).split('-').map(Number);
  const midnightMs = stockholmMidnightUtc(year, month - 1, day).getTime();
  const loggedMs = Date.parse(loggedAt);

  let firstMs = midnightMs;
  let bestDistance = Infinity;
  for (let i = 0; i < slotsInStockholmDay(firstLabel.slice(0, 10)); i++) {
    const candidate = midnightMs + i * SLOT_MS;
    if (localSlotTime(candidate) !== firstLabel) continue;
    const distance = Number.isFinite(loggedMs) ? Math.abs(loggedMs - candidate) : i * SLOT_MS;
    if (distance < bestDistance) {
      bestDistance = distance;
      firstMs = candidate;
    }
  }

  // Advance in real elapsed time, not naive local timestamps. This preserves
  // both folds of 02:00, the spring jump, and occasional missing plan slots.
  const epochs: number[] = [];
  let cursor = firstMs;
  for (const label of startTimes) {
    let searched = 0;
    while (localSlotTime(cursor) !== label && searched < 96 * 4) {
      cursor += SLOT_MS;
      searched++;
    }
    if (localSlotTime(cursor) !== label) throw new Error(`Unresolvable plan slot time: ${label}`);
    epochs.push(Math.floor(cursor / 1000));
    cursor += SLOT_MS;
  }
  return epochs;
}
