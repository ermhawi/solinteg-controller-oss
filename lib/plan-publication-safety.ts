/** O06: opt-in, fail-closed planner-to-SQLite publication preflight.
 *
 * A live inverter sample, unbroken Stockholm local slot axis, coherent DP
 * output and a compatible SQLite schema are separate requirements. This
 * validates only software evidence, never physical implementation or BMS.
 */
import { BATTERY_KWH } from './constants';
import type { OptimizerSlot, DispatchSlot } from './optimizer';
import type { InverterLiveData } from './inverter';

export type PublicationReason =
  | 'disabled' | 'valid' | 'invalid-configuration' | 'invalid-clock'
  | 'missing-live-soc' | 'stale-live-soc' | 'invalid-soc'
  | 'incompatible-schema' | 'invalid-inputs' | 'invalid-time-axis' | 'invalid-dispatch';

export type PublicationGate = {
  publishable: boolean;
  enabled: boolean;
  reason: PublicationReason;
};

function timeLabel(time: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00$/.test(time)) return false;
  const parsed = new Date(time + 'Z');
  return Number.isFinite(parsed.getTime()) &&
    parsed.toISOString().slice(0, 19) === time &&
    Number(time.slice(14, 16)) % 15 === 0;
}

function lastSunday(year: number, month: number): string {
  const last = new Date(Date.UTC(year, month, 0));
  last.setUTCDate(last.getUTCDate() - last.getUTCDay());
  return last.toISOString().slice(0, 10);
}

function consecutiveStockholmSlots(before: string, after: string): boolean {
  const step = Date.parse(after + 'Z') - Date.parse(before + 'Z');
  if (step === 15 * 60_000) return true;
  const date = before.slice(0, 10);
  if (date !== after.slice(0, 10)) return false;
  const year = Number(date.slice(0, 4));
  return (step === 75 * 60_000 &&
      date === lastSunday(year, 3) &&
      before.slice(11, 16) === '01:45' &&
      after.slice(11, 16) === '03:00') ||
    (step === -45 * 60_000 &&
      date === lastSunday(year, 10) &&
      before.slice(11, 16) === '02:45' &&
      after.slice(11, 16) === '02:00');
}

function validInputs(inputs: readonly OptimizerSlot[]): PublicationReason | null {
  if (!inputs.length || inputs.length > 72 * 4) return 'invalid-inputs';
  const seenDstTransitions = new Set<string>();
  for (let i = 0; i < inputs.length; i++) {
    const slot = inputs[i];
    if (!timeLabel(slot.startTime)) return 'invalid-time-axis';
    if (i > 0) {
      const previous = inputs[i - 1].startTime;
      if (!consecutiveStockholmSlots(previous, slot.startTime)) return 'invalid-time-axis';
      const step = Date.parse(slot.startTime + 'Z') - Date.parse(previous + 'Z');
      if (step !== 15 * 60_000) {
        const transition = slot.startTime.slice(0, 10) + ':' + step;
        if (seenDstTransitions.has(transition)) return 'invalid-time-axis';
        seenDstTransitions.add(transition);
      }
    }
    if (![slot.buyPrice, slot.sellPrice, slot.solarKwh, slot.consumptionKwh]
      .every(v => typeof v === 'number' && Number.isFinite(v)) ||
      slot.solarKwh < 0 || slot.consumptionKwh == null || slot.consumptionKwh < 0) return 'invalid-inputs';
  }
  return null;
}

function validDispatch(inputs: readonly OptimizerSlot[], rows: readonly DispatchSlot[]): boolean {
  if (rows.length !== inputs.length) return false;
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];
    if (row.startTime !== inputs[i].startTime ||
        !['charge', 'discharge', 'idle', 'hold'].includes(row.action) ||
        !Number.isFinite(row.socAfter) || row.socAfter < 0 || row.socAfter > BATTERY_KWH ||
        ![row.gridKwh, row.solarExportKwh, row.batteryToGridKwh, row.gridToBatteryKwh,
          row.batteryToLoadKwh, row.loadFromGridKwh].every(Number.isFinite)) {
      return false;
    }
  }
  return true;
}

export function assessPlanPublication(args: {
  configured: string | undefined;
  nowMs: number;
  live: Pick<InverterLiveData, 'timestamp' | 'soc_kwh' | 'soc_pct'> | null;
  inputs: readonly OptimizerSlot[];
  dispatch: readonly DispatchSlot[];
  schemaCompatible: boolean;
}): PublicationGate {
  const result = (reason: PublicationReason, enabled = true): PublicationGate => ({
    reason, enabled, publishable: reason === 'valid' || reason === 'disabled',
  });
  if (args.configured === undefined || args.configured === '0') return result('disabled', false);
  if (args.configured !== '1') return result('invalid-configuration');
  if (!Number.isFinite(args.nowMs)) return result('invalid-clock');
  if (!args.live) return result('missing-live-soc');
  if (typeof args.live.timestamp !== 'string' ||
      !/Z$|[+-]\d{2}:\d{2}$/.test(args.live.timestamp)) return result('stale-live-soc');
  const sampleMs = Date.parse(args.live.timestamp);
  const ageMs = args.nowMs - sampleMs;
  if (!Number.isFinite(ageMs) || ageMs < -5000 || ageMs > 120_000) {
    return result('stale-live-soc');
  }
  if (![args.live.soc_pct, args.live.soc_kwh].every(v =>
    typeof v === 'number' && Number.isFinite(v)) ||
    args.live.soc_pct < 0 || args.live.soc_pct > 100 ||
    args.live.soc_kwh < 0 || args.live.soc_kwh > BATTERY_KWH) {
    return result('invalid-soc');
  }
  if (!args.schemaCompatible) return result('incompatible-schema');
  const inputError = validInputs(args.inputs);
  if (inputError) return result(inputError);
  return validDispatch(args.inputs, args.dispatch) ? result('valid') : result('invalid-dispatch');
}
