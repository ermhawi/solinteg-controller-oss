import type { FlowReading } from './actual-flows';

const DEFAULT_HORIZON_SLOTS = 24;
const MIN_FORECAST_KWH = 0.05;
const MIN_SAMPLES_PER_SLOT = 2;
const MIN_RATIO_SAMPLES = 2;
const MIN_RATIO = 0.05;
const MAX_RATIO = 1.5;

export interface SolarNowcastResult {
  forecast: Record<string, number[]>;
  applied: boolean;
  ratio: number | null;
  sampleCount: number;
}

function localParts(date: Date): { date: string; slot: number } {
  const parts = new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Europe/Stockholm', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).formatToParts(date);
  const get = (type: string) => parts.find((part) => part.type === type)?.value ?? '0';
  const hour = Number(get('hour')) % 24;
  return { date: `${get('year')}-${get('month')}-${get('day')}`, slot: hour * 4 + Math.floor(Number(get('minute')) / 15) };
}

function median(values: number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

/** Adjust only the next six hours from measured PV; never changes inverter commands. */
export function applyIntradaySolarNowcast(
  forecast: Record<string, number[]> | null | undefined,
  readings: FlowReading[],
  now: Date = new Date(),
  horizonSlots = DEFAULT_HORIZON_SLOTS,
): SolarNowcastResult {
  if (!forecast) return { forecast: {}, applied: false, ratio: null, sampleCount: 0 };
  const { date, slot: currentSlot } = localParts(now);
  const today = forecast[date];
  if (!today?.length) return { forecast, applied: false, ratio: null, sampleCount: 0 };

  const buckets = new Map<number, number[]>();
  for (const reading of readings) {
    if (reading.pv_w == null || !Number.isFinite(reading.pv_w) || reading.pv_w < 0) continue;
    const parts = localParts(new Date(reading.timestamp));
    if (parts.date !== date || parts.slot >= currentSlot) continue;
    const values = buckets.get(parts.slot) ?? [];
    values.push(reading.pv_w);
    buckets.set(parts.slot, values);
  }

  const ratios: number[] = [];
  for (const [slot, values] of buckets) {
    const expected = today[slot];
    if (values.length < MIN_SAMPLES_PER_SLOT || expected == null || expected < MIN_FORECAST_KWH) continue;
    const averageW = values.reduce((sum, value) => sum + value, 0) / values.length;
    ratios.push((averageW * 0.25) / 1000 / expected);
  }
  if (ratios.length < MIN_RATIO_SAMPLES) return { forecast, applied: false, ratio: null, sampleCount: ratios.length };

  const ratio = Math.min(MAX_RATIO, Math.max(MIN_RATIO, median(ratios)));
  const adjusted = { ...forecast, [date]: [...today] };
  for (let slot = currentSlot; slot < Math.min(today.length, currentSlot + horizonSlots); slot++) {
    adjusted[date][slot] = Math.round(adjusted[date][slot] * ratio * 100) / 100;
  }
  return { forecast: adjusted, applied: true, ratio, sampleCount: ratios.length };
}