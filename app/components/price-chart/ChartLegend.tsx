'use client';

/**
 * The buy/sell series always stay visible. Additional layers can be hidden when
 * they compete for space, especially on the narrow mobile chart.
 */
export interface ChartLayers {
  solar: boolean;
  soc: boolean;
  zones: boolean;
}

function ToggleChip({
  on,
  onClick,
  children,
}: {
  on: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={on}
      onClick={onClick}
      className="chip flex min-h-11 items-center gap-1.5 rounded-full px-3 py-2 text-[13px] font-semibold min-[900px]:min-h-0 min-[700px]:px-2.5 min-[700px]:py-1"
      style={on ? { border: '1px solid var(--divider)' } : { color: 'var(--text-muted)', opacity: 0.75 }}
    >
      {children}
    </button>
  );
}

function LayerControls({ layers, setLayers, hasPlan }: {
  layers: ChartLayers;
  setLayers: React.Dispatch<React.SetStateAction<ChartLayers>>;
  hasPlan: boolean;
}) {
  return (
    <>
      <ToggleChip on={layers.solar} onClick={() => setLayers((s) => ({ ...s, solar: !s.solar }))}>
        <span className="h-[11px] w-[13px] rounded-sm" style={{ background: 'var(--color-solar)' }} />
        Sol
      </ToggleChip>
      {hasPlan && (
        <ToggleChip on={layers.soc} onClick={() => setLayers((s) => ({ ...s, soc: !s.soc }))}>
          <span className="w-[18px] border-t-2" style={{ borderColor: 'var(--color-soc)', borderStyle: 'dashed' }} />
          Batteri-SoC
        </ToggleChip>
      )}
      {hasPlan && (
        <ToggleChip on={layers.zones} onClick={() => setLayers((s) => ({ ...s, zones: !s.zones }))}>
          <span className="h-[11px] w-[9px] rounded-sm" style={{ background: 'var(--color-charge-band)' }} />
          Ladda / Sälj / Sparar
        </ToggleChip>
      )}
    </>
  );
}

export default function ChartLegend({
  layers,
  setLayers,
  hasPlan,
  hasMixedPriceSources,
}: {
  layers: ChartLayers;
  setLayers: React.Dispatch<React.SetStateAction<ChartLayers>>;
  hasPlan: boolean;
  hasMixedPriceSources: boolean;
}) {
  return (
    <div className="mb-2">
      <div className="flex min-h-9 flex-wrap items-center gap-x-3 gap-y-1.5 text-[13px] font-semibold">
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-[3px] w-[18px] rounded" style={{ background: 'var(--color-buy)' }} />
          Köp
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-[3px] w-[18px] rounded" style={{ background: 'var(--color-sell)' }} />
          Sälj
        </span>
        {hasMixedPriceSources && (
          <span className="hidden items-center gap-1.5 min-[700px]:flex" style={{ color: 'var(--text-muted)' }}>
            <span className="h-3 w-[18px] rounded-sm" style={{ background: 'color-mix(in srgb, var(--color-now) 18%, transparent)' }} />
            Prognos
          </span>
        )}
        <div className="hidden flex-wrap items-center gap-2 min-[700px]:flex">
          <span className="text-xs font-bold uppercase" style={{ color: 'var(--text-muted)' }}>Lager</span>
          <LayerControls layers={layers} setLayers={setLayers} hasPlan={hasPlan} />
        </div>
      </div>

      <details className="mt-1 min-[700px]:hidden">
        <summary className="chip flex min-h-11 cursor-pointer items-center rounded-xl px-3 text-[13px] font-semibold" style={{ background: 'var(--badge-bg)' }}>
          Visning och lager
          <span className="ml-2 text-xs" style={{ color: 'var(--text-muted)' }}>· Anpassa grafen</span>
        </summary>
        <div className="mt-2 flex flex-wrap gap-2">
          <LayerControls layers={layers} setLayers={setLayers} hasPlan={hasPlan} />
          {hasMixedPriceSources && (
            <span className="flex items-center text-xs" style={{ color: 'var(--text-muted)' }}>
              Gul bakgrund = prisprognos
            </span>
          )}
        </div>
      </details>
    </div>
  );
}
