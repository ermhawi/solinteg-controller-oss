'use client';

import type { InverterLiveData } from '@/lib/inverter';
import type { InverterApiStatus } from '@/lib/inverter-api-status';
import { powerFlowDirection } from '@/lib/power-flow';
import type { PowerFlowDirection } from '@/lib/power-flow';

function power(watts: number) {
  return `${(Math.abs(watts) / 1000).toFixed(2)} kW`;
}

function FlowArrow({ flow, color }: { flow: PowerFlowDirection | null; color: string }) {
  if (!flow) return null;

  return (
    <div className="mt-1 flex min-w-0 items-center gap-1 text-[11px] font-bold min-[380px]:text-[12px]"
      style={{ color }} aria-label={`Elflöde från ${flow.from} till ${flow.to}`}>
      <span className="shrink-0">{flow.from}</span>
      <svg className="h-3 w-5 shrink-0" viewBox="0 0 24 12" aria-hidden="true">
        <path d="M1 6 H21 M16 1 L21 6 L16 11" fill="none" stroke="currentColor"
          strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
        <circle className="mobile-flow-dot" cx="2" cy="6" r="2" fill="currentColor" />
      </svg>
      <span className="min-w-0">{flow.to}</span>
    </div>
  );
}

function Metric({ label, value, detail, color, flow }: {
  label: string;
  value: string;
  detail: string;
  color?: string;
  flow: PowerFlowDirection | null;
}) {
  return (
    <div className="min-w-0 rounded-2xl px-3 py-2.5" style={{ background: 'var(--badge-bg)' }}>
      <p className="text-[13px] font-semibold" style={{ color: 'var(--text-muted)' }}>{label}</p>
      <p className="mt-0.5 text-[20px] font-bold leading-tight tabular-nums"
        style={{ color: color ?? 'var(--text)', fontFamily: 'var(--font-heading)' }}>{value}</p>
      <p className="mt-0.5 text-[12px] font-medium" style={{ color: 'var(--text-muted)' }}>{detail}</p>
      <FlowArrow flow={flow} color={color ?? 'var(--flow-house-accent)'} />
    </div>
  );
}

export default function MobileOverview({ data, status }: { data: InverterLiveData | null; status: InverterApiStatus }) {
  const solarFlow = data ? powerFlowDirection('solar', data.pv_w) : null;
  const houseFlow = data ? powerFlowDirection('house', data.house_load_w) : null;
  const batteryFlow = data ? powerFlowDirection('battery', data.battery_w) : null;
  const gridFlow = data ? powerFlowDirection('grid', data.grid_w) : null;
  const noData = 'Ingen aktuell data';

  return (
    <section className="card-surface order-0 min-w-0 p-3.5 min-[700px]:hidden" aria-label="Energiöversikt">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h2 className="text-[17px] font-bold" style={{ fontFamily: 'var(--font-heading)' }}>Energi just nu</h2>
        <span className="text-[12px] font-semibold" style={{ color: status === 'live' ? 'var(--live-dot)' : 'var(--color-warning)' }}>
          {status === 'live' ? '● Live' : '● Ingen live-data'}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-2.5">
        <Metric label="Huset" value={data ? power(data.house_load_w) : '–'}
          detail={data ? 'Förbrukning' : noData} flow={houseFlow} />
        <Metric label="Sol" value={data ? power(data.pv_w) : '–'}
          detail={data ? (solarFlow ? 'Produktion' : 'Ingen produktion') : noData}
          color="var(--flow-solar)" flow={solarFlow} />
        <Metric label="Batteri" value={data ? `${data.soc_pct.toFixed(0)} %` : '–'}
          detail={data ? (batteryFlow ? `${batteryFlow.state} ${power(data.battery_w)}` : 'Vilar') : noData}
          color="var(--flow-battery)" flow={batteryFlow} />
        <Metric label="Elnät" value={data ? power(data.grid_w) : '–'}
          detail={data ? (gridFlow?.state ?? 'I balans') : noData}
          color={gridFlow?.state === 'Import' ? 'var(--flow-red)' : 'var(--flow-sky)'} flow={gridFlow} />
      </div>
    </section>
  );
}
