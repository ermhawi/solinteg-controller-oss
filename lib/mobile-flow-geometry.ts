// Three independent panel-to-panel connections, as in Systemstatus:
// Sol -> Hus, Hus <-> Batteri, Hus <-> Nät.
// Each connector starts/ends on the visible card edge, including the outer
// inset that keeps the compact mobile cards slightly narrower than the grid.
export const MOBILE_TILE_HEIGHT = 100;
export const MOBILE_ROW_GAP = 10;
export const MOBILE_COL_GAP = 24;
export const MOBILE_CARD_INSET = 10;
export const MOBILE_FLOW_HEIGHT = MOBILE_TILE_HEIGHT * 2 + MOBILE_ROW_GAP;

export type MobileFlowPoint = [number, number];
export type MobileFlowLinkId = 'solar-house' | 'house-battery' | 'house-grid';

export interface MobileFlowLink {
  id: MobileFlowLinkId;
  from: MobileFlowPoint;
  to: MobileFlowPoint;
  controls: [MobileFlowPoint, MobileFlowPoint];
}

export function mobileFlowSegments(width: number): MobileFlowLink[] {
  const leftWidth = (width - MOBILE_COL_GAP) / 2;
  const rightStart = leftWidth + MOBILE_COL_GAP;
  const houseRight = leftWidth - MOBILE_CARD_INSET;
  const solarLeft = rightStart + MOBILE_CARD_INSET;
  const top = MOBILE_TILE_HEIGHT;
  const bottom = MOBILE_TILE_HEIGHT + MOBILE_ROW_GAP;
  const midGap = (top + bottom) / 2;

  const batteryX = houseRight / 2;
  const gridFrom = houseRight - 7;
  const gridTo = solarLeft + 7;

  return [
    {
      id: 'solar-house',
      from: [solarLeft, top / 2], to: [houseRight, top / 2],
      controls: [[solarLeft - 10, top / 2], [houseRight + 10, top / 2]],
    },
    {
      id: 'house-battery',
      from: [batteryX, top], to: [batteryX, bottom],
      controls: [[batteryX, midGap - 2], [batteryX, midGap + 2]],
    },
    {
      id: 'house-grid',
      from: [gridFrom, top], to: [gridTo, bottom],
      controls: [[houseRight + 13, midGap - 2], [solarLeft - 13, midGap + 2]],
    },
  ];
}
