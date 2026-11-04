import type { EvSocScenario } from './ev-soc-state';
import { buildEvPriceWindows } from './ev-price-windows';
import type { EvPriceWindow } from './ev-price-windows';
import type { ChartPoint } from './chart-utils';

type PricePoint = Pick<ChartPoint, 'time' | 'buy' | 'priceSource'>;

export interface EvProjectedSlot {
  time: string;
  socPct: number; // estimated SoC at START of the slot
  charging: boolean; // hypothetical 6 A during this slot, not EVCC state
  forecast: boolean;
}

export interface EvSocProjection {
  mode: 'economy' | 'deadline';
  slots: EvProjectedSlot[];
  windows: EvPriceWindow[];
  targetPct: number;
  endSocPct: number;
  targetReached: boolean;
  deadlineCovered: boolean;
  departureKey: string | null;
  assumedUsableKwh: number;
  assumedEfficiency: number;
  estimatedKwh: number;
  reason: string;
}

const SLOT_KWH_6A = 230 * 6 / 4000;
const LOOKAHEAD_SLOTS = 48 * 4;

function nextDepartureKey(nowKey: string, clock: string): string {
  const currentDate = nowKey.slice(0, 10);
  const local = nowKey.slice(11, 16);
  const [y, m, d] = currentDate.split('-').map(Number);
  const date = local >= clock
    ? new Date(Date.UTC(y, m - 1, d + 1)).toISOString().slice(0, 10)
    : currentDate;
  return date + 'T' + clock + ':00';
}

/**
 * An explicitly UNVERIFIED SoC scenario. The real EV controller must never use
 * these assumed battery specifications or the chart's cheap-price candidates.
 * It follows the current UI intent: cheapest 6 A slots to next departure in
 * deadline mode, or economical windows without deadline in economy mode.
 */
export function buildEvSocProjection(
  prices: PricePoint[],
  currentSlot: string | null,
  session: EvSocScenario | null,
  riskOre: number,
  nowEpochMs: number | null,
): EvSocProjection | null {
  if (!session || !currentSlot || nowEpochMs === null ||
      !Number.isFinite(riskOre) || riskOre < 0 || riskOre > 100 ||
      nowEpochMs - Date.parse(session.receivedAtUtc) > 10 * 60_000 ||
      Date.parse(session.receivedAtUtc) > nowEpochMs + 5000) return null;
  const index = prices.findIndex((p) => p.time === currentSlot);
  if (index < 0) return null;
  const horizon = prices.slice(index, index + LOOKAHEAD_SLOTS);
  if (!horizon.length || horizon[0].priceSource !== 'actual' ||
      horizon.some((p) => !Number.isFinite(p.buy) || p.buy < -500 ||
        p.buy > 3000 || (p.priceSource !== 'actual' && p.priceSource !== 'forecast'))) return null;

  const stepPct = SLOT_KWH_6A * session.assumedEfficiency / session.assumedUsableKwh * 100;
  if (!(stepPct > 0) || session.socPct >= session.targetPct) return null;
  let chosen = new Set<string>();
  let candidate: PricePoint[] = [];
  let departureKey: string | null = null;
  let deadlineCovered = true;

  if (session.chargeMode === 'deadline') {
    departureKey = nextDepartureKey(currentSlot, session.departureLocalTime);
    // A 02:xx departure on the October fold has two possible instants:
    // refuse to predict a false punctual result.
    if (departureKey.slice(11, 13) === '02' &&
        prices.some((p) => p.time === departureKey + '#2')) return null;
    candidate = horizon.filter((p) => p.time.slice(0, 19) < departureKey!);
    if (!candidate.length) return null;
    deadlineCovered = horizon.some((p) => p.time.slice(0, 19) >= departureKey!);
    const slotsNeeded = Math.ceil((session.targetPct - session.socPct) / stepPct);
    // Choose least expensive full BUY prices, not just spot, with the same
    // forecast risk premium as the house. Timely completion can cost more.
    const cheapest = [...candidate].sort((a, b) =>
      a.buy + (a.priceSource === 'forecast' ? riskOre : 0) -
      (b.buy + (b.priceSource === 'forecast' ? riskOre : 0)) ||
      prices.indexOf(a) - prices.indexOf(b));
    chosen = new Set(cheapest.slice(0, slotsNeeded).map((p) => p.time));
  } else {
    const windows = buildEvPriceWindows(prices, currentSlot, riskOre, session.maxBuyOre);
    if (windows.status !== 'preview') return null;
    chosen = new Set(windows.slots.filter((p) => p.charging).map((p) => p.time));
    candidate = horizon;
  }

  let socPct = session.socPct;
  let estimatedKwh = 0;
  const slots: EvProjectedSlot[] = [];
  const windows: EvPriceWindow[] = [];
  for (const p of candidate) {
    const charging = chosen.has(p.time) && socPct < session.targetPct;
    slots.push({
      time: p.time, socPct: Math.round(socPct * 10) / 10,
      charging, forecast: p.priceSource === 'forecast',
    });
    windows.push({
      time: p.time,
      priceOre: p.buy + (p.priceSource === 'forecast' ? riskOre : 0),
      charging, forecast: p.priceSource === 'forecast',
    });
    if (charging) {
      const increment = Math.min(stepPct, session.targetPct - socPct);
      estimatedKwh += increment / 100 * session.assumedUsableKwh / session.assumedEfficiency;
      socPct += increment;
    }
  }
  // Show the final slot's boundary even if it lies beyond the selected
  // display price horizon (the chart simply doesn't draw unavailable keys).
  const nextSlot = prices[index + candidate.length];
  if (nextSlot && slots.length) {
    slots.push({
      time: nextSlot.time, socPct: Math.round(socPct * 10) / 10,
      charging: false, forecast: nextSlot.priceSource === 'forecast',
    });
  }
  return {
    mode: session.chargeMode,
    slots, windows,
    targetPct: session.targetPct,
    endSocPct: Math.round(socPct * 10) / 10,
    targetReached: socPct >= session.targetPct - 1e-6,
    deadlineCovered, departureKey,
    assumedUsableKwh: session.assumedUsableKwh,
    assumedEfficiency: session.assumedEfficiency,
    estimatedKwh: Math.round(estimatedKwh * 100) / 100,
    reason: 'Illustrativ 6 A-plan baserad på Volvo API och antagen kapacitet/verkningsgrad. Ingen aktiv EVCC-order.',
  };
}
