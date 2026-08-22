'use client';

import { useMemo, useState } from 'react';
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
      <div className="text-[10px] font-bold uppercase tracking-wide" style={{ color: 'var(--text-muted)' }}>{label}</div>
      <div className="mt-0.5 truncate text-[18px] font-bold leading-tight" style={{ color, fontFamily: 'var(--font-heading)' }}>{value}</div>
      {detail && <div className="mt-0.5 truncate text-[10px] font-medium" style={{ color: 'var(--text-muted)' }}>{detail}</div>}
    </div>
  );
}

export default function EnergySavingsCard({ daily }: { daily: DailyEnergy[] }) {
  const [period, setPeriod] = useState<Period>('dag');
  const [hoveredKey, setHoveredKey] = useState<string | null>(null);
  const periods = useMemo(() => aggregate(daily, period), [daily, period]);
  const visible = periods.slice(-31);
  const latest = periods[periods.length - 1];
  const total = latest ?? emptyTotals();

  if (daily.length === 0) {
    return (
      <section className="card-surface box-border min-w-0 p-5 min-[1600px]:[grid-column:1/span_3]">
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
  const barWidth = Math.max(5, Math.min(13, 28 / Math.max(1, visible.length / 12)));
  const groupWidth = chartWidth / Math.max(1, visible.length);
  const zeroY = chartHeight - ((0 - minCost) / costRange) * chartHeight;
  const hovered = visible.find((row) => row.key === hoveredKey) ?? null;

  return (
    <section className="card-surface box-border min-w-0 p-5 min-[1600px]:[grid-column:1/span_3]" aria-label="Besparing från sol och batteri">
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
              className="rounded-lg px-2.5 py-1 text-[11px] font-semibold"
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

      <div className="mt-4 overflow-x-auto pb-1">
        <svg viewBox={`0 0 ${chartWidth} ${chartHeight + 30}`} width="100%" height={chartHeight + 30} role="img" aria-label="Faktisk kostnad och motkostnad över tid">
          <line x1="0" y1={zeroY} x2={chartWidth} y2={zeroY} stroke="var(--divider)" strokeWidth="1" />
          {visible.map((row, index) => {
            const center = index * groupWidth + groupWidth / 2;
            const actualHeight = (Math.abs(row.actualCostKr) / costRange) * chartHeight;
            const baselineHeight = (Math.max(0, row.baselineCostKr) / costRange) * chartHeight;
            const actualY = row.actualCostKr >= 0 ? zeroY - actualHeight : zeroY;
            const baselineY = zeroY - baselineHeight;
            return (
              <g
                key={row.key}
                onMouseEnter={() => setHoveredKey(row.key)}
                onMouseLeave={() => setHoveredKey(null)}
                style={{ cursor: 'default' }}
              >
                <rect x={center - barWidth - 2} y={actualY} width={barWidth} height={actualHeight} rx="2" fill="var(--econ-earned)" opacity="0.9" />
                <rect x={center + 2} y={baselineY} width={barWidth} height={baselineHeight} rx="2" fill="var(--text-muted)" opacity="0.35" />
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
                  <text x={center} y={chartHeight + 16} textAnchor="middle" fontSize="9" fill="var(--text-muted)">{row.label}</text>
                )}
              </g>
            );
          })}
          {hovered && (() => {
            const index = visible.findIndex((row) => row.key === hovered.key);
            const center = index * groupWidth + groupWidth / 2;
            const tooltipWidth = 164;
            const tooltipX = Math.max(4, Math.min(chartWidth - tooltipWidth - 4, center - tooltipWidth / 2));
            return (
              <g pointerEvents="none">
                <rect x={tooltipX} y={5} width={tooltipWidth} height={58} rx="7" fill="var(--node-bg)" stroke="var(--card-border)" />
                <text x={tooltipX + 9} y={19} fontSize="10" fontWeight="700" fill="var(--text)">{hovered.label}</text>
                <text x={tooltipX + 9} y={33} fontSize="9" fill="var(--text-muted)">Faktiskt: {kr(hovered.actualCostKr)}</text>
                <text x={tooltipX + 9} y={45} fontSize="9" fill="var(--text-muted)">Utan sol/batteri: {kr(hovered.baselineCostKr)}</text>
                <text x={tooltipX + 9} y={57} fontSize="9" fontWeight="700" fill="var(--econ-green)">Skillnad: {kr(hovered.savingsKr)}</text>
              </g>
            );
          })()}
        </svg>
      </div>
      <div className="flex flex-wrap items-center gap-3 text-[10px] font-semibold" style={{ color: 'var(--text-muted)' }}>
        <span><i className="mr-1 inline-block h-2 w-2 rounded-sm" style={{ background: 'var(--econ-earned)' }} />Faktisk nettokostnad</span>
        <span><i className="mr-1 inline-block h-2 w-2 rounded-sm" style={{ background: 'var(--text-muted)', opacity: 0.45 }} />Utan sol + batteri</span>
        {latest && <span className="ml-auto">Senaste period: {latest.label} · {kr(latest.savingsKr)} sparat</span>}
      </div>
    </section>
  );
}
