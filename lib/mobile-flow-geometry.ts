// Direct connections between the four cards: the same three relationships as
// Systemstatus (Sol -> Hus, Hus <-> Batteri, Hus <-> Nät).
// No shared junction: every curve begins and ends at a card's edge.
export const MOBILE_TILE_HEIGHT = 110;
export const MOBILE_ROW_GAP = 112;
export const MOBILE_COL_GAP = 24;
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
  const top = MOBILE_TILE_HEIGHT;
  const bottom = MOBILE_TILE_HEIGHT + MOBILE_ROW_GAP;

  const batteryX = leftWidth * 0.23;
  const gridX = rightStart + leftWidth * 0.7;

  return [
    {
      id: 'solar-house',
      from: [rightStart + 26, top], to: [leftWidth - 26, top],
      controls: [[rightStart + 21, top + 45], [leftWidth - 21, top + 45]],
    },
    {
      id: 'house-battery',
      from: [batteryX, top], to: [batteryX, bottom],
      controls: [[batteryX + 18, top + 35], [batteryX - 18, bottom - 35]],
    },
    {
      id: 'house-grid',
      from: [leftWidth * 0.68, top], to: [gridX, bottom],
      controls: [[leftWidth * 0.68 + 9, top + 42], [gridX - 32, bottom - 42]],
    },
  ];
}
