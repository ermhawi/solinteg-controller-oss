// Three direct panel-to-panel links, as in Systemstatus.
// Keep the overall footprint compact while making the cards fit their content
// and using the freed space for longer connectors.
export const MOBILE_COL_GAP = 24;
export const MOBILE_FLOW_HEIGHT = 210;
export const MOBILE_CARD_MAX_WIDTH = 160;

export type MobileFlowPoint = [number, number];
export type MobileFlowLinkId = 'solar-house' | 'house-battery' | 'house-grid';

export interface MobileFlowLink {
  id: MobileFlowLinkId;
  from: MobileFlowPoint;
  to: MobileFlowPoint;
  controls: [MobileFlowPoint, MobileFlowPoint];
}

export function mobileFlowLayout(width: number) {
  // Narrow phones need room for wrapped battery status without smaller text.
  const tileHeight = width < 298 ? 96 : width < 322 ? 90 : 82;
  const inset = width < 298 ? 12 : width < 322 ? 18 : width < 368 ? 26 : 34;
  const cardWidth = Math.min((width - MOBILE_COL_GAP) / 2 - inset, MOBILE_CARD_MAX_WIDTH);
  return { tileHeight, rowGap: MOBILE_FLOW_HEIGHT - tileHeight * 2, inset, cardWidth };
}

export function mobileFlowSegments(width: number): MobileFlowLink[] {
  const { tileHeight, rowGap, cardWidth } = mobileFlowLayout(width);
  const houseRight = cardWidth;
  const solarLeft = width - cardWidth;
  const bottom = tileHeight + rowGap;
  const midGap = (tileHeight + bottom) / 2;
  const batteryX = cardWidth / 2;
  const gridFrom = houseRight - 7;
  const gridTo = solarLeft + 7;

  return [
    {
      id: 'solar-house',
      from: [solarLeft, tileHeight / 2], to: [houseRight, tileHeight / 2],
      controls: [[solarLeft - 10, tileHeight / 2], [houseRight + 10, tileHeight / 2]],
    },
    {
      id: 'house-battery',
      from: [batteryX, tileHeight], to: [batteryX, bottom],
      controls: [[batteryX, midGap - rowGap * 0.22], [batteryX, midGap + rowGap * 0.22]],
    },
    {
      id: 'house-grid',
      from: [gridFrom, tileHeight], to: [gridTo, bottom],
      controls: [[houseRight + 13, midGap - rowGap * 0.22], [solarLeft - 13, midGap + rowGap * 0.22]],
    },
  ];
}
