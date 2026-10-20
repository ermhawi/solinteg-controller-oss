'use client';

import type { InverterLiveData } from '@/lib/inverter';
import type { InverterApiStatus } from '@/lib/inverter-api-status';

function power(watts: number) {
  return `${(Math.abs(watts) / 1000).toFixed(2)} kW`;
}

function Metric({ label, value, detail, color }: { label: string; value: string; detail?: string; color?: string }) {
  return (
    <div className="min-w-0 rounded-2xl px-3 py-2" style={{ background: 'var(--badge-bg)' }}>
      <p className="text-[13px] font-semibold" style={{ color: 'var(--text-muted)' }}>{label}</p>
      <p className="mt-0.5 text-[20px] font-bold leading-tight tabular-nums" style={{ color: color ?? 'var(--text)', fontFamily: 'var(--font-heading)' }}>{value}</p>
      <p className="mt-0.5 text-[12px] font-medium" style={{ color: 'var(--text-muted)' }}>{detail ?? '\u00a0'}</p>
    </div>
  );
}

export default function MobileOverview({ data, status }: { data: InverterLiveData | null; status: InverterApiStatus }) {
  const gridDetail = data ? (data.grid_w < -20 ? 'Import från nätet' : data.grid_w > 20 ? 'Export till nätet' : 'I balans') : 'Ingen aktuell data';
  const batteryDetail = data ? (data.battery_w < -20 ? 'Laddar' : data.battery_w > 20 ? 'Urladdar' : 'Vilar') : 'Ingen aktuell data';

  return (
    <section className="card-surface order-0 min-w-0 p-3.5 min-[700px]:hidden" aria-label="Energiöversikt">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h2 className="text-[17px] font-bold" style={{ fontFamily: 'var(--font-heading)' }}>Energi just nu</h2>
        <span className="text-[12px] font-semibold" style={{ color: status === 'live' ? 'var(--live-dot)' : 'var(--color-warning)' }}>
          {status === 'live' ? '● Live' : '● Ingen live-data'}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-2.5">
        <Metric label="Huset" value={data ? power(data.house_load_w) : '–'} detail="Förbrukning" />
        <Metric label="Sol" value={data ? power(data.pv_w) : '–'} detail="Produktion" color="var(--flow-solar)" />
        <Metric label="Batteri" value={data ? `${data.soc_pct.toFixed(0)} %` : '–'} detail={batteryDetail} color="var(--flow-battery)" />
        <Metric label="Elnät" value={data ? power(data.grid_w) : '–'} detail={gridDetail} />
      </div>
    </section>
  );
}
