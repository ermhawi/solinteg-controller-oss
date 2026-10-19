'use client';

import { useEffect, useState } from 'react';
import { apiPath } from '@/lib/base-path';
import { DASHBOARD_POLL_MS } from './poll-cadence';
import { classifyInverterResponse, INVERTER_STATUS_TEXT } from '@/lib/inverter-api-status';
import type { InverterApiStatus } from '@/lib/inverter-api-status';

/**
 * Header "Live" indicator, polling the same /api/inverter route LiveInverterPanel uses — so the
 * global status line can no longer disagree with Systemstatus. Before this, the header's
 * "Live · uppdaterad nyss" text was static JSX in AppShell even when the inverter had no
 * current data. Distinguish /api/inverter's 503 (stale telemetry) from authentication,
 * network, and server failures so a session timeout is not mistaken for a dead inverter.
 *
 * Starts optimistic (status='live') rather than "checking…" — avoids a flash of the offline state on
 * every normal load while the first poll is in flight; the first real poll (fired immediately,
 * not just on the interval) corrects it within a second or two if the inverter is actually down.
 */
export default function LiveStatus({ today }: { today: string }) {
  const [status, setStatus] = useState<InverterApiStatus>('live');

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const res = await fetch(apiPath('/api/inverter'), { cache: 'no-store', redirect: 'manual' });
        if (!cancelled) setStatus(classifyInverterResponse(res));
      } catch {
        if (!cancelled) setStatus('network-error');
      }
    };
    tick();
    const id = setInterval(tick, DASHBOARD_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  return (
    <div className="flex items-center gap-2 text-xs font-medium" style={{ color: 'var(--text-muted)' }}>
      <span className="relative h-[7px] w-[7px]">
        <span
          className="absolute inset-0 rounded-full"
          style={{ background: status === 'live' ? 'var(--live-dot)' : 'var(--color-warning)' }}
        />
        {status === 'live' && <span className="absolute inset-0 animate-live-pulse rounded-full" style={{ background: 'var(--live-dot)' }} />}
      </span>
      {INVERTER_STATUS_TEXT[status]} · {today}
    </div>
  );
}
