// Geometry shared by the four rounded cards and the connecting SVG.
// There is no visible node: all curves meet at an invisible electrical junction.
export const MOBILE_TILE_HEIGHT = 110;
export const MOBILE_ROW_GAP = 112;
export const MOBILE_COL_GAP = 24;
export const MOBILE_FLOW_HEIGHT = MOBILE_TILE_HEIGHT * 2 + MOBILE_ROW_GAP;

export type MobileFlowPoint = [number, number];

export interface MobileFlowCurve {
  card: MobileFlowPoint;
  hub: MobileFlowPoint;
  controls: [MobileFlowPoint, MobileFlowPoint]; // from card to invisible junction
}

export function mobileFlowSegments(width: number): MobileFlowCurve[] {
  const left = (width - MOBILE_COL_GAP) / 4;
  const right = width - left;
  const centerX = width / 2;
  const centerY = MOBILE_TILE_HEIGHT + MOBILE_ROW_GAP / 2;
  const hub: MobileFlowPoint = [centerX, centerY];

  return [
    { card: [left, MOBILE_TILE_HEIGHT], hub,
      controls: [[left + 7, 139], [centerX - 34, centerY - 11]] }, // House
    { card: [right, MOBILE_TILE_HEIGHT], hub,
      controls: [[right - 7, 139], [centerX + 34, centerY - 11]] }, // Solar
    { card: [left, MOBILE_TILE_HEIGHT + MOBILE_ROW_GAP], hub,
      controls: [[left + 7, 193], [centerX - 34, centerY + 11]] }, // Battery
    { card: [right, MOBILE_TILE_HEIGHT + MOBILE_ROW_GAP], hub,
      controls: [[right - 7, 193], [centerX + 34, centerY + 11]] }, // Grid
  ];
}
