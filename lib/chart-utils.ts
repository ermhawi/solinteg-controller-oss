import { slotSolarKwh } from './slot-utils';
import { stockholmSlotKey } from './economics';
import type { PriceSlot } from './prices';
import type { DispatchSlot, Action } from './optimizer';
import type { ActualSlotFlows } from './actual-flows';

// A zone marks a DELIBERATE optimizer decision — something a plain self-consumption battery
// would never do on its own. Default behaviour draws nothing: discharging to cover load at
// night, charging from solar surplus, and exporting solar when the battery is full are all what
// the inverter's auto mode does anyway, and are readable from the SoC line against the solar
// curve. The three decisions:
//   buy  — the plan grid-funds the battery (charging beyond what solar provides)
//   sell — the plan exports battery energy to the grid (selling into a price peak)
//   hold — the plan lets the GRID cover house load while the battery sits on usable charge,
//          reserving it for a better hour — the least obvious decision, hence made visible
// Classification runs on the ATTRIBUTED flows the optimizer emits (batteryToGridKwh etc.), not
// on net gridKwh: the net number mixes house load into the picture (a solar-funded charge while
// the house imports a little for load is not a grid-buy decision, and a discharge that mostly
// covers load with a dribble of export is not a sell). A one-slot gap inside a sell/buy run is
// deliberately rendered as a gap — "not this slot" (a momentary price dip) is itself a decision
// worth seeing, so bands are never merged across sub-threshold slots.
export type BandKind = 'buy' | 'sell' | 'hold';

// kWh per 15-min slot (≈2 kW average) — below this a battery-grid flow is slot-granularity
// slop around covering load, not a decision. Real planned buys/sells run ~2-2.7 kWh/slot.
const DECISION_GRID_KWH = 0.5;
const MINOR_GRID_CHARGE_KWH = 0.05;

/** True for a planned grid charge that is real but below the strong decision-zone threshold. */
export function isMinorGridCharge(d: Pick<DispatchSlot, 'gridToBatteryKwh'>): boolean {
  return d.gridToBatteryKwh >= MINOR_GRID_CHARGE_KWH && d.gridToBatteryKwh < DECISION_GRID_KWH;
}

// Grid-covered load before a slot reads as a "hold". Must clear the DP's SoC discretisation
// step (~0.123 kWh for the reference 25.6 kWh battery): a discharge-to-load slot can leave up
// to one step of load to the grid purely from snap-to-grid rounding, which is not a reserve
// decision.
const HOLD_LOAD_FROM_GRID_KWH = 0.15;
// The battery must hold at least this much above the discharge floor for "reserving" to be a
// meaningful choice — an empty battery isn't holding back, it has nothing to give.
const HOLD_SOC_HEADROOM_KWH = 1;

// Divergence marker thresholds (buy/sell zones only — see isBandDivergent): a fully-elapsed
// zone's measured total must clear BOTH an absolute and a relative floor before it's flagged.
// The absolute floor keeps small zones from flagging on poller-noise-level wiggle; the relative
// floor keeps large zones from flagging on a proportionally tiny miss. A guard skipping even
// one 15-min slot of a multi-slot zone (≈2+ kWh) clears both comfortably.
const DIVERGENCE_MIN_KWH = 0.75;
const DIVERGENCE_FRACTION = 0.25;

// Swedish labels for control_actions outcomes surfaced in the hover's "Ingrepp" line — the
// dispatch loop's own guard vocabulary (see deploy/schema.sql's control_actions taxonomy and
// lib/dispatch-card.ts's fuller reason-building, which this deliberately doesn't reuse: that
// module needs both plannedAction and outcome together, whereas here we only have a bare list
// of outcomes per slot).
const INTERVENTION_LABEL: Record<string, string> = {
  skipped_solar_shortfall: 'Solunderskott',
  skipped_divergence: 'SoC-avvikelse',
  error_reverted: 'Fel',
  error_revert_failed: 'Fel',
};

/** Swedish label for a raw control_actions outcome, for the hover's intervention line. Falls
 *  back to the raw string for any outcome not in the map (defensive — 'applied' never reaches
 *  here, see readControlActionsForDay's own filter). */
export function interventionLabel(outcome: string): string {
  return INTERVENTION_LABEL[outcome] ?? outcome;
}

export interface ActionBand {
  x1: string;
  x2: string;
  kind: BandKind;
  kwh: number; // planned energy behind the decision, summed over the band's slots (see bandFlowKwh)
}

/** The flow a band's kind is classified from — buy: grid→battery, sell: battery→grid,
 *  hold: load left to the grid (what the battery is deliberately not covering). */
function bandFlowKwh(d: DispatchSlot, kind: BandKind): number {
  if (kind === 'buy') return d.gridToBatteryKwh;
  if (kind === 'sell') return d.batteryToGridKwh;
  return d.loadFromGridKwh;
}

/** Classify a slot's deliberate decision; null for default self-use behaviour. batteryFloorKwh
 *  is the operational SoC floor (BATTERY_MIN_SOC_KWH) — passed in rather than imported so this
 *  stays usable from the 'use client' chart hook (see buildChartData's own note below). */
export function classifyBand(d: DispatchSlot, batteryFloorKwh: number): BandKind | null {
  if (d.gridToBatteryKwh >= DECISION_GRID_KWH) return 'buy';
  if (d.batteryToGridKwh >= DECISION_GRID_KWH) return 'sell';
  if (d.loadFromGridKwh >= HOLD_LOAD_FROM_GRID_KWH && d.socAfter >= batteryFloorKwh + HOLD_SOC_HEADROOM_KWH) {
    return 'hold';
  }
  return null;
}

export interface ChartPoint {
  time: string;
  buy: number; // full consumer buy price: priceIncludingTaxAndSurcharge + skatt/överföring
  sell: number; // the sell price (spot + EXPORT_BONUS_ORE nätnytta) — the export/sell price
  priceSource?: 'actual' | 'forecast';
  socPct: number | null; // planned battery SoC % after this slot (null when there is no plan)
  actualSocPct: number | null; // real measured SoC %, averaged into this slot (null: future, or no readings)
  solarKwh: number;
  solarSource: 'forecast' | 'stale' | 'typical';
  action: Action;
  decision: BandKind | null; // the slot's deliberate decision (classifyBand), for the tooltip
  // Planned per-slot battery flows (kWh/15 min), for the tooltip's dispatch quantities —
  // null when the slot has no dispatch plan. Same attribution semantics as DispatchSlot.
  gridToBatteryKwh: number | null;
  batteryToGridKwh: number | null;
  batteryToLoadKwh: number | null;
  // Measured (actual) per-slot battery flows, for the tooltip's plan-vs-actual reconciliation —
  // null when the slot has no reading coverage (future, or a poller gap). See lib/actual-flows.ts.
  actual: ActualSlotFlows | null;
  // Raw control_actions outcomes (excluding 'applied') logged for this slot, oldest first,
  // deduped — see readControlActionsForDay. Empty when nothing intervened / no data.
  interventions: string[];
}

/** Limit chart input to the interval covered by the current forward plan. */
export function limitPricesToPlan(prices: PriceSlot[], dispatch: Pick<DispatchSlot, 'startTime'>[]): PriceSlot[] {
  if (dispatch.length === 0) return prices;
  const lastPlannedTime = dispatch.reduce(
    (latest, slot) => (slot.startTime > latest ? slot.startTime : latest),
    dispatch[0].startTime,
  );
  return prices.filter((price) => price.startTime <= lastPlannedTime);
}

/**
 * The price chart is a forecast view, not a window into the executable battery plan.
 * Preserve up to three Stockholm calendar days of prices even when the dispatch plan
 * (intentionally) covers only actual spot prices. Never show a partial fourth day.
 * Date.UTC is used just for calendar arithmetic; the local slot timestamps retain
 * their 92/96/100-slot daylight-saving-time day lengths.
 */
export function limitPricesToThreeDays(prices: PriceSlot[], today: string): PriceSlot[] {
  const [year, month, day] = today.split('-').map(Number);
  const thirdDay = new Date(Date.UTC(year, month - 1, day + 2)).toISOString().slice(0, 10);
  return prices.filter((slot) => {
    const date = slot.startTime.slice(0, 10);
    return date >= today && date <= thirdDay;
  });
}

/** Whether the tooltip should show a small battery export outside a deliberate sell zone. */
export function shouldShowSupplementalBatteryExport(point: ChartPoint): boolean {
  return point.decision == null && point.batteryToGridKwh != null && point.batteryToGridKwh > 0;
}

/**
 * Uses the first raw poller reading in each 15-min slot, keyed the same way buildPriceLookup
 * keys price slots. The first reading is the closest available measurement to the slot boundary,
 * which keeps actual SoC comparable to the planned SoC plotted at the slot start.
 */
export function buildActualSocByTime(
  readings: { timestamp: string; soc_pct: number }[],
): Record<string, number> {
  const out: Record<string, number> = {};
  for (const r of readings) {
    if (r.soc_pct == null || !isFinite(r.soc_pct)) continue;
    const key = stockholmSlotKey(r.timestamp);
    if (out[key] == null) out[key] = Math.round(r.soc_pct * 10) / 10;
  }
  return out;
}

/**
 * Collapses consecutive same-kind dispatch slots into contiguous decision bands (see the
 * taxonomy above classifyBand). Slots with no deliberate decision are skipped, and a
 * sub-threshold slot inside a run splits it — that gap is real signal, never merged over.
 */
export function buildActionBands(schedule: DispatchSlot[], batteryFloorKwh: number): ActionBand[] {
  const bands: ActionBand[] = [];
  let i = 0;
  while (i < schedule.length) {
    const kind = classifyBand(schedule[i], batteryFloorKwh);
    if (kind !== null) {
      const x1 = schedule[i].startTime;
      let j = i + 1;
      while (j < schedule.length && classifyBand(schedule[j], batteryFloorKwh) === kind) j++;
      let kwh = 0;
      for (let k = i; k < j; k++) kwh += bandFlowKwh(schedule[k], kind);
      bands.push({ x1, x2: schedule[j - 1].startTime, kind, kwh: Math.round(kwh * 100) / 100 });
      i = j;
    } else {
      i++;
    }
  }
  return bands;
}

/** The ACTUAL flow a band's kind is reconciled against — mirrors bandFlowKwh's kind selection
 *  exactly, just reading ChartPoint.actual instead of a DispatchSlot. */
function actualFlowKwh(a: ActualSlotFlows, kind: BandKind): number {
  if (kind === 'buy') return a.gridToBatteryKwh;
  if (kind === 'sell') return a.batteryToGridKwh;
  return a.loadFromGridKwh;
}

export interface BandActualSummary {
  kwh: number;
  /** False the moment ANY slot in the band lacks actual data (still in the future, or a poller
   *  gap) — callers must never present an incomplete sum as the band's real total. */
  complete: boolean;
}

/**
 * Sums a band's kind-relevant ACTUAL flow across its slots, for the hover's plan-vs-actual
 * reconciliation row and the divergence marker. `band.x1`/`x2` are startTime strings already
 * present in `timeIndex` (they came from the same chartData) — a miss only happens for a
 * malformed caller input, in which case this returns a `complete: false` zero rather than
 * throwing.
 */
export function sumActualForBand(
  band: ActionBand,
  chartData: ChartPoint[],
  timeIndex: Map<string, number>,
): BandActualSummary {
  const i1 = timeIndex.get(band.x1);
  const i2 = timeIndex.get(band.x2);
  if (i1 == null || i2 == null) return { kwh: 0, complete: false };
  let kwh = 0;
  let complete = true;
  for (let i = i1; i <= i2; i++) {
    const a = chartData[i]?.actual;
    if (!a) {
      complete = false;
      continue;
    }
    kwh += actualFlowKwh(a, band.kind);
  }
  return { kwh: Math.round(kwh * 100) / 100, complete };
}

/**
 * True when a fully-elapsed band's measured total diverges from its planned total by more than
 * both the absolute and relative thresholds (DIVERGENCE_MIN_KWH / DIVERGENCE_FRACTION) — the
 * chart's ⚠ marker. Always false for an incomplete summary: a band still partly in the future,
 * or with a poller gap inside it, must never be flagged as having gone wrong.
 */
export function isBandDivergent(plannedKwh: number, actual: BandActualSummary): boolean {
  if (!actual.complete) return false;
  const threshold = Math.max(DIVERGENCE_MIN_KWH, DIVERGENCE_FRACTION * plannedKwh);
  return Math.abs(actual.kwh - plannedKwh) > threshold;
}

/**
 * Maps each price slot to a chart-ready data point with BOTH prices shown at once:
 *  - buy  = full consumer price (priceIncludingTaxAndSurcharge + skatt/överföring) — what the
 *           optimizer decides on and what you pay to import.
 *  - sell = the sell price (spot + EXPORT_BONUS_ORE nätnytta) — what you actually receive per exported kWh.
 * dispatchByTime is a pre-keyed lookup. batteryKwh/skattOverforing/batteryFloorKwh are passed in
 * (rather than imported from lib/constants) because this runs inside a 'use client' hook
 * (useChartData) — Next.js never exposes non-NEXT_PUBLIC_ env vars to the client bundle, so a
 * direct import would silently read the hardcoded fallback instead of the deployment's real
 * env-configured value. Callers must pass the values resolved server-side (see app/page.tsx).
 */
export function buildChartData(
  prices: PriceSlot[],
  forecast: Record<string, number[]> | null | undefined,
  profiles: Record<number, number[]>,
  dispatchByTime: Record<string, DispatchSlot>,
  batteryKwh: number,
  skattOverforing: number,
  batteryFloorKwh: number,
  actualSocByTime: Record<string, number> = {},
  actualFlowsByTime: Record<string, ActualSlotFlows> = {},
  interventionsByTime: Record<string, string[]> = {},
  startSocKwh = batteryKwh / 2,
  plannedSocKwhByTime: Record<string, number> = {},
): ChartPoint[] {
  // Decision bands occupy [slot start, slot start + 15 min]. Plot the SoC at the
  // slot boundary, not socAfter, otherwise each charge/discharge appears one slot
  // earlier than its decision band.
  const socAtStartByTime = new Map<string, number>();
  let soc = startSocKwh;
  for (const dispatch of Object.values(dispatchByTime).sort((a, b) => a.startTime.localeCompare(b.startTime))) {
    socAtStartByTime.set(dispatch.startTime, soc);
    soc = dispatch.socAfter;
  }

  return prices.map((slot) => {
    const { kwh: solarKwh, source: solarSource } = slotSolarKwh(slot.startTime, forecast, profiles);
    const dispatch = dispatchByTime[slot.startTime];
    return {
      time: slot.startTime,
      buy: slot.priceIncludingTaxAndSurcharge + skattOverforing,
      sell: slot.price,
      priceSource: slot.source,
      socPct: dispatch
        ? Math.round(((plannedSocKwhByTime[slot.startTime] ?? socAtStartByTime.get(slot.startTime) ?? dispatch.socAfter) / batteryKwh) * 1000) / 10
        : null,
      actualSocPct: actualSocByTime[slot.startTime.slice(0, 16)] ?? null,
      solarKwh,
      solarSource,
      action: dispatch?.action ?? 'idle',
      decision: dispatch ? classifyBand(dispatch, batteryFloorKwh) : null,
      gridToBatteryKwh: dispatch ? dispatch.gridToBatteryKwh : null,
      batteryToGridKwh: dispatch ? dispatch.batteryToGridKwh : null,
      batteryToLoadKwh: dispatch ? dispatch.batteryToLoadKwh : null,
      actual: actualFlowsByTime[slot.startTime] ?? null,
      interventions: interventionsByTime[slot.startTime] ?? [],
    };
  });
}

/**
 * Returns the startTime of every even-hour :00 slot — the X-axis tick positions.
 */
export function buildXTicks(prices: PriceSlot[]): string[] {
  return prices
    .filter(
      (s) =>
        s.startTime.slice(14, 16) === '00' &&
        parseInt(s.startTime.slice(11, 13), 10) % 2 === 0,
    )
    .map((s) => s.startTime);
}

const STHLM_NOW_FORMAT = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Europe/Stockholm',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
});

/**
 * The current 15-min slot's startTime ("YYYY-MM-DDTHH:MM:00"), Stockholm local, floored to the
 * slot boundary — used to draw a "now" marker that lines up exactly with a loaded x value. Returns
 * null if that slot isn't among `availableTimes` (e.g. "now" falls outside the fetched horizon),
 * so the caller never renders a marker at a nonexistent point.
 */
export function computeNowSlotTime(now: Date, availableTimes: string[]): string | null {
  const parts = STHLM_NOW_FORMAT.formatToParts(now);
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? '00';
  const hour = get('hour') === '24' ? '00' : get('hour'); // some engines emit 24 at midnight
  const flooredMin = Math.floor(parseInt(get('minute'), 10) / 15) * 15;
  const candidate = `${get('year')}-${get('month')}-${get('day')}T${hour}:${String(flooredMin).padStart(2, '0')}:00`;
  return availableTimes.includes(candidate) ? candidate : null;
}

// ─── SVG chart geometry ──────────────────────────────────────────────────────
// The chart's horizon is variable-length (96 slots today-only, ~192 today+tomorrow), so x is
// scaled by array index rather than hour-of-day — unlike a fixed-width mockup, this needs no
// knowledge of how many hours are actually loaded.

export interface ChartGeometry {
  width: number;
  height: number;
  padL: number;
  padR: number;
  padT: number;
  padB: number;
  plotW: number;
  plotH: number;
  baseY: number;
}

export function buildChartGeometry(width = 1040, height = 388): ChartGeometry {
  const padL = 42;
  const padR = 46;
  const padT = 30;
  const padB = 26;
  const plotW = width - padL - padR;
  const plotH = height - padT - padB;
  return { width, height, padL, padR, padT, padB, plotW, plotH, baseY: padT + plotH };
}

/** time → array index, so timeToX doesn't do a linear scan per lookup. */
export function buildTimeIndex(chartData: ChartPoint[]): Map<string, number> {
  const m = new Map<string, number>();
  chartData.forEach((d, i) => m.set(d.time, i));
  return m;
}

export function indexToX(index: number, count: number, geometry: ChartGeometry): number {
  if (count <= 1) return geometry.padL;
  return geometry.padL + (index / (count - 1)) * geometry.plotW;
}

/** Null when `time` isn't in chartData (e.g. "now" outside the fetched horizon) — mirrors
 *  computeNowSlotTime's contract so callers never draw a marker at a nonexistent point. */
export function timeToX(
  time: string,
  chartData: ChartPoint[],
  geometry: ChartGeometry,
  timeIndex?: Map<string, number>,
): number | null {
  const idx = timeIndex ? timeIndex.get(time) : chartData.findIndex((d) => d.time === time);
  if (idx == null || idx < 0) return null;
  return indexToX(idx, chartData.length, geometry);
}

/** Real-data-derived price axis max (öre/kWh): headroom above the highest buy/sell value,
 *  rounded up to a round number, floored so a flat/near-zero price day still gets a sane axis. */
export function computePriceMax(chartData: ChartPoint[]): number {
  const max = chartData.reduce((m, d) => Math.max(m, d.buy, d.sell), 0);
  return Math.max(100, Math.ceil((max + 10) / 20) * 20);
}

/** Real-data-derived price axis min (öre/kWh): 0 on a normal day, but a negative round number
 *  (with headroom, mirroring computePriceMax) when any price dips below zero — negative spot
 *  prices are real on sunny/windy days, and a 0-floored axis would draw them outside the plot. */
export function computePriceMin(chartData: ChartPoint[]): number {
  const min = chartData.reduce((m, d) => Math.min(m, d.buy, d.sell), 0);
  if (min >= 0) return 0; // headroom only once something actually dips below zero
  return Math.floor((min - 10) / 20) * 20;
}

/** Real-data-derived solar axis max (kWh/slot): headroom above the highest slot, floored so a
 *  near-zero solar day (winter) still gets a sane axis. */
export function computeSolarMax(chartData: ChartPoint[]): number {
  const max = chartData.reduce((m, d) => Math.max(m, d.solarKwh), 0);
  return Math.max(0.5, max * 1.15);
}

/** min defaults to 0 (the everyday case); pass computePriceMin's value so days with negative
 *  prices keep every point inside the plot instead of dropping below the baseline. */
export function priceYScale(value: number, max: number, geometry: ChartGeometry, min = 0): number {
  return geometry.padT + (1 - (value - min) / (max - min)) * geometry.plotH;
}

/** Battery SoC is always shown on a fixed 0–100% scale. */
export function socYScale(value: number, geometry: ChartGeometry): number {
  return geometry.padT + (1 - value / 100) * geometry.plotH;
}

/** Solar is drawn as a contextual band hugging the plot's baseline, not the full plot height,
 *  so it stays visually secondary to the price lines. `band` is the fraction of plotH it uses. */
export function solarYScale(value: number, max: number, geometry: ChartGeometry, band = 0.44): number {
  return geometry.baseY - (value / max) * (geometry.plotH * band);
}

export function buildLinePath(points: [number, number][]): string {
  if (!points.length) return '';
  return 'M ' + points.map(([x, y]) => `${x.toFixed(1)} ${y.toFixed(1)}`).join(' L ');
}

/**
 * Builds a step path for quarter-hour prices: keep each price horizontal for its
 * entire slot, then change level vertically at the next slot boundary.
 */
export function buildStepLinePath(points: [number, number][]): string {
  if (!points.length) return '';
  const parts = [`M ${points[0][0].toFixed(1)} ${points[0][1].toFixed(1)}`];
  for (let i = 1; i < points.length; i++) {
    const [x, y] = points[i];
    const previousY = points[i - 1][1];
    parts.push(`L ${x.toFixed(1)} ${previousY.toFixed(1)}`);
    parts.push(`L ${x.toFixed(1)} ${y.toFixed(1)}`);
  }
  return parts.join(' ');
}

/** Calculates an index range for a two-finger pinch, anchored at the fingers' midpoint. */
export function pinchZoomRange(
  viewStart: number,
  viewEnd: number,
  fullEnd: number,
  initialDistance: number,
  currentDistance: number,
  centerFraction: number,
  minSpan = 8,
): { start: number; end: number } | null {
  if (!(initialDistance > 0) || !(currentDistance > 0) || fullEnd < 1) return null;
  const maxSpan = Math.max(1, fullEnd);
  const initialSpan = Math.max(1, viewEnd - viewStart);
  const nextSpan = Math.max(minSpan, Math.min(maxSpan, Math.round(initialSpan * (initialDistance / currentDistance))));
  if (nextSpan >= maxSpan) return null;

  const clampedFraction = Math.max(0, Math.min(1, centerFraction));
  const anchorIndex = viewStart + clampedFraction * initialSpan;
  const start = Math.max(0, Math.min(Math.round(anchorIndex - clampedFraction * nextSpan), fullEnd - nextSpan));
  return { start, end: start + nextSpan };
}

/** Closes a filled area under a top line, down to baseY, back along the baseline. */
export function buildAreaPath(topPoints: [number, number][], baseY: number): string {
  if (!topPoints.length) return '';
  const first = topPoints[0];
  const last = topPoints[topPoints.length - 1];
  const top = topPoints.map(([x, y]) => `${x.toFixed(1)} ${y.toFixed(1)}`).join(' L ');
  return `M ${first[0].toFixed(1)} ${baseY.toFixed(1)} L ${top} L ${last[0].toFixed(1)} ${baseY.toFixed(1)} Z`;
}
