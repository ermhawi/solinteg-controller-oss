// Direct connections between the four cards: the same three relationships as
// Systemstatus (Sol -> Hus, Hus <-> Batteri, Hus <-> Nät).
// The compact 10px row gap is the original mobile layout; the long diagonal
// connectors run sideways through it rather than requiring extra vertical space.
export const MOBILE_TILE_HEIGHT = 110;
export const MOBILE_ROW_GAP = 10;
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
  const midGap = (top + bottom) / 2;

  return [
    {
      id: 'solar-house',
      from: [rightStart, top * 0.7], to: [leftWidth, top * 0.7],
      controls: [[rightStart - 6, top * 0.7 + 8], [leftWidth + 6, top * 0.7 + 8]],
    },
    {
      id: 'house-battery',
      from: [leftWidth * 0.82, top], to: [leftWidth * 0.18, bottom],
      controls: [[leftWidth * 0.69, midGap], [leftWidth * 0.31, midGap]],
    },
    {
      id: 'house-grid',
      from: [leftWidth * 0.93, top], to: [rightStart + leftWidth * 0.52, bottom],
      controls: [[leftWidth + 7, midGap], [rightStart + leftWidth * 0.37, midGap]],
    },
  ];
}
