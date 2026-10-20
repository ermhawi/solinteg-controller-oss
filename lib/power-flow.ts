// The sign conventions are those from InverterLiveData (not the optimizer).
// Ignore near-zero measurements, matching the detailed PowerFlowCard diagram.
export const FLOW_THRESHOLD_W = 20;

export type FlowSource = 'solar' | 'house' | 'battery' | 'grid';

export interface PowerFlowDirection {
  from: 'Sol' | 'System' | 'Batteri' | 'Nät';
  to: 'System' | 'Hus' | 'Batteri' | 'Nät';
  state: string;
}

export function powerFlowDirection(source: FlowSource, watts: number): PowerFlowDirection | null {
  if (!Number.isFinite(watts)) return null;

  switch (source) {
    case 'solar':
      return watts > FLOW_THRESHOLD_W ? { from: 'Sol', to: 'System', state: 'Produktion' } : null;
    case 'house':
      return watts > FLOW_THRESHOLD_W ? { from: 'System', to: 'Hus', state: 'Förbrukning' } : null;
    case 'battery':
      if (watts < -FLOW_THRESHOLD_W) return { from: 'System', to: 'Batteri', state: 'Laddar' };
      if (watts > FLOW_THRESHOLD_W) return { from: 'Batteri', to: 'System', state: 'Urladdar' };
      return null;
    case 'grid':
      if (watts < -FLOW_THRESHOLD_W) return { from: 'Nät', to: 'System', state: 'Import' };
      if (watts > FLOW_THRESHOLD_W) return { from: 'System', to: 'Nät', state: 'Export' };
      return null;
  }
}
