/**
 * What the calibration measured, drawn.
 *
 * Two arrows: where the star went when the mount was pushed west, and
 * where it went when it was pushed north. Everything that matters about
 * a calibration is in the picture of those two - whether they came out
 * perpendicular, how far the star actually travelled, and which side of
 * west north landed on.
 *
 * That last one is the difference between a declination correction that
 * helps and one that doubles the error every frame, and it is invisible
 * in a rate and an angle, which is all this used to keep.
 */
export function CalibrationPlot({
  west,
  north,
  size = 128,
}: {
  west: [number, number];
  north: [number, number];
  size?: number;
}) {
  const reach = Math.max(1, Math.hypot(...west), Math.hypot(...north)) * 1.25;
  const half = size / 2;
  // The sensor's y axis points down, as image coordinates do; flipping
  // it here means the picture matches what the guide view shows.
  const to = ([dx, dy]: [number, number]) => ({
    x: half + (dx / reach) * half,
    y: half + (dy / reach) * half,
  });
  const w = to(west);
  const n = to(north);
  const cross = west[0] * north[1] - west[1] * north[0];

  return (
    <svg
      className="calibration-plot"
      viewBox={`0 0 ${size} ${size}`}
      role="img"
      aria-label="Calibration vectors on the sensor"
    >
      <rect x="0.5" y="0.5" width={size - 1} height={size - 1} rx="8" className="plot-frame" />
      <line x1={half} y1="6" x2={half} y2={size - 6} className="plot-axis" />
      <line x1="6" y1={half} x2={size - 6} y2={half} className="plot-axis" />

      <line x1={half} y1={half} x2={w.x} y2={w.y} className="plot-west" />
      <circle cx={w.x} cy={w.y} r="2.5" className="plot-west-dot" />
      <text x={w.x} y={w.y - 5} className="plot-label west" textAnchor="middle">
        W
      </text>

      <line x1={half} y1={half} x2={n.x} y2={n.y} className="plot-north" />
      <circle cx={n.x} cy={n.y} r="2.5" className="plot-north-dot" />
      <text x={n.x} y={n.y - 5} className="plot-label north" textAnchor="middle">
        N
      </text>

      <circle cx={half} cy={half} r="2" className="plot-origin" />
      <text x={size - 5} y={size - 5} textAnchor="end" className="plot-scale">
        {cross >= 0 ? "" : "mirrored "}
        {reach.toFixed(0)} px
      </text>
    </svg>
  );
}
