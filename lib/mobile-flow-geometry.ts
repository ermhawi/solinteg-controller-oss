// Three independent panel-to-panel connections, as in Systemstatus:
// Sol -> Hus, Hus <-> Batteri, Hus <-> Nät.
// With the original 10px row gap, each link takes the shortest useful route
// between facing panel edges without overlapping the other connectors.
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

  const batteryFrom = leftWidth * 0.4;
  const batteryTo = leftWidth * 0.6;
  const gridFrom = leftWidth - 12;
  const gridTo = rightStart + 12;

  return [
    {
      id: 'solar-house',
      from: [rightStart, top * 0.48], to: [leftWidth, top * 0.68],
      controls: [[rightStart - 9, top * 0.48], [leftWidth + 9, top * 0.68]],
    },
    {
      id: 'house-battery',
      from: [batteryFrom, top], to: [batteryTo, bottom],
      controls: [[batteryFrom + 8, midGap - 2], [batteryTo - 8, midGap + 2]],
    },
    {
      id: 'house-grid',
      from: [gridFrom, top], to: [gridTo, bottom],
      controls: [[leftWidth + 5, midGap - 2], [rightStart - 5, midGap + 2]],
    },
  ];
}
