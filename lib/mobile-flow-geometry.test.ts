import { describe, expect, it } from 'vitest';
import { mobileFlowSegments, MOBILE_CARD_INSET, MOBILE_COL_GAP, MOBILE_FLOW_HEIGHT, MOBILE_ROW_GAP, MOBILE_TILE_HEIGHT } from './mobile-flow-geometry';

describe('mobileFlowSegments', () => {
  it('keeps a 10px row gap while reducing the panel height', () => {
    expect(MOBILE_TILE_HEIGHT).toBe(100);
    expect(MOBILE_ROW_GAP).toBe(10);
    expect(MOBILE_FLOW_HEIGHT).toBe(210);
    expect(MOBILE_CARD_INSET).toBe(10);
  });

  it.each([258, 298, 328, 368, 600])('uses facing panel edges at %i px without a junction', (width) => {
    const links = mobileFlowSegments(width);
    const leftWidth = (width - MOBILE_COL_GAP) / 2;
    const rightStart = leftWidth + MOBILE_COL_GAP;
    const houseRight = leftWidth - MOBILE_CARD_INSET;
    const solarLeft = rightStart + MOBILE_CARD_INSET;
    const bottom = MOBILE_TILE_HEIGHT + MOBILE_ROW_GAP;

    expect(links.map((link) => link.id)).toEqual(['solar-house', 'house-battery', 'house-grid']);
    const [solar, battery, grid] = links;

    // Horizontal: center of Sol's left side to center of Hus's right side.
    expect(solar.from).toEqual([solarLeft, MOBILE_TILE_HEIGHT / 2]);
    expect(solar.to).toEqual([houseRight, MOBILE_TILE_HEIGHT / 2]);
    for (const control of solar.controls) expect(control[1]).toBe(MOBILE_TILE_HEIGHT / 2);

    // Vertical: center of Hus's bottom side to center of Batteri's top side.
    const batteryCenter = houseRight / 2;
    expect(battery.from).toEqual([batteryCenter, MOBILE_TILE_HEIGHT]);
    expect(battery.to).toEqual([batteryCenter, bottom]);
    for (const control of battery.controls) expect(control[0]).toBe(batteryCenter);

    // Grid stays a short separate diagonal between facing card edges.
    expect(grid.from).toEqual([houseRight - 7, MOBILE_TILE_HEIGHT]);
    expect(grid.to).toEqual([solarLeft + 7, bottom]);

    // No common midpoint or shared endpoint; all endpoints touch panel edges.
    expect(new Set(links.flatMap((link) => [link.from.join(','), link.to.join(',')])).size).toBe(6);
    expect(solarLeft - houseRight).toBe(MOBILE_COL_GAP + 2 * MOBILE_CARD_INSET);
    expect(Math.hypot(grid.to[0] - grid.from[0], grid.to[1] - grid.from[1])).toBeLessThan(65);
  });
});
