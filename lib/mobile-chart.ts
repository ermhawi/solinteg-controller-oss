/** A readable 12-hour mobile window, beginning two hours before the current slot. */
export function mobileChartWindow(nowIndex: number, count: number, slots = 48, history = 8) {
  const end = Math.max(0, count - 1);
  if (count <= slots) return { start: 0, end };
  const start = Math.max(0, Math.min(end - slots + 1, nowIndex - history));
  return { start, end: start + slots - 1 };
}
