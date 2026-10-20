'use client';

import { useEffect, useRef, useState } from 'react';
import type { DispatchCardData, DispatchTone, GaugeTone, DecisionOutcome } from '@/lib/dispatch-card';
import { gaugeFillPct, gaugeTone } from '@/lib/dispatch-card';
import { DASHBOARD_POLL_MS } from './poll-cadence';
import { apiPath } from '@/lib/base-path';

const BADGE_TONE_STYLE: Record<DispatchTone, { bg: string; color: string }> = {
  AKTIV: { bg: 'color-mix(in srgb, var(--color-buy) 16%, transparent)', color: 'var(--color-buy)' },
  AVVAKTAR: { bg: 'color-mix(in srgb, var(--color-warning) 16%, transparent)', color: 'var(--color-warning)' },
  PLANERAT: { bg: 'var(--badge-bg)', color: 'var(--text-muted)' },
};

const GAUGE_TONE_COLOR: Record<GaugeTone, string> = {
  green: 'var(--color-buy)',
  amber: 'var(--color-warning)',
  red: 'var(--econ-red)',
};

const OUTCOME_BAR_COLOR: Record<DecisionOutcome, string> = {
  ok: 'var(--color-buy)',
  skip: 'var(--color-warning)',
  error: 'var(--econ-red)',
};

function formatAgo(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s sedan`;
  return `${Math.round(seconds / 60)} min sedan`;
}

function PriceChip({ label, value, color }: { label: string; value: number | undefined; color: string }) {
  return (
    <div
      className="flex flex-1 flex-col gap-1 rounded-[10px]"
      style={{ padding: '8px 10px', background: 'color-mix(in srgb, var(--text) 5%, transparent)' }}
    >
      <span className="text-[9.5px] font-semibold" style={{ color: 'var(--text-muted)' }}>
        {label}
      </span>
      <span className="text-[14px] font-bold" style={{ fontFamily: 'var(--font-heading)', color }}>
        {value != null ? `${value.toFixed(1)} öre` : '—'}
      </span>
    </div>
  );
}

function Gauge({ name, value, limit, unit }: { name: string; value: number; limit: number; unit: string }) {
  const pct = gaugeFillPct(value, limit);
  const tone = gaugeTone(pct);
  return (
    <div className="flex flex-col" style={{ gap: 4 }}>
      <div className="flex items-baseline justify-between">
        <span className="text-[12px] font-semibold min-[480px]:text-[11px]">{name}</span>
        <span className="text-[11.5px] font-semibold min-[480px]:text-[10.5px]" style={{ color: 'var(--text-muted)' }}>
          {value.toFixed(1)} av {limit.toFixed(1)} {unit}
        </span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full" style={{ background: 'var(--track-bg)' }}>
        <div
          className="h-full rounded-full"
          style={{ width: `${pct}%`, background: GAUGE_TONE_COLOR[tone] }}
        />
      </div>
    </div>
  );
}

export default function DispatchCard({ initialData }: { initialData: DispatchCardData | null }) {
  const [data, setData] = useState(initialData);
  const [expanded, setExpanded] = useState(false);
  const [selectedIdx, setSelectedIdx] = useState<number | null>(null);
  const [ageS, setAgeS] = useState<number | null>(
    initialData ? Math.round(initialData.current.secondsAgo) : null,
  );

  // Right-edge fade on the timeline strip below — a visual cue that there's more to scroll to,
  // since nothing else here hints a 12-bar-wide strip is scrollable on a narrow card. Only
  // shown while there's actually unscrolled content to the right (recomputed on scroll/resize
  // and whenever the decision list itself changes length).
  const timelineRef = useRef<HTMLDivElement>(null);
  const [timelineOverflows, setTimelineOverflows] = useState(false);
  const decisionsLength = data?.recentDecisions.length ?? 0;

  useEffect(() => {
    const el = timelineRef.current;
    if (!el) return;
    const check = () => setTimelineOverflows(el.scrollWidth - el.clientWidth - el.scrollLeft > 4);
    check();
    el.addEventListener('scroll', check, { passive: true });
    window.addEventListener('resize', check);
    return () => {
      el.removeEventListener('scroll', check);
      window.removeEventListener('resize', check);
    };
  }, [decisionsLength]);

  // Poll for fresh decisions — there's no push channel, and the underlying data changes at
  // most once per LOOP_INTERVAL_S server-side; polling on the shared dashboard cadence keeps
  // this card in step with the panels beside it (see poll-cadence.ts).
  //
  // ON FAILURE: keep the last known decision. A dispatch decision stays MEANINGFUL when it
  // ages — "reserving for the evening peak, 4 min ago" is still the reason the battery is
  // doing what it's doing, and the header shows how old it is. That is the opposite call from
  // Systemstatus, which blanks (see LiveInverterPanel), and the difference is deliberate:
  // live instantaneous power goes stale in seconds, a decision does not.
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const res = await fetch(apiPath('/api/dispatch'), { cache: 'no-store' });
        const next = res.ok ? ((await res.json()) as DispatchCardData) : null;
        if (!cancelled && next) setData(next);
      } catch {
        // swallowed on purpose — see ON FAILURE above
      }
    };
    const id = setInterval(tick, DASHBOARD_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  // Ticks the "Xs sedan" label between polls, extrapolating forward from the secondsAgo
  // captured at the last successful poll (same idea as the old DispatchStatus.tsx).
  useEffect(() => {
    if (!data) return;
    const base = data.current.secondsAgo;
    const startedAt = Date.now();
    const tick = () => setAgeS(Math.round(base + (Date.now() - startedAt) / 1000));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [data]);

  if (!data) {
    return (
      <div
        className="card-surface box-border flex flex-col gap-2 order-3 min-[1600px]:order-none min-[1600px]:flex-none"
        style={{ padding: '18px 20px' }}
      >
        <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
          Ingen dispatch-data ännu.
        </p>
      </div>
    );
  }

  const { current, recentDecisions } = data;
  const activeIdx = selectedIdx ?? recentDecisions.length - 1;
  const selected = recentDecisions[activeIdx];
  const tone = BADGE_TONE_STYLE[current.badge];

  return (
    <div
      className="card-surface box-border flex flex-col order-3 min-[1600px]:order-none min-[1600px]:flex-none"
      style={{ padding: '18px 20px', gap: 8 }}
    >
      {/* header */}
      <div className="flex items-center gap-2">
        <div
          className="flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-lg"
          style={{ background: 'var(--badge-bg)', color: 'var(--badge-color)' }}
        >
          <svg
            viewBox="0 0 24 24"
            style={{ width: 14, height: 14 }}
            fill="none"
            stroke="currentColor"
            strokeWidth={2.1}
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M4 15 L9 9 L13 12 L20 4" />
          </svg>
        </div>
        <span
          className="text-[15px] font-bold"
          style={{ fontFamily: 'var(--font-heading)', letterSpacing: '-0.2px' }}
        >
          Dispatch
        </span>
        <span
          className="rounded-full px-2 py-0.5 text-[11px] font-bold uppercase"
          style={{ letterSpacing: '0.4px', background: tone.bg, color: tone.color }}
        >
          {current.badge}
        </span>
        <span className="ml-auto shrink-0 text-[11px] font-medium" style={{ color: 'var(--text-muted)' }}>
          {ageS != null ? formatAgo(ageS) : ''}
        </span>
      </div>

      {/* reasoning (Layer 1, always visible) */}
      <p className="text-[14px] leading-[1.5]" style={{ fontFamily: 'var(--font-body)' }}>
        {current.reason}
      </p>

      {/* warning (conditional) */}
      {current.warning && (
        <div
          className="flex items-start gap-1.5 text-[11.5px] font-semibold"
          style={{ color: 'var(--color-warning)' }}
        >
          <svg
            viewBox="0 0 24 24"
            style={{ width: 14, height: 14, flexShrink: 0, marginTop: 1 }}
            fill="none"
            stroke="currentColor"
            strokeWidth={1.8}
            strokeLinejoin="round"
          >
            <path d="M12 3.5 L21.5 20 H2.5 Z" />
            <line x1="12" y1="9.5" x2="12" y2="14" strokeLinecap="round" />
            <circle cx="12" cy="16.8" r="0.9" fill="currentColor" stroke="none" />
          </svg>
          <span>{current.warning}</span>
        </div>
      )}

      {/* expand toggle */}
      <button
        type="button"
        onClick={() => setExpanded((e) => !e)}
        className="chip flex min-h-11 items-center gap-1 self-start rounded-lg px-2 text-[13px] font-bold"
        aria-expanded={expanded}
        style={{ color: 'var(--color-soc)', background: 'none', border: 'none' }}
      >
        {expanded ? 'Dölj detaljer' : 'Visa detaljer'}
        <svg
          viewBox="0 0 24 24"
          style={{ width: 11, height: 11, transition: 'transform .2s', transform: expanded ? 'rotate(180deg)' : 'none' }}
          fill="none"
          stroke="currentColor"
          strokeWidth={2.4}
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M5 9 L12 16 L19 9" />
        </svg>
      </button>

      {/* expanded detail (Layer 2) */}
      {expanded && (
        <div className="flex flex-col" style={{ gap: 12 }}>
          <div className="flex" style={{ gap: 8 }}>
            <PriceChip label="Köp" value={current.buyOre} color="var(--color-buy)" />
            <PriceChip label="Sälj" value={current.sellOre} color="var(--color-sell)" />
          </div>
          <Gauge
            name="Solunderskott"
            value={current.solarDeficitKwh}
            limit={current.solarDeficitLimitKwh}
            unit="kWh-gräns"
          />
          <Gauge
            name="SoC-avvikelse"
            value={current.socDeviationKwh}
            limit={current.socDeviationLimitKwh}
            unit="kWh"
          />
        </div>
      )}

      <div style={{ height: 1, background: 'var(--divider)', margin: '8px 0 2px' }} />

      {/* timeline header (Layer 3) */}
      <div className="flex flex-col gap-0.5">
        <span className="text-[13px] font-bold" style={{ fontFamily: 'var(--font-heading)' }}>
          Senaste besluten
        </span>
        <span className="text-[12px] font-medium" style={{ color: 'var(--text-muted)' }}>
          Senaste ~4 h · tryck för detaljer
        </span>
      </div>

      {/* Timeline bars keep their original visual width, with 44px touch targets.
          Scrolling exposes the full history without squeezing controls. The 4px
          inset leaves room for the selected bar's ring at either edge. */}
      <div className="relative">
        <div
          ref={timelineRef}
          className="flex items-end"
          style={{
            gap: 2,
            height: 44,
            overflowX: 'auto',
            padding: '0 4px',
            margin: '0 -4px',
            // Fades the bars themselves near the right edge — a mask rather than an overlay
            // box, so it doesn't need to match the card's background color (which is a
            // gradient, not a flat color, in dark mode — see --card-bg-grad in globals.css).
            // Only applied while there's unscrolled content (see the effect above); otherwise
            // it would misleadingly fade the last bar even when nothing more exists to scroll to.
            ...(timelineOverflows
              ? {
                  WebkitMaskImage: 'linear-gradient(to right, black calc(100% - 20px), transparent 100%)',
                  maskImage: 'linear-gradient(to right, black calc(100% - 20px), transparent 100%)',
                }
              : {}),
          }}
        >
          {recentDecisions.map((d, i) => {
          const isSelected = i === activeIdx;
          const height = Math.min(40, Math.max(10, 8 + d.powerKw * 5.5));
          const color = OUTCOME_BAR_COLOR[d.outcome];
          return (
            <button
              key={i}
              type="button"
              title={`${d.time} · ${d.action} · ${d.powerKw.toFixed(1)} kW`}
              aria-label={`${d.time} · ${d.action} · ${d.powerKw.toFixed(1)} kW`}
              aria-pressed={isSelected}
              onClick={() => setSelectedIdx(i)}
              style={{
                width: 44,
                height: 44,
                border: 'none',
                background: 'transparent',
                cursor: 'pointer',
                padding: 0,
                flexShrink: 0,
                display: 'flex',
                alignItems: 'flex-end',
                justifyContent: 'center',
              }}
            >
              <span aria-hidden="true" style={{ width: 22, height, borderRadius: 5, background: color,
                opacity: isSelected ? 1 : 0.55,
                boxShadow: isSelected ? `0 0 0 2px var(--card-bg), 0 0 0 4px ${color}` : 'none' }} />
            </button>
          );
          })}
        </div>
      </div>

      {/* selected-item summary */}
      {selected && (
        <div style={{ borderTop: '1px solid var(--divider)', paddingTop: 9 }}>
          <span className="text-[12px] font-semibold">
            {selected.time} · {selected.action} · {selected.powerKw.toFixed(1)} kW
          </span>
        </div>
      )}
    </div>
  );
}
