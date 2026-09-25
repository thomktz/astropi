import type { GuideSample } from "../lib/types";

const WIDTH = 320;
const HEIGHT = 90;
/** The smallest range drawn, in arcsec per minute. */
const MIN_RANGE = 2;

/**
 * Each axis's drift estimate over the run, with its uncertainty.
 *
 * One estimate, refined by every frame - so this is a line that settles,
 * inside a band that narrows as it does. A line that keeps moving while
 * its band is narrow is a drift that is genuinely changing: periodic
 * error, on right ascension.
 */
export function DriftChart({ samples }: { samples: GuideSample[] }) {
  const points = samples.filter((s) => s.ra_drift_arcsec_per_min != null);
  if (points.length < 2) {
    return (
      <div className="small faint" style={{ padding: "16px 0", textAlign: "center" }}>
        Estimating drift…
      </div>
    );
  }

  const value = (s: GuideSample, axis: "ra" | "dec") =>
    (axis === "ra" ? s.ra_drift_arcsec_per_min : s.dec_drift_arcsec_per_min) ?? 0;
  const error = (s: GuideSample, axis: "ra" | "dec") =>
    (axis === "ra" ? s.ra_drift_error_arcsec_per_min : s.dec_drift_error_arcsec_per_min) ?? 0;
  // Scaled to the estimates, not to their early uncertainty - the first
  // few frames claim +/-12"/min and would flatten everything after.
  const range = Math.max(
    MIN_RANGE,
    ...points.map((s) => Math.max(Math.abs(value(s, "ra")), Math.abs(value(s, "dec")))),
  ) * 1.2;
  const x = (index: number) => (index / (points.length - 1)) * WIDTH;
  const y = (rate: number) =>
    HEIGHT / 2 - (Math.max(-range, Math.min(range, rate)) / range) * (HEIGHT / 2 - 3);

  const line = (axis: "ra" | "dec") =>
    points.map((s, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(value(s, axis))}`).join(" ");
  const band = (axis: "ra" | "dec") => {
    const upper = points.map((s, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(value(s, axis) + error(s, axis))}`);
    const lower = points
      .map((s, i) => `L${x(i)},${y(value(s, axis) - error(s, axis))}`)
      .reverse();
    return `${upper.join(" ")} ${lower.join(" ")} Z`;
  };
  const last = points[points.length - 1];

  return (
    <svg className="chart" viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label="Drift estimate">
      <line x1="0" y1={HEIGHT / 2} x2={WIDTH} y2={HEIGHT / 2} stroke="var(--border)" />
      <path d={band("ra")} fill="var(--accent)" opacity="0.12" />
      <path d={band("dec")} fill="var(--fair)" opacity="0.12" />
      <path d={line("ra")} fill="none" stroke="var(--accent)" strokeWidth="1.5" />
      <path d={line("dec")} fill="none" stroke="var(--fair)" strokeWidth="1.5" />
      <text x="2" y="10" fontSize="9" fill="var(--accent)">
        RA {signed(value(last, "ra"))}
      </text>
      <text x="62" y="10" fontSize="9" fill="var(--fair)">
        Dec {signed(value(last, "dec"))}
      </text>
      <text x={WIDTH - 4} y="10" fontSize="9" textAnchor="end" fill="var(--text-faint)">
        &#177;{range.toFixed(0)}"/min
      </text>
    </svg>
  );
}

function signed(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(1)}`;
}
