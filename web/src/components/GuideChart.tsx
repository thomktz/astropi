import type { GuideSample } from "../lib/types";

const WIDTH = 320;
const HEIGHT = 110;
/** Vertical range in arcseconds; errors beyond this are clipped, not scaled. */
const RANGE = 4;

/**
 * The guiding error trace.
 *
 * Fixed scale rather than auto-scaling to the data. An auto-scaled graph
 * looks identical whether the rig is guiding at 0.4 arcseconds or 4, which
 * defeats the point of glancing at it - here, good guiding is visibly a
 * flat line near the middle.
 */
export function GuideChart({ samples }: { samples: GuideSample[] }) {
  if (samples.length < 2) {
    return (
      <div className="small faint" style={{ padding: "24px 0", textAlign: "center" }}>
        No guide samples yet
      </div>
    );
  }

  const x = (index: number) => (index / Math.max(samples.length - 1, 1)) * WIDTH;
  const y = (error: number) => HEIGHT / 2 - (Math.max(-RANGE, Math.min(RANGE, error)) / RANGE) * (HEIGHT / 2);

  const trace = (pick: (sample: GuideSample) => number) =>
    samples.map((sample, index) => `${index === 0 ? "M" : "L"}${x(index)},${y(pick(sample))}`).join(" ");

  return (
    <svg className="chart" viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label="Guiding error">
      {/* One-arcsecond band: inside it, guiding is doing its job. */}
      <rect x="0" y={y(1)} width={WIDTH} height={y(-1) - y(1)} fill="var(--good)" opacity="0.07" />
      <line x1="0" y1={HEIGHT / 2} x2={WIDTH} y2={HEIGHT / 2} stroke="var(--border)" />
      <path d={trace((s) => s.ra_error_arcsec)} fill="none" stroke="var(--accent)" strokeWidth="1.5" />
      <path d={trace((s) => s.dec_error_arcsec)} fill="none" stroke="var(--fair)" strokeWidth="1.5" />
      <text x="2" y="10" fontSize="9" fill="var(--accent)">
        RA
      </text>
      <text x="24" y="10" fontSize="9" fill="var(--fair)">
        Dec
      </text>
      <text x={WIDTH - 20} y="10" fontSize="9" fill="var(--text-faint)">
        &#177;{RANGE}"
      </text>
    </svg>
  );
}
