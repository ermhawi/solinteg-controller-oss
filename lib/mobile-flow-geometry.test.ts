import { describe, expect, it } from 'vitest';
import { mobileFlowSegments, MOBILE_COL_GAP, MOBILE_FLOW_HEIGHT, MOBILE_TILE_HEIGHT, MOBILE_ROW_GAP } from './mobile-flow-geometry';

describe('mobileFlowSegments', () => {
  it.each([258, 298, 328, 368, 600])('connects cards directly at %i px', (width) => {
    const links = mobileFlowSegments(width);
    expect(links.map((link) => link.id)).toEqual(['solar-house', 'house-battery', 'house-grid']);
    const leftWidth = (width - MOBILE_COL_GAP) / 2;
    const rightStart = leftWidth + MOBILE_COL_GAP;
    const bottom = MOBILE_TILE_HEIGHT + MOBILE_ROW_GAP;

    const belongsTo = (p: [number, number], column: 'left' | 'right', row: 'top' | 'bottom') =>
      p[0] > (column === 'left' ? 0 : rightStart) &&
      p[0] < (column === 'left' ? leftWidth : width) &&
      p[1] === (row === 'top' ? MOBILE_TILE_HEIGHT : bottom);

    expect(belongsTo(links[0].from, 'right', 'top')).toBe(true); // Sol
    expect(belongsTo(links[0].to, 'left', 'top')).toBe(true); // Hus
    expect(belongsTo(links[1].from, 'left', 'top')).toBe(true); // Hus
    expect(belongsTo(links[1].to, 'left', 'bottom')).toBe(true); // Batteri
    expect(belongsTo(links[2].from, 'left', 'top')).toBe(true); // Hus
    expect(belongsTo(links[2].to, 'right', 'bottom')).toBe(true); // Nät

    // No edge ends in a common junction; all six endpoints are panel anchors.
    expect(new Set(links.flatMap((link) => [link.from.join(','), link.to.join(',')])).size).toBe(6);
    for (const link of links) {
      expect(link.controls[0]).not.toEqual(link.from);
      expect(link.controls[1]).not.toEqual(link.to);
    }
    expect(MOBILE_FLOW_HEIGHT).toBe(bottom + MOBILE_TILE_HEIGHT);
  });
});
