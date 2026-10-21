import { describe, expect, it } from 'vitest';
import { mobileFlowSegments, MOBILE_COL_GAP, MOBILE_FLOW_HEIGHT, MOBILE_ROW_GAP, MOBILE_TILE_HEIGHT } from './mobile-flow-geometry';

describe('mobileFlowSegments', () => {
  it('uses the original compact mobile row gap', () => {
    expect(MOBILE_ROW_GAP).toBe(10);
    expect(MOBILE_FLOW_HEIGHT).toBe(MOBILE_TILE_HEIGHT * 2 + 10);
  });

  it.each([258, 298, 328, 368, 600])('connects the four cards directly at %i px without entering the cards', (width) => {
    const links = mobileFlowSegments(width);
    expect(links.map((link) => link.id)).toEqual(['solar-house', 'house-battery', 'house-grid']);

    const leftWidth = (width - MOBILE_COL_GAP) / 2;
    const rightStart = leftWidth + MOBILE_COL_GAP;
    const top = MOBILE_TILE_HEIGHT;
    const bottom = MOBILE_TILE_HEIGHT + MOBILE_ROW_GAP;

    // Sol -> Hus stays within the 24px central column gutter.
    expect(links[0].from).toEqual([rightStart, top * 0.7]);
    expect(links[0].to).toEqual([leftWidth, top * 0.7]);
    for (const p of links[0].controls) {
      expect(p[0]).toBeGreaterThan(leftWidth);
      expect(p[0]).toBeLessThan(rightStart);
    }

    // Battery and grid connections span sideways across the compact row gap.
    for (const link of links.slice(1)) {
      expect(link.from[1]).toBe(top);
      expect(link.to[1]).toBe(bottom);
      expect(link.from[0]).toBeGreaterThan(0);
      expect(link.from[0]).toBeLessThan(leftWidth);
      for (const p of link.controls) {
        expect(p[1]).toBeGreaterThan(top);
        expect(p[1]).toBeLessThan(bottom);
      }
    }
    expect(links[1].to[0]).toBeLessThan(leftWidth); // Batteri
    expect(links[2].to[0]).toBeGreaterThan(rightStart); // Nät

    // Endpoints are on cards, never at an invisible shared junction.
    expect(new Set(links.flatMap((link) => [link.from.join(','), link.to.join(',')])).size).toBe(6);
  });
});
