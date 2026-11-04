import type { ChartPoint } from './chart-utils';

// Economic EV windows are NOT physical charging commands. The EVCC writer
// requires an independently validated vehicle session/target before activation.
export interface EvPriceWindow {
  time: string;
  priceOre: number; // full buy price incl. tax/transfer and forecast risk
  charging: boolean;
  forecast: boolean;
}

export interface EvPriceWindowPlan {
  status: 'preview' | 'unavailable';
  reason: string;
  slots: EvPriceWindow[];
  thresholdOre: number | null;
  chargeSlots: number;
  estimatedKwhAt6A: number;
  hasForecast: boolean;
}

const LOOKAHEAD_SLOTS = 48 * 4;
const MIN_HORIZON_SLOTS = 6 * 4;
const CHEAP_QUANTILE = 0.25;
const EV_KWH_PER_SLOT_AT_6A = 230 * 6 / 4000;

function unavailable(reason: string): EvPriceWindowPlan {
  return {
    status: 'unavailable', reason, slots: [],
    thresholdOre: null, chargeSlots: 0, estimatedKwhAt6A: 0, hasForecast: false,
  };
}

/**
 * Identifies economical 15-minute windows from the SAME full buy prices as
 * the house optimizer, following ev_price_policy.py's economy threshold.
 *
 * Uses ordered chart keys (#2 identifies the second DST 02:xx slot), not local
 * Date parsing. Always preview-only: without a trusted EV SoC/energy need the
 * window duration must not be presented as a real charging schedule.
 */
export function buildEvPriceWindows(
  prices: Pick<ChartPoint, 'time' | 'buy' | 'priceSource'>[],
  currentSlot: string | null,
  forecastRiskOre: number,
  maxBuyOre: number | null = null,
): EvPriceWindowPlan {
  if (!currentSlot || !Number.isFinite(forecastRiskOre) ||
      forecastRiskOre < 0 || forecastRiskOre > 100 ||
      (maxBuyOre !== null && (!Number.isFinite(maxBuyOre) ||
        maxBuyOre < -200 || maxBuyOre > 3000))) {
    return unavailable('Laddfönster kan inte beräknas utan giltig tid och prisinställningar.');
  }
  const start = prices.findIndex((price) => price.time === currentSlot);
  if (start < 0) return unavailable('Aktuell priskvart saknas.');
  const horizon = prices.slice(start, start + LOOKAHEAD_SLOTS);
  if (horizon.length < MIN_HORIZON_SLOTS || horizon[0]?.priceSource !== 'actual') {
    return unavailable('Det saknas minst sex timmars giltig prisprognos.');
  }
  if (horizon.some((slot) => !Number.isFinite(slot.buy) ||
      slot.buy < -500 || slot.buy > 3000 ||
      (slot.priceSource !== 'actual' && slot.priceSource !== 'forecast'))) {
    return unavailable('Prisunderlaget innehåller ogiltiga eller okända priser.');
  }
  const values = horizon.map((slot) => slot.buy +
    (slot.priceSource === 'forecast' ? forecastRiskOre : 0));
  const sorted = [...values].sort((a, b) => a - b);
  let threshold = sorted[Math.floor((sorted.length - 1) * CHEAP_QUANTILE)];
  const median = sorted[Math.floor((sorted.length - 1) / 2)];
  if (maxBuyOre !== null) {
    threshold = Math.min(threshold, maxBuyOre);
  } else if (median > 0) {
    // Flat expensive horizons must NOT be labeled as cheap. Zero/negative is
    // allowed even if all future quarters have the same price.
    threshold = Math.min(threshold, Math.round(median * 100) / 100 - 0.01);
  }
  const slots = horizon.map((slot, i) => ({
    time: slot.time,
    priceOre: values[i],
    charging: values[i] <= threshold,
    forecast: slot.priceSource === 'forecast',
  }));
  const chargeSlots = slots.filter((slot) => slot.charging).length;
  return {
    status: 'preview',
    reason: 'Ekonomiska prisfönster – ingen aktiv EVCC-styrning eller verifierat energibehov.',
    slots,
    thresholdOre: Math.round(threshold * 100) / 100,
    chargeSlots,
    estimatedKwhAt6A: Math.round(chargeSlots * EV_KWH_PER_SLOT_AT_6A * 100) / 100,
    hasForecast: slots.some((slot) => slot.forecast),
  };
}
