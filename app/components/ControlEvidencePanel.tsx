'use client';

import { useEffect, useState } from 'react';
import { apiPath } from '@/lib/base-path';
import type { ControlEvidence, EvidenceLayer } from '@/lib/control-evidence';
import { DASHBOARD_POLL_MS } from './poll-cadence';

const LABELS: { key: keyof Pick<ControlEvidence, 'process' | 'decision' | 'registerAccepted' | 'readback' | 'physicalEffect'>; label: string }[] = [
  { key: 'process', label: 'Process' },
  { key: 'decision', label: 'Beslut' },
  { key: 'registerAccepted', label: 'Skrivkvitto' },
  { key: 'readback', label: 'Registerreadback' },
  { key: 'physicalEffect', label: 'Fysisk effekt' },
];

const STATE_TEXT: Record<EvidenceLayer['state'], string> = {
  observed: 'Observerad',
  failed: 'Fel',
  unknown: 'Okänd',
  stale: 'För gammal',
};

/** Source/telemetry evidence only. The UI never grants physical control permission. */
export default function ControlEvidencePanel() {
  const [evidence, setEvidence] = useState<ControlEvidence | null>(null);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const response = await fetch(apiPath('/api/control-evidence'), {
          cache: 'no-store', redirect: 'manual',
        });
        const data = response.ok ? (await response.json()) as ControlEvidence : null;
        if (!cancelled) setEvidence(data?.physicalControlVerified === false ? data : null);
      } catch {
        if (!cancelled) setEvidence(null);
      }
    };
    tick();
    const timer = setInterval(tick, DASHBOARD_POLL_MS);
    return () => { cancelled = true; clearInterval(timer); };
  }, []);

  return (
    <section aria-label="Kontrollkvalitet" className="rounded-xl p-3 text-[11px]"
      style={{ background: 'var(--badge-bg)' }}>
      <p className="font-semibold" style={{ color: 'var(--color-warning)' }}>
        Styrning är inte fysiskt säkerhetsverifierad
      </p>
      <dl className="mt-2 grid gap-1">
        {LABELS.map(({ key, label }) => {
          const detail = evidence?.[key] ?? { state: 'unknown', detail: 'Mätning saknas' };
          return (
            <div key={key} className="flex flex-wrap justify-between gap-x-2 gap-y-0.5">
              <dt className="font-medium">{label}</dt>
              <dd style={{ color: detail.state === 'failed' ? 'var(--econ-red)' : 'var(--text-muted)' }}
                title={detail.detail}>{STATE_TEXT[detail.state]} · {detail.detail}</dd>
            </div>
          );
        })}
      </dl>
      {evidence?.measuredBatteryW != null && (
        <p className="mt-2" style={{ color: 'var(--text-muted)' }}>
          Batteri (färsk mätning): {evidence.measuredBatteryW.toFixed(0)} W.
          Mätning är inte bevis för att ett kommando verkställts.
        </p>
      )}
    </section>
  );
}
