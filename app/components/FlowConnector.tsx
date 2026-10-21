// Shared SVG styling for the Systemstatus diagram and the compact mobile overview.
export type Pt = [number, number];

function arrowPolygon(from: Pt, to: Pt): string {
  const dx = to[0] - from[0];
  const dy = to[1] - from[1];
  const len = Math.hypot(dx, dy) || 1;
  const ux = dx / len;
  const uy = dy / len;
  const px = -uy;
  const py = ux;
  const al = 9;
  const aw = 6;
  const b1: Pt = [to[0] - ux * al + px * aw, to[1] - uy * al + py * aw];
  const b2: Pt = [to[0] - ux * al - px * aw, to[1] - uy * al - py * aw];
  return `${to[0]},${to[1]} ${b1[0]},${b1[1]} ${b2[0]},${b2[1]}`;
}

function cubicPoint(from: Pt, c1: Pt, c2: Pt, to: Pt, t: number): Pt {
  const s = 1 - t;
  return [
    s * s * s * from[0] + 3 * s * s * t * c1[0] + 3 * s * t * t * c2[0] + t * t * t * to[0],
    s * s * s * from[1] + 3 * s * s * t * c1[1] + 3 * s * t * t * c2[1] + t * t * t * to[1],
  ];
}

export default function FlowConnector({
  from,
  to,
  color,
  active,
  controls,
}: {
  from: Pt;
  to: Pt;
  color: string;
  active: boolean;
  controls?: [Pt, Pt];
}) {
  // Keep the original straight style in Systemstatus. Mobile reuses its colors,
  // glow, dash animation and arrowheads on a gently curved cubic path.
  const d = controls
    ? `M ${from[0]} ${from[1]} C ${controls[0][0]} ${controls[0][1]}, ${controls[1][0]} ${controls[1][1]}, ${to[0]} ${to[1]}`
    : `M ${from[0]} ${from[1]} L ${to[0]} ${to[1]}`;
  const arrowFrom = controls ? cubicPoint(from, controls[0], controls[1], to, 0.64) : from;
  const arrowTo = controls ? cubicPoint(from, controls[0], controls[1], to, 0.77) : to;

  if (!active) {
    return (
      <path d={d} fill="none"
        stroke="var(--node-idle-stroke)"
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeDasharray="2 7"
        opacity={0.7} />
    );
  }
  return (
    <g>
      <path d={d} fill="none"
        stroke={color}
        strokeWidth={6}
        strokeLinecap="round"
        opacity={0.16} />
      <path d={d} fill="none"
        stroke={color}
        strokeWidth={2.6}
        strokeLinecap="round"
        strokeDasharray="5 7"
        className="animate-flow-dash"
        style={{ filter: `drop-shadow(0 0 4px ${color})` }} />
      <polygon points={arrowPolygon(arrowFrom, arrowTo)} fill={color} />
    </g>
  );
}
