import { readRecentControlActions } from '@/lib/telemetry';
import { readOrFallback } from '@/lib/telemetry/core';
import { readLiveInverterData } from '@/lib/inverter';
import { buildControlEvidence } from '@/lib/control-evidence';
import type { ControlReadbackObservation } from '@/lib/control-evidence';

export async function GET() {
  const actions = readRecentControlActions(1);
  const action = actions.at(-1) ?? null;
  const readback = readOrFallback(null as ControlReadbackObservation | null, (db) => {
    const row = db.prepare(
      'SELECT timestamp, readback_ok FROM control_readbacks ORDER BY id DESC LIMIT 1',
    ).get() as { timestamp?: string; readback_ok?: number } | undefined;
    return row && typeof row.timestamp === 'string'
      ? { timestamp: row.timestamp, readbackOk: row.readback_ok === 1 } : null;
  });
  const live = await readLiveInverterData();
  const evidence = buildControlEvidence({
    action,
    readback,
    battery: live ? { timestamp: live.timestamp, batteryW: live.battery_w } : null,
    nowMs: Date.now(),
  });
  return Response.json(evidence, { headers: { 'Cache-Control': 'no-store' } });
}
