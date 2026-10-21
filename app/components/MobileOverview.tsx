'use client';

import { useRef } from 'react';
import type { InverterLiveData } from '@/lib/inverter';
import type { InverterApiStatus } from '@/lib/inverter-api-status';
import { powerFlowDirection } from '@/lib/power-flow';
import type { PowerFlowDirection } from '@/lib/power-flow';
import { MOBILE_FLOW_HEIGHT, mobileFlowSegments } from '@/lib/mobile-flow-geometry';
import FlowConnector from './FlowConnector';
import { useContainerWidth } from './useResponsiveLayout';

function power(watts: number) {
  return `${(Math.abs(watts) / 1000).toFixed(2)} kW`;
}

// The three direct curved connections match Systemstatus: solar → house,
// house ↔ battery and house ↔ grid. No shared center or implicit power allocation.
function FlowNetwork({ width, flows }: { width: number; flows: {
  solar: PowerFlowDirection | null;
  battery: PowerFlowDirection | null;
  grid: PowerFlowDirection | null;
} }) {
  const branches = mobileFlowSegments(width);
  const activeFlows = [
    flows.solar && 'Sol till Hus',
    flows.battery && (flows.battery.from === 'Batteri' ? 'Batteri till Hus' : 'Hus till Batteri'),
    flows.grid && (flows.grid.from === 'Nät' ? 'Nät till Hus' : 'Hus till Nät'),
  ].filter((flow): flow is string => !!flow);

  return (
    <svg viewBox={`0 0 ${width} ${MOBILE_FLOW_HEIGHT}`} role="img"
      aria-label={activeFlows.length ? `Elflöden: ${activeFlows.join(', ')}` : 'Inga aktiva elflöden mellan energipanelerna'}
      className="pointer-events-none absolute inset-0 z-10 h-full w-full overflow-visible">
      {branches.map(({ id, from, to, controls }) => {
        const flow = id === 'solar-house' ? flows.solar : id === 'house-battery' ? flows.battery : flows.grid;
        const reverse = id === 'house-battery'
          ? flow?.from === 'Batteri'
          : id === 'house-grid' && flow?.from === 'Nät';
        const color = id === 'solar-house' ? 'var(--flow-solar)'
          : id === 'house-battery' ? 'var(--flow-battery)'
            : flow?.from === 'Nät' ? 'var(--flow-red)' : 'var(--flow-sky)';
        return (
          <FlowConnector key={id}
            from={reverse ? to : from}
            to={reverse ? from : to}
            controls={reverse ? [controls[1], controls[0]] : controls}
            color={color}
            active={!!flow} />
        );
      })}
    </svg>
  );
}

function Metric({ label, value, detail, color }: {
  label: string;
  value: string;
  detail: string;
  color?: string;
}) {
  return (
    <div className="relative z-0 h-full min-w-0 rounded-2xl px-2.5 py-2.5 min-[360px]:px-3"
      style={{ background: 'var(--badge-bg)', border: '1px solid var(--divider)' }}>
      <p className="text-[13px] font-semibold" style={{ color: 'var(--text-muted)' }}>{label}</p>
      <p className="mt-0.5 text-[18px] font-bold leading-tight tabular-nums min-[360px]:text-[20px]"
        style={{ color: color ?? 'var(--text)', fontFamily: 'var(--font-heading)' }}>{value}</p>
      <p className="mt-0.5 text-[12px] font-medium" style={{ color: 'var(--text-muted)' }}>{detail}</p>
    </div>
  );
}

export default function MobileOverview({ data, status }: { data: InverterLiveData | null; status: InverterApiStatus }) {
  const gridRef = useRef<HTMLDivElement>(null);
  const width = useContainerWidth(gridRef);
  const solarFlow = data ? powerFlowDirection('solar', data.pv_w) : null;
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
      <div ref={gridRef} className="relative grid grid-cols-2 grid-rows-[110px_110px] gap-x-6 gap-y-2.5">
        <Metric label="Huset" value={data ? power(data.house_load_w) : '–'}
          detail={data ? 'Förbrukning' : noData} />
        <Metric label="Sol" value={data ? power(data.pv_w) : '–'}
          detail={data ? (solarFlow ? 'Produktion' : 'Ingen produktion') : noData}
          color="var(--flow-solar)" />
        <Metric label="Batteri" value={data ? `${data.soc_pct.toFixed(0)} %` : '–'}
          detail={data ? (batteryFlow ? `${batteryFlow.state} ${power(data.battery_w)}` : 'Vilar') : noData}
          color="var(--flow-battery)" />
        <Metric label="Elnät" value={data ? power(data.grid_w) : '–'}
          detail={data ? (gridFlow?.state ?? 'I balans') : noData}
          color={gridFlow?.state === 'Import' ? 'var(--flow-red)' : 'var(--flow-sky)'} />
        <FlowNetwork width={width ?? 258} flows={{ solar: solarFlow, battery: batteryFlow, grid: gridFlow }} />
      </div>
    </section>
  );
}
