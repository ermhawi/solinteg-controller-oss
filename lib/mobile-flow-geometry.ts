// The card rows are fixed height so all connector endpoints sit precisely on
// the card edges, even when the labels wrap on a narrow phone.
export const MOBILE_TILE_HEIGHT = 110;
export const MOBILE_ROW_GAP = 112;
export const MOBILE_COL_GAP = 24;
export const MOBILE_HUB_RADIUS = 10;
export const MOBILE_FLOW_HEIGHT = MOBILE_TILE_HEIGHT * 2 + MOBILE_ROW_GAP;

export type MobileFlowPoint = [number, number];

export function mobileFlowSegments(width: number): { card: MobileFlowPoint; hub: MobileFlowPoint }[] {
  const left = (width - MOBILE_COL_GAP) / 4;
  const right = width - left;
  const centerX = width / 2;
  const centerY = MOBILE_TILE_HEIGHT + MOBILE_ROW_GAP / 2;
  const topHub: MobileFlowPoint = [centerX, centerY - MOBILE_HUB_RADIUS];
  const bottomHub: MobileFlowPoint = [centerX, centerY + MOBILE_HUB_RADIUS];

  return [
    { card: [left, MOBILE_TILE_HEIGHT], hub: topHub },        // House
    { card: [right, MOBILE_TILE_HEIGHT], hub: topHub },       // Solar
    { card: [left, MOBILE_TILE_HEIGHT + MOBILE_ROW_GAP], hub: bottomHub }, // Battery
    { card: [right, MOBILE_TILE_HEIGHT + MOBILE_ROW_GAP], hub: bottomHub }, // Grid
  ];
}
