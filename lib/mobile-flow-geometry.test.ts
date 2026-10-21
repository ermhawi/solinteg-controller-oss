import { describe, expect, it } from 'vitest';
import { mobileFlowSegments, MOBILE_COL_GAP, MOBILE_FLOW_HEIGHT, MOBILE_ROW_GAP, MOBILE_TILE_HEIGHT } from './mobile-flow-geometry';

describe('mobileFlowSegments', () => {
  it('keeps the original compact row gap', () => {
    expect(MOBILE_ROW_GAP).toBe(10);
    expect(MOBILE_FLOW_HEIGHT).toBe(MOBILE_TILE_HEIGHT * 2 + 10);
  });

  it.each([258, 298, 328, 368, 600])('renders separate direct connections between card edges at %i px', (width) => {
    const links = mobileFlowSegments(width);
    expect(links.map((link) => link.id)).toEqual(['solar-house', 'house-battery', 'house-grid']);
    const leftWidth = (width - MOBILE_COL_GAP) / 2;
    const rightStart = leftWidth + MOBILE_COL_GAP;
    const top = MOBILE_TILE_HEIGHT;
    const bottom = MOBILE_TILE_HEIGHT + MOBILE_ROW_GAP;
    const [solar, battery, grid] = links;

    // Solar -> house is confined to the vertical gutter between the upper cards.
    expect(solar.from[0]).toBe(rightStart);
    expect(solar.to[0]).toBe(leftWidth);
    expect(solar.from[1]).toBeGreaterThan(0);
    expect(solar.to[1]).toBeLessThan(top);
    for (const p of solar.controls) {
      expect(p[0]).toBeGreaterThan(leftWidth);
      expect(p[0]).toBeLessThan(rightStart);
      expect(p[1]).toBeLessThan(top);
    }

    // Battery connection remains entirely within the left column's row gap.
    expect(battery.from[1]).toBe(top);
    expect(battery.to[1]).toBe(bottom);
    for (const p of [battery.from, battery.to, ...battery.controls]) {
      expect(p[0]).toBeGreaterThan(0);
      expect(p[0]).toBeLessThan(leftWidth);
    }

    // Grid connection follows its own corridor towards the bottom-right card.
    expect(grid.from[1]).toBe(top);
    expect(grid.to[1]).toBe(bottom);
    expect(grid.from[0]).toBeLessThan(leftWidth);
    expect(grid.to[0]).toBeGreaterThan(rightStart);

    // Battery and grid paths must not meet or cross in the narrow row gap.
    const batteryRight = Math.max(...[battery.from, battery.to, ...battery.controls].map(p => p[0]));
    const gridLeft = Math.min(...[grid.from, grid.to, ...grid.controls].map(p => p[0]));
    expect(gridLeft - batteryRight).toBeGreaterThan(10);

    for (const link of [battery, grid]) {
      for (const p of link.controls) {
        expect(p[1]).toBeGreaterThan(top);
        expect(p[1]).toBeLessThan(bottom);
      }
    }
    // Six unique endpoints, each on a card boundary; no shared midpoint.
    expect(new Set(links.flatMap(link => [link.from.join(','), link.to.join(',')])).size).toBe(6);
  });
});
