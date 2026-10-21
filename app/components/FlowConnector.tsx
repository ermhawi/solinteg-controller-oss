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
  const b2: Pt = [to[0] - ux * al - px * aw, to[1] - uy * al + py * aw];
  return `${to[0]},${to[1]} ${b1[0]},${b1[1]} ${b2[0]},${b2[1]}`;
}

export default function FlowConnector({
  from,
  to,
  color,
  active,
}: {
  from: Pt;
  to: Pt;
  color: string;
  active: boolean;
}) {
  if (!active) {
    return (
      <line
        x1={from[0]}
        y1={from[1]}
        x2={to[0]}
        y2={to[1]}
        stroke="var(--node-idle-stroke)"
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeDasharray="2 7"
        opacity={0.7}
      />
    );
  }
  return (
    <g>
      <line
        x1={from[0]}
        y1={from[1]}
        x2={to[0]}
        y2={to[1]}
        stroke={color}
        strokeWidth={6}
        strokeLinecap="round"
        opacity={0.16}
      />
      <line
        x1={from[0]}
        y1={from[1]}
        x2={to[0]}
        y2={to[1]}
        stroke={color}
        strokeWidth={2.6}
        strokeLinecap="round"
        strokeDasharray="5 7"
        className="animate-flow-dash"
        style={{ filter: `drop-shadow(0 0 4px ${color})` }}
      />
      <polygon points={arrowPolygon(from, to)} fill={color} />
    </g>
  );
}
