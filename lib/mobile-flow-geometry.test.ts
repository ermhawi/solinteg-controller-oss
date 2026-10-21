import { describe, expect, it } from 'vitest';
import { mobileFlowLayout, mobileFlowSegments, MOBILE_COL_GAP, MOBILE_FLOW_HEIGHT } from './mobile-flow-geometry';

describe('mobileFlowLayout', () => {
  it.each([
    [258, 96, 18, 12],
    [298, 90, 30, 18],
    [328, 82, 46, 26],
    [368, 82, 46, 34],
    [600, 82, 46, 34],
  ])('fits the text and frees connector space at %i px', (width, tileHeight, rowGap, inset) => {
    const layout = mobileFlowLayout(width);
    expect(layout).toMatchObject({ tileHeight, rowGap, inset });
    expect(tileHeight * 2 + rowGap).toBe(MOBILE_FLOW_HEIGHT);
    expect(layout.cardWidth).toBeLessThan((width - MOBILE_COL_GAP) / 2 - 1);
    expect(layout.cardWidth).toBeLessThanOrEqual(160);
  });

  it.each([258, 298, 328, 368, 600])('anchors three direct links to the actual card edges at %i px', (width) => {
    const { tileHeight, rowGap, cardWidth } = mobileFlowLayout(width);
    const links = mobileFlowSegments(width);
    const [solar, battery, grid] = links;
    const bottom = tileHeight + rowGap;
    const solarLeft = width - cardWidth;

    expect(links.map(link => link.id)).toEqual(['solar-house', 'house-battery', 'house-grid']);

    expect(solar.from).toEqual([solarLeft, tileHeight / 2]);
    expect(solar.to).toEqual([cardWidth, tileHeight / 2]);
    for (const p of solar.controls) expect(p[1]).toBe(tileHeight / 2);

    const batteryX = cardWidth / 2;
    expect(battery.from).toEqual([batteryX, tileHeight]);
    expect(battery.to).toEqual([batteryX, bottom]);
    for (const p of battery.controls) expect(p[0]).toBe(batteryX);

    expect(grid.from).toEqual([cardWidth - 7, tileHeight]);
    expect(grid.to).toEqual([solarLeft + 7, bottom]);

    expect(new Set(links.flatMap(link => [link.from.join(','), link.to.join(',')])).size).toBe(6);
    expect(Math.hypot(grid.to[0] - grid.from[0], grid.to[1] - grid.from[1])).toBeLessThan(width);
    expect(solarLeft - cardWidth).toBeGreaterThan(MOBILE_COL_GAP);
  });
});
