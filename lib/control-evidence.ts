/** Five independent read-only evidence layers, never a physical control GO. */
export type EvidenceState = 'observed' | 'failed' | 'unknown' | 'stale';
export interface EvidenceLayer {
  state: EvidenceState;
  detail: string;
}
export interface ControlEvidence {
  process: EvidenceLayer;
  decision: EvidenceLayer;
  registerAccepted: EvidenceLayer;
  readback: EvidenceLayer;
  physicalEffect: EvidenceLayer;
  measuredBatteryW: number | null;
  physicalControlVerified: false;
}
export interface ControlActionObservation {
  timestamp: string;
  outcome: string;
  armed: boolean;
}
export interface ControlReadbackObservation {
  timestamp: string;
  readbackOk: boolean;
}
export interface BatteryPowerObservation {
  timestamp: string;
  batteryW: number;
}

function recent(timestamp: string | null | undefined, nowMs: number, ttlMs: number): boolean {
  if (typeof timestamp !== 'string') return false;
  const stamp = Date.parse(timestamp);
  return Number.isFinite(stamp) && Number.isFinite(nowMs) &&
    0 <= nowMs - stamp && nowMs - stamp <= ttlMs;
}

const UNKNOWN: EvidenceLayer = { state: 'unknown', detail: 'Ej oberoende verifierat' };

export function buildControlEvidence({
  action, readback, battery, nowMs,
}: {
  action?: ControlActionObservation | null;
  readback?: ControlReadbackObservation | null;
  battery?: BatteryPowerObservation | null;
  nowMs: number;
}): ControlEvidence {
  // Liveness must originate from independent systemd supervision. An HTTP 200
  // or a fresh dashboard/dispatch heartbeat is NOT a process-health proof.
  const process: EvidenceLayer = UNKNOWN;
  const decision: EvidenceLayer = !action ? UNKNOWN :
    !recent(action.timestamp, nowMs, 15 * 60_000)
      ? { state: 'stale', detail: 'Senaste beslut för gammalt' }
      : {
        state: action.outcome.startsWith('error') ? 'failed' : 'observed',
        detail: 'Loggat beslut: ' + action.outcome + (action.armed ? '' : ' (avväpnad)'),
      };

  // A logged 'applied' may also be a disarmed/no-op simulation, and a
  // transport-level ACK is not present in control_actions.
  const registerAccepted: EvidenceLayer = {
    state: 'unknown', detail: 'Saknar separat kommandokvitto; applied räcker inte',
  };
  const readbackLayer: EvidenceLayer = !readback ? UNKNOWN :
    !recent(readback.timestamp, nowMs, 5 * 60_000)
      ? { state: 'stale', detail: 'Registerreadback för gammal' }
      : readback.readbackOk
        ? { state: 'observed', detail: 'Registerreadback dokumenterad' }
        : { state: 'failed', detail: 'Registerreadback misslyckades' };

  // Inverter power is a measured physical observation, but attribution to an
  // accepted command requires independently verified mode/settling/SoC.
  const measuredBatteryW = battery && recent(battery.timestamp, nowMs, 120_000) &&
    Number.isFinite(battery.batteryW) ? battery.batteryW : null;
  const physicalEffect: EvidenceLayer = {
    state: 'unknown',
    detail: measuredBatteryW === null
      ? 'Fysisk kommandoeffekt ej verifierad; mätning saknas'
      : 'Fysisk kommandoeffekt ej verifierad; batteri-W är observation',
  };
  return {
    process, decision, registerAccepted, readback: readbackLayer, physicalEffect,
    measuredBatteryW, physicalControlVerified: false,
  };
}
