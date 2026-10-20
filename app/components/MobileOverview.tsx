'use client';

import type { InverterLiveData } from '@/lib/inverter';
import type { InverterApiStatus } from '@/lib/inverter-api-status';
import { powerFlowDirection } from '@/lib/power-flow';
import type { PowerFlowDirection } from '@/lib/power-flow';

function power(watts: number) {
  return `${(Math.abs(watts) / 1000).toFixed(2)} kW`;
}

// The four lines meet in the gap between the cards. Arrowheads indicate whether
// electricity is moving towards the system or from it to that particular card.
function FlowNetwork({ flows }: { flows: {
  house: PowerFlowDirection | null;
  solar: PowerFlowDirection | null;
  battery: PowerFlowDirection | null;
  grid: PowerFlowDirection | null;
} }) {
  const hub = 56;
  const corners = [
    { flow: flows.house, x: 34, y: 34, color: 'var(--flow-house-accent)' },
    { flow: flows.solar, x: 78, y: 34, color: 'var(--flow-solar)' },
    { flow: flows.battery, x: 34, y: 78, color: 'var(--flow-battery)' },
    { flow: flows.grid, x: 78, y: 78, color: flows.grid?.state === 'Import' ? 'var(--flow-red)' : 'var(--flow-sky)' },
  ];
  const activeFlows = corners.flatMap(({ flow }) => flow ? [`${flow.from} till ${flow.to}`] : []);

  return (
    <svg viewBox="0 0 112 112" role="img"
      aria-label={activeFlows.length ? `Elflöden: ${activeFlows.join(', ')}` : 'Inga aktiva elflöden mellan energipanelerna'}
      className="pointer-events-none absolute left-1/2 top-1/2 z-10 h-28 w-28 -translate-x-1/2 -translate-y-1/2 overflow-visible">
      {corners.map(({ flow, x, y, color }, index) => {
        const dx = x - hub;
        const dy = y - hub;
        // The line ends before the center circle, and touches the rounded inner corners.
        const inner: [number, number] = [hub + dx * 0.3, hub + dy * 0.3];
        const outer: [number, number] = [hub + dx * 0.98, hub + dy * 0.98];
        const inward = flow?.to === 'System';
        const [from, to] = inward ? [outer, inner] : [inner, outer];
        const deltaX = to[0] - from[0];
        const deltaY = to[1] - from[1];
        const length = Math.hypot(deltaX, deltaY);
        const ux = deltaX / length;
        const uy = deltaY / length;
        const sideX = -uy;
        const sideY = ux;
        const arrow = [
          to,
          [to[0] - ux * 6 + sideX * 3.4, to[1] - uy * 6 + sideY * 3.4],
          [to[0] - ux * 6 - sideX * 3.4, to[1] - uy * 6 - sideY * 3.4],
        ].map(([a, b]) => `${a},${b}`).join(' ');

        return (
          <g key={index}>
            <line x1={inner[0]} y1={inner[1]} x2={outer[0]} y2={outer[1]}
              stroke="var(--node-idle-stroke)" strokeWidth="2.5" strokeLinecap="round" />
            {flow && (
              <g>
                <line x1={from[0]} y1={from[1]} x2={to[0]} y2={to[1]}
                  stroke={color} strokeWidth="4" strokeLinecap="round"
                  strokeDasharray="5 4" className="mobile-interpanel-flow" />
                <polygon points={arrow} fill={color} />
                <title>{`${flow.from} → ${flow.to}`}</title>
              </g>
            )}
          </g>
        );
      })}
      <circle cx={hub} cy={hub} r="9" fill="var(--card-bg)" stroke="var(--divider)" strokeWidth="1.5" />
      <circle cx={hub} cy={hub} r="3.5" fill={activeFlows.length ? 'var(--flow-house-accent)' : 'var(--node-idle-stroke)'} />
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
    <div className="relative z-0 min-w-0 rounded-2xl px-2.5 py-2.5 min-[360px]:px-3"
      style={{ background: 'var(--badge-bg)' }}>
      <p className="text-[13px] font-semibold" style={{ color: 'var(--text-muted)' }}>{label}</p>
      <p className="mt-0.5 text-[18px] font-bold leading-tight tabular-nums min-[360px]:text-[20px]"
        style={{ color: color ?? 'var(--text)', fontFamily: 'var(--font-heading)' }}>{value}</p>
      <p className="mt-0.5 text-[12px] font-medium" style={{ color: 'var(--text-muted)' }}>{detail}</p>
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
      <div className="relative grid grid-cols-2 gap-10">
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
        <FlowNetwork flows={{ house: houseFlow, solar: solarFlow, battery: batteryFlow, grid: gridFlow }} />
      </div>
    </section>
  );
}
