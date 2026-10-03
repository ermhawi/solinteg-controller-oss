'use client';

import { useEffect, useState } from 'react';
import { apiPath } from '@/lib/base-path';
import { DASHBOARD_POLL_MS } from './poll-cadence';

/**
 * Header "Live" indicator, polling the same /api/inverter route LiveInverterPanel uses — so the
 * global status line can no longer disagree with Systemstatus. Before this, the header's
 * "Live · uppdaterad nyss" text was static JSX in AppShell: it kept saying "Live" even while
 * PowerFlowCard showed its own "no current data" empty state after a poll failure, which looked
 * self-contradictory. /api/inverter returns 503 when readLiveInverterData() has nothing fresh
 * (see that route), so res.ok here is the same live/offline signal the rest of the dashboard
 * already uses.
 *
 * Starts optimistic (ok=true) rather than "checking…" — avoids a flash of the offline state on
 * every normal load while the first poll is in flight; the first real poll (fired immediately,
 * not just on the interval) corrects it within a second or two if the inverter is actually down.
 */
export default function LiveStatus({ today }: { today: string }) {
  const [ok, setOk] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const res = await fetch(apiPath('/api/inverter'), { cache: 'no-store' });
        if (!cancelled) setOk(res.ok);
      } catch {
        if (!cancelled) setOk(false);
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
          style={{ background: ok ? 'var(--live-dot)' : 'var(--color-warning)' }}
        />
        {ok && <span className="absolute inset-0 animate-live-pulse rounded-full" style={{ background: 'var(--live-dot)' }} />}
      </span>
      {ok ? `Live · uppdaterad nyss · ${today}` : `Frånkopplad från växelriktaren · ${today}`}
    </div>
  );
}
