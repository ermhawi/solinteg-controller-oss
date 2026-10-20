import { describe, expect, it } from 'vitest';
import { FLOW_THRESHOLD_W, powerFlowDirection } from './power-flow';

describe('powerFlowDirection', () => {
  it('shows solar production into the system and house consumption out', () => {
    expect(powerFlowDirection('solar', 200)).toEqual({ from: 'Sol', to: 'System', state: 'Produktion' });
    expect(powerFlowDirection('house', 1050)).toEqual({ from: 'System', to: 'Hus', state: 'Förbrukning' });
  });

  it('uses inverter signs for grid import/export', () => {
    expect(powerFlowDirection('grid', -1330)).toEqual({ from: 'Nät', to: 'System', state: 'Import' });
    expect(powerFlowDirection('grid', 1330)).toEqual({ from: 'System', to: 'Nät', state: 'Export' });
  });

  it('uses inverter signs for battery charge/discharge', () => {
    expect(powerFlowDirection('battery', -280)).toEqual({ from: 'System', to: 'Batteri', state: 'Laddar' });
    expect(powerFlowDirection('battery', 280)).toEqual({ from: 'Batteri', to: 'System', state: 'Urladdar' });
  });

  it('never presents arrows for idle or invalid power', () => {
    for (const source of ['solar', 'house', 'battery', 'grid'] as const) {
      for (const watts of [-FLOW_THRESHOLD_W, 0, FLOW_THRESHOLD_W, NaN, Infinity]) {
        expect(powerFlowDirection(source, watts)).toBeNull();
      }
    }
    expect(powerFlowDirection('solar', -200)).toBeNull();
    expect(powerFlowDirection('house', -200)).toBeNull();
  });

  it('shows arrows immediately outside the threshold', () => {
    expect(powerFlowDirection('grid', -(FLOW_THRESHOLD_W + 1))?.from).toBe('Nät');
    expect(powerFlowDirection('battery', FLOW_THRESHOLD_W + 1)?.from).toBe('Batteri');
  });
});
