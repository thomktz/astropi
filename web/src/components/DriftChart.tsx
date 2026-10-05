import type { GuideSample } from "../lib/types";

const WIDTH = 320;
const HEIGHT = 90;
/** The smallest range drawn, in pixels per minute. */
const MIN_RANGE = 0.5;

/**
 * The drift fitted after each frame, in sensor pixels per minute: x to the
 * right, y down, each inside its uncertainty. One window refitted every
 * frame, so it is a line that settles rather than one that jumps.
 */
export function DriftChart({ samples }: { samples: GuideSample[] }) {
  const points = samples.filter((s) => s.fit?.drift_x != null);
  if (points.length < 2) {
    return (
      <div className="small faint" style={{ padding: "16px 0", textAlign: "center" }}>
        Fitting the drift…
      </div>
    );
  }

  const value = (s: GuideSample, axis: "x" | "y") => (axis === "x" ? s.fit?.drift_x : s.fit?.drift_y) ?? 0;
  const error = (s: GuideSample, axis: "x" | "y") =>
    (axis === "x" ? s.fit?.drift_x_error : s.fit?.drift_y_error) ?? 0;
  // Scaled to the estimates, not their early uncertainty, which would
  // flatten everything after the first few frames.
  const range =
    Math.max(MIN_RANGE, ...points.map((s) => Math.max(Math.abs(value(s, "x")), Math.abs(value(s, "y"))))) * 1.2;
  const x = (index: number) => (index / (points.length - 1)) * WIDTH;
  const y = (rate: number) => HEIGHT / 2 - (Math.max(-range, Math.min(range, rate)) / range) * (HEIGHT / 2 - 3);

  const line = (axis: "x" | "y") =>
    points.map((s, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(value(s, axis))}`).join(" ");
  const band = (axis: "x" | "y") => {
    const upper = points.map((s, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(value(s, axis) + error(s, axis))}`);
    const lower = points.map((s, i) => `L${x(i)},${y(value(s, axis) - error(s, axis))}`).reverse();
    return `${upper.join(" ")} ${lower.join(" ")} Z`;
  };
  const last = points[points.length - 1];

  return (
    <svg className="chart" viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label="Drift, pixels per minute">
      <line x1="0" y1={HEIGHT / 2} x2={WIDTH} y2={HEIGHT / 2} stroke="var(--border)" />
      <path d={band("x")} fill="var(--accent)" opacity="0.12" />
      <path d={band("y")} fill="var(--fair)" opacity="0.12" />
      <path d={line("x")} fill="none" stroke="var(--accent)" strokeWidth="1.5" />
      <path d={line("y")} fill="none" stroke="var(--fair)" strokeWidth="1.5" />
      <text x="2" y="10" fontSize="9" fill="var(--accent)">
        drift x {signed(value(last, "x"))}
      </text>
      <text x="82" y="10" fontSize="9" fill="var(--fair)">
        y {signed(value(last, "y"))}
      </text>
      <text x={WIDTH - 4} y="10" fontSize="9" textAnchor="end" fill="var(--text-faint)">
        &#177;{range.toFixed(1)} px/min
      </text>
    </svg>
  );
}

function signed(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}`;
}
