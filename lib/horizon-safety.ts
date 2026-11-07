/** O04: source-only opt-in barrier against collapsed forecast horizons.
 *
 * A horizon length below the configured minimum may change the first slots'
 * discharge decision substantially. The default is OFF to avoid changing
 * installed or historically approved planning behavior without a separate GO.
 */
export type HorizonGate = {
  publishable: boolean;
  enabled: boolean;
  reason: 'disabled' | 'valid' | 'short' | 'invalid-configuration' | 'invalid-horizon';
  requiredHours: number | null;
  availableHours: number;
};

export function assessPublishableHorizon(
  slots: number,
  configuredHours: string | undefined,
): HorizonGate {
  const availableHours = Number.isSafeInteger(slots) && slots >= 0 ? slots / 4 : NaN;
  const base = { availableHours, requiredHours: null, enabled: false } as const;
  if (!Number.isFinite(availableHours)) {
    return { ...base, publishable: false, reason: 'invalid-horizon' };
  }
  // Explicit zero or absent means disabled (requires separate approval for opt-in).
  if (configuredHours === undefined || configuredHours === '' || configuredHours === '0') {
    return { ...base, publishable: true, reason: 'disabled' };
  }
  const requiredHours = Number(configuredHours);
  if (!/^\d+(?:\.\d+)?$/.test(configuredHours) ||
      !Number.isFinite(requiredHours) || requiredHours <= 0 || requiredHours > 72) {
    return { ...base, enabled: true, publishable: false, reason: 'invalid-configuration' };
  }
  return {
    availableHours, requiredHours, enabled: true,
    publishable: availableHours >= requiredHours,
    reason: availableHours >= requiredHours ? 'valid' : 'short',
  };
}
