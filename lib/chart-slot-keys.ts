/**
 * Distinguish the two 02:xx slots on an autumn DST day in the chart ONLY.
 * The optimizer/executor still consume their original Stockholm wall-clock labels.
 */
export function chartSlotKeys(slots: { startTime: string }[]): string[] {
  const seen = new Map<string, number>();
  return slots.map(({ startTime }) => {
    const occurrence = (seen.get(startTime) ?? 0) + 1;
    seen.set(startTime, occurrence);
    return occurrence === 1 ? startTime : `${startTime}#${occurrence}`;
  });
}

/** Match a contiguous forward plan and (possibly sparse) history to the price timeline. */
export function chartSlotsForDisplay<P extends { startTime: string }, D extends { startTime: string }>(
  prices: P[],
  past: D[],
  future: D[],
): { prices: P[]; past: D[]; future: D[] } {
  const keys = chartSlotKeys(prices);
  if (keys.every((key, i) => key === prices[i].startTime)) return { prices, past, future };

  const originalTimes = prices.map((p) => p.startTime);
  const taggedPrices = prices.map((p, i) => ({ ...p, startTime: keys[i] }));

  // A forward plan is contiguous. Both 02:00 occurrences may match its first slot;
  // prefer the one whose succeeding slots actually match, not the first label found.
  let futureStart = -1;
  let bestMatch = -1;
  for (let i = 0; i < prices.length; i++) {
    if (originalTimes[i] !== future[0]?.startTime) continue;
    let matched = 0;
    while (matched < future.length && originalTimes[i + matched] === future[matched].startTime) matched++;
    if (matched > bestMatch || (matched === bestMatch && i > futureStart)) {
      futureStart = i;
      bestMatch = matched;
    }
  }
  const taggedFuture = future.map((slot, i) => ({
    ...slot,
    startTime: originalTimes[futureStart + i] === slot.startTime ? keys[futureStart + i] : slot.startTime,
  }));

  // Historical snapshots can be sparse. Work backwards from the next plan's start,
  // so the second 02:xx is assigned to the second occurrence when both are present.
  const taggedPast = [...past];
  let before = futureStart < 0 ? prices.length : futureStart;
  for (let i = past.length - 1; i >= 0; i--) {
    let at = before - 1;
    while (at >= 0 && originalTimes[at] !== past[i].startTime) at--;
    if (at < 0) continue;
    taggedPast[i] = { ...past[i], startTime: keys[at] };
    before = at;
  }
  return { prices: taggedPrices, past: taggedPast, future: taggedFuture };
}
