'use client';

import { useEffect, useRef } from 'react';
import { useRouter } from 'next/navigation';

/**
 * Re-fetches server-rendered chart/plan/SoC data while the dashboard is open.
 * Systemstatus/Dispatch poll their own API routes independently.
 *
 * Use the server render time, not the component mount time: the App Router and
 * browser back/forward cache can restore an already-old page without remounting
 * it. Check immediately on mount/return, then every minute while visible.
 *
 * router.refresh() re-runs producePlan's uncached price/SoC reads. Solar/temp
 * forecasts deliberately keep their own longer cache lifetimes.
 */
const RETURN_MAX_AGE_MS = 15_000;
const MIN_REFRESH_GAP_MS = 10_000;

export default function AutoRefresh({
  renderedAt,
  intervalMs = 60_000,
}: {
  renderedAt: string;
  intervalMs?: number;
}) {
  const router = useRouter();
  const lastRefresh = useRef(0);

  useEffect(() => {
    const serverRenderedAt = Date.parse(renderedAt);
    const refreshIfStale = (maxAgeMs: number) => {
      if (document.visibilityState !== 'visible') return;
      const now = Date.now();
      if (now - serverRenderedAt < maxAgeMs) return;
      // router.refresh() is asynchronous; don't stack requests while one is pending.
      if (now - lastRefresh.current < MIN_REFRESH_GAP_MS) return;
      lastRefresh.current = now;
      router.refresh();
    };

    const refreshOnReturn = () => refreshIfStale(Math.min(intervalMs, RETURN_MAX_AGE_MS));
    const refreshOnTimer = () => refreshIfStale(intervalMs);

    refreshOnReturn(); // catches a stale App Router navigation on first mount
    const id = setInterval(refreshOnTimer, Math.min(intervalMs, 10_000));
    document.addEventListener('visibilitychange', refreshOnReturn);
    window.addEventListener('focus', refreshOnReturn);
    window.addEventListener('pageshow', refreshOnReturn);
    return () => {
      clearInterval(id);
      document.removeEventListener('visibilitychange', refreshOnReturn);
      window.removeEventListener('focus', refreshOnReturn);
      window.removeEventListener('pageshow', refreshOnReturn);
    };
  }, [router, renderedAt, intervalMs]);

  return null;
}
