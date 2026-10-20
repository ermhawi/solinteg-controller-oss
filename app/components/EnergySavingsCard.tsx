'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { useMobileViewport } from './useResponsiveLayout';
import type { SystemDailyTotals } from '@/lib/economics';

type Period = 'dag' | 'månad' | 'år';

type DailyEnergy = SystemDailyTotals & { date: string };

type AggregatedEnergy = SystemDailyTotals & { label: string; key: string };

const PERIOD_LABELS: Record<Period, string> = { dag: 'Dag', månad: 'Månad', år: 'År' };

function emptyTotals(): SystemDailyTotals {
  return {
    loadKwh: 0,
    solarKwh: 0,
    batteryChargeKwh: 0,
    batteryDischargeKwh: 0,
    boughtKwh: 0,
    soldKwh: 0,
    actualCostKr: 0,
    baselineCostKr: 0,
    savingsKr: 0,
    readingCount: 0,
  };
}

function periodKey(date: string, period: Period): string {
  if (period === 'år') return date.slice(0, 4);
  if (period === 'månad') return date.slice(0, 7);
  return date;
}

function formatLabel(key: string, period: Period): string {
  if (period === 'år') return key;
  if (period === 'månad') return new Intl.DateTimeFormat('sv-SE', { month: 'short', year: '2-digit' }).format(new Date(`${key}-15T12:00:00`));
  return new Intl.DateTimeFormat('sv-SE', { day: 'numeric', month: 'short' }).format(new Date(`${key}T12:00:00`));
}

function kr(value: number, digits = 0): string {
  return new Intl.NumberFormat('sv-SE', {
    style: 'currency',
    currency: 'SEK',
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);
}

function kwh(value: number): string {
  return `${value.toFixed(1)} kWh`;
}

function axisTicks(min: number, max: number, count = 4): number[] {
  return Array.from({ length: count + 1 }, (_, index) => min + ((max - min) * index) / count);
}

function axisLabel(value: number): string {
  return `${Math.round(value)} kr`;
}

function aggregate(rows: DailyEnergy[], period: Period): AggregatedEnergy[] {
  const grouped = new Map<string, SystemDailyTotals>();
  for (const row of rows) {
    const key = periodKey(row.date, period);
    const acc = grouped.get(key) ?? emptyTotals();
    for (const field of Object.keys(acc) as (keyof SystemDailyTotals)[]) {
      acc[field] += row[field];
    }
    grouped.set(key, acc);
  }
  return [...grouped.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([key, totals]) => ({
    ...totals,
    key,
    label: formatLabel(key, period),
  }));
}

function Metric({ label, value, detail, tone = 'neutral' }: { label: string; value: string; detail?: string; tone?: 'good' | 'neutral' | 'muted' }) {
  const color = tone === 'good' ? 'var(--econ-green)' : tone === 'muted' ? 'var(--text-muted)' : 'var(--text)';
  return (
    <div className="min-w-0 rounded-2xl px-3 py-2.5" style={{ background: 'var(--badge-bg)' }}>
      <div className="text-[12px] font-bold uppercase tracking-wide" style={{ color: 'var(--text-muted)' }}>{label}</div>
      <div className="mt-0.5 truncate text-[18px] font-bold leading-tight" style={{ color, fontFamily: 'var(--font-heading)' }}>{value}</div>
      {detail && <div className="mt-0.5 line-clamp-2 text-[12px] font-medium" style={{ color: 'var(--text-muted)' }}>{detail}</div>}
    </div>
  );
}

export default function EnergySavingsCard({ daily }: { daily: DailyEnergy[] }) {
  const [period, setPeriod] = useState<Period>('dag');
  const [hoveredKey, setHoveredKey] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const isMobile = useMobileViewport();
  const costScrollRef = useRef<HTMLDivElement>(null);
  const savingsScrollRef = useRef<HTMLDivElement>(null);
  const periods = useMemo(() => aggregate(daily, period), [daily, period]);
  const visible = periods.slice(-31);
  const latest = periods[periods.length - 1];
  const total = latest ?? emptyTotals();

  useEffect(() => {
    if (!isMobile || !expanded) return;
    for (const node of [costScrollRef.current, savingsScrollRef.current]) {
      if (node) node.scrollLeft = node.scrollWidth - node.clientWidth;
    }
  }, [isMobile, expanded, period]);

  if (daily.length === 0) {
    return (
      <section className="card-surface box-border min-w-0 p-5 order-7 min-[700px]:col-span-2 min-[1600px]:order-none min-[1600px]:[grid-column:1/span_3]">
        <h2 className="text-[15px] font-bold" style={{ fontFamily: 'var(--font-heading)' }}>Besparing från sol och batteri</h2>
        <p className="mt-1 text-sm" style={{ color: 'var(--text-muted)' }}>Inväntar tillräckligt med mätdata.</p>
      </section>
    );
  }

  const minCost = Math.min(0, ...visible.map((row) => row.actualCostKr));
  const maxCost = Math.max(1, ...visible.map((row) => Math.max(row.baselineCostKr, row.actualCostKr)));
  const costRange = Math.max(1, maxCost - minCost);
  const chartHeight = 148;
  const chartWidth = Math.max(520, visible.length * 42);
  const chartLeft = 42;
  const plotWidth = chartWidth - chartLeft;
  const barWidth = Math.max(5, Math.min(13, 28 / Math.max(1, visible.length / 12)));
  const groupWidth = plotWidth / Math.max(1, visible.length);
  const zeroY = chartHeight - ((0 - minCost) / costRange) * chartHeight;
  const costTicks = axisTicks(minCost, maxCost);
  const savingsMin = Math.min(0, ...visible.map((row) => row.savingsKr));
  const savingsMax = Math.max(1, ...visible.map((row) => row.savingsKr));
  const savingsRange = Math.max(1, savingsMax - savingsMin);
  const savingsZeroY = chartHeight - ((0 - savingsMin) / savingsRange) * chartHeight;
  const savingsTicks = axisTicks(savingsMin, savingsMax);
  const savingsPoints = visible
    .map((row, index) => `${chartLeft + index * groupWidth + groupWidth / 2},${chartHeight - ((row.savingsKr - savingsMin) / savingsRange) * chartHeight}`)
    .join(' ');
  const hovered = visible.find((row) => row.key === hoveredKey) ?? null;

  return (
    <section className="card-surface box-border min-w-0 p-5 order-7 min-[700px]:col-span-2 min-[1600px]:order-none min-[1600px]:[grid-column:1/span_3]" aria-label="Besparing från sol och batteri">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-[15px] font-bold" style={{ fontFamily: 'var(--font-heading)' }}>Besparing från sol och batteri</h2>
          <p className="mt-0.5 text-[11px] font-medium" style={{ color: 'var(--text-muted)' }}>
            Faktiskt uppmätt jämfört med samma husförbrukning utan solceller och batteri
          </p>
        </div>
        <div className="flex gap-1 rounded-xl p-1" style={{ background: 'var(--badge-bg)' }}>
          {(Object.keys(PERIOD_LABELS) as Period[]).map((value) => (
            <button
              key={value}
              type="button"
              className="min-h-11 min-w-11 rounded-lg px-3 text-[13px] font-semibold min-[900px]:min-h-0 min-[700px]:py-1"
              style={period === value ? { background: 'var(--card-bg)', boxShadow: 'var(--card-shadow)' } : { color: 'var(--text-muted)' }}
              onClick={() => setPeriod(value)}
            >
              {PERIOD_LABELS[value]}
            </button>
          ))}
        </div>
      </div>

      <div className="mt-4 grid grid-cols-2 gap-2 min-[700px]:grid-cols-4">
        <Metric label="Faktisk nettokostnad" value={kr(total.actualCostKr)} detail={`${kwh(total.boughtKwh)} köpt · ${kwh(total.soldKwh)} sålt`} />
        <Metric label="Utan sol + batteri" value={kr(total.baselineCostKr)} detail={`${kwh(total.loadKwh)} husförbrukning`} tone="muted" />
        <Metric label="Besparing" value={kr(total.savingsKr)} detail="motkostnad − faktisk nettokostnad" tone="good" />
        <Metric label="Solproduktion" value={kwh(total.solarKwh)} detail={`${kwh(total.batteryChargeKwh)} laddat · ${kwh(total.batteryDischargeKwh)} urladdat`} tone="muted" />
      </div>

      <button type="button" className="chip mt-3 min-h-11 w-full rounded-xl px-3 text-left text-[13px] font-semibold min-[700px]:hidden"
        style={{ border: '1px solid var(--divider)' }} aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
        {expanded ? 'Dölj historik' : 'Visa historik och grafer'}
      </button>
      <div className={expanded ? 'mt-4 grid grid-cols-1 gap-4 min-[900px]:grid-cols-2' : 'mt-4 hidden grid-cols-1 gap-4 min-[700px]:grid min-[900px]:grid-cols-2'}>
        <div className="min-w-0 overflow-x-auto pb-1" ref={costScrollRef} tabIndex={0} aria-label="Kostnad över tid, svep för att se tidigare dagar">
          <div className="mb-1 text-[12px] font-bold uppercase tracking-wide" style={{ color: 'var(--text-muted)' }}>Kostnad jämfört med motkostnad</div>
          <svg
            viewBox={`0 0 ${chartWidth} ${chartHeight + 30}`}
            width={isMobile ? chartWidth : '100%'}
            height={chartHeight + 30}
            role="img"
            aria-label="Faktisk kostnad och motkostnad över tid"

          >
          {/* Touch selection is retained until another point is tapped, so mobile detail stays readable. */}
          {costTicks.map((tick, index) => {
            const y = chartHeight - ((tick - minCost) / costRange) * chartHeight;
            return (
              <g key={`cost-axis-${index}`}>
                <line x1={chartLeft} y1={y} x2={chartWidth} y2={y} stroke="var(--grid-line)" strokeWidth="1" />
                <text x={chartLeft - 6} y={y + 3} textAnchor="end" fontSize="11" fill="var(--text-muted)">{axisLabel(tick)}</text>
              </g>
            );
          })}
          <line x1={chartLeft} y1={zeroY} x2={chartWidth} y2={zeroY} stroke="var(--divider)" strokeWidth="1" />
          {visible.map((row, index) => {
            const center = chartLeft + index * groupWidth + groupWidth / 2;
            const actualHeight = (Math.abs(row.actualCostKr) / costRange) * chartHeight;
            const baselineHeight = (Math.max(0, row.baselineCostKr) / costRange) * chartHeight;
            const actualY = row.actualCostKr >= 0 ? zeroY - actualHeight : zeroY;
            const baselineY = zeroY - baselineHeight;
            return (
              <g
                key={row.key}
                onMouseEnter={() => setHoveredKey(row.key)}
                onMouseLeave={() => setHoveredKey(null)}
                onTouchStart={() => setHoveredKey(row.key)}
                style={{ cursor: 'default' }}
              >
                <rect x={center - barWidth - 2} y={actualY} width={barWidth} height={actualHeight} rx="2" fill="var(--econ-earned)" opacity="0.9" />
                <rect x={center + 2} y={baselineY} width={barWidth} height={baselineHeight} rx="2" fill="var(--econ-red)" opacity="0.8" />
                {hoveredKey === row.key && (
                  <rect
                    x={center - 5}
                    y={0}
                    width={10}
                    height={chartHeight}
                    fill="var(--text-muted)"
                    opacity="0.06"
                    pointerEvents="none"
                  />
                )}
                {(index === 0 || index === visible.length - 1 || index % Math.max(1, Math.ceil(visible.length / 6)) === 0) && (
                  <text x={center} y={chartHeight + 16} textAnchor="middle" fontSize="11" fill="var(--text-muted)">{row.label}</text>
                )}
              </g>
            );
          })}
          {hovered && (() => {
            const index = visible.findIndex((row) => row.key === hovered.key);
            const center = chartLeft + index * groupWidth + groupWidth / 2;
            const tooltipWidth = 164;
            const tooltipX = Math.max(4, Math.min(chartWidth - tooltipWidth - 4, center - tooltipWidth / 2));
            return (
              <g pointerEvents="none">
                <rect x={tooltipX} y={5} width={tooltipWidth} height={58} rx="7" fill="var(--node-bg)" stroke="var(--card-border)" />
                <text x={tooltipX + 9} y={19} fontSize="10" fontWeight="700" fill="var(--text)">{hovered.label}</text>
                <text x={tooltipX + 9} y={33} fontSize="11" fill="var(--text-muted)">Faktiskt: {kr(hovered.actualCostKr)}</text>
                <text x={tooltipX + 9} y={45} fontSize="11" fill="var(--text-muted)">Utan sol/batteri: {kr(hovered.baselineCostKr)}</text>
                <text x={tooltipX + 9} y={57} fontSize="11" fontWeight="700" fill="var(--econ-green)">Skillnad: {kr(hovered.savingsKr)}</text>
              </g>
            );
          })()}
          </svg>
        </div>
        <div className="min-w-0 overflow-x-auto pb-1" ref={savingsScrollRef} tabIndex={0} aria-label="Besparing över tid, svep för att se tidigare dagar">
          <div className="mb-1 text-[12px] font-bold uppercase tracking-wide" style={{ color: 'var(--text-muted)' }}>Besparing över tid</div>
          <svg
            viewBox={`0 0 ${chartWidth} ${chartHeight + 30}`}
            width={isMobile ? chartWidth : '100%'}
            height={chartHeight + 30}
            role="img"
            aria-label="Besparing över tid"

          >
            {savingsTicks.map((tick, index) => {
              const y = chartHeight - ((tick - savingsMin) / savingsRange) * chartHeight;
              return (
                <g key={`savings-axis-${index}`}>
                  <line x1={chartLeft} y1={y} x2={chartWidth} y2={y} stroke="var(--grid-line)" strokeWidth="1" />
                  <text x={chartLeft - 6} y={y + 3} textAnchor="end" fontSize="11" fill="var(--text-muted)">{axisLabel(tick)}</text>
                </g>
              );
            })}
            <line x1={chartLeft} y1={savingsZeroY} x2={chartWidth} y2={savingsZeroY} stroke="var(--divider)" strokeWidth="1" />
            <polyline points={savingsPoints} fill="none" stroke="var(--econ-green)" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
            {visible.map((row, index) => {
              const center = chartLeft + index * groupWidth + groupWidth / 2;
              const y = chartHeight - ((row.savingsKr - savingsMin) / savingsRange) * chartHeight;
              return (
                <g
                  key={row.key}
                  onMouseEnter={() => setHoveredKey(row.key)}
                  onMouseLeave={() => setHoveredKey(null)}
                  onTouchStart={() => setHoveredKey(row.key)}
                >
                  <circle cx={center} cy={y} r="9" fill="transparent" />
                  <circle cx={center} cy={y} r={hoveredKey === row.key ? 4 : 2.5} fill={row.savingsKr >= 0 ? 'var(--econ-green)' : 'var(--econ-red)'} />
                  {(index === 0 || index === visible.length - 1 || index % Math.max(1, Math.ceil(visible.length / 6)) === 0) && (
                    <text x={center} y={chartHeight + 16} textAnchor="middle" fontSize="11" fill="var(--text-muted)">{row.label}</text>
                  )}
                </g>
              );
            })}
          </svg>
        </div>
      </div>
      <div className={`flex flex-wrap items-center gap-3 text-[12px] font-semibold ${expanded ? '' : 'hidden min-[700px]:flex'}`} style={{ color: 'var(--text-muted)' }}>
        <span><i className="mr-1 inline-block h-2 w-2 rounded-sm" style={{ background: 'var(--econ-earned)' }} />Faktisk nettokostnad</span>
        <span><i className="mr-1 inline-block h-2 w-2 rounded-sm" style={{ background: 'var(--econ-red)', opacity: 0.8 }} />Utan sol + batteri</span>
        {latest && <span className="ml-auto">Senaste period: {latest.label} · {kr(latest.savingsKr)} sparat</span>}
      </div>
    </section>
  );
}
