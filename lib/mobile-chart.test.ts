import { describe, expect, it } from 'vitest';
import { mobileChartWindow } from './mobile-chart';

describe('mobileChartWindow', () => {
  it('shows the next 10 hours plus 2 hours of context around now', () => {
    expect(mobileChartWindow(96, 192)).toEqual({ start: 88, end: 135 });
  });
  it('clamps both ends of the available horizon', () => {
    expect(mobileChartWindow(2, 192)).toEqual({ start: 0, end: 47 });
    expect(mobileChartWindow(188, 192)).toEqual({ start: 144, end: 191 });
  });
  it('handles fewer slots than the window', () => {
    expect(mobileChartWindow(3, 12)).toEqual({ start: 0, end: 11 });
    expect(mobileChartWindow(0, 0)).toEqual({ start: 0, end: 0 });
  });
});
