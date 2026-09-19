import type { Visibility } from "../lib/types";

const WIDTH = 320;
const HEIGHT = 96;

/**
 * Tonight's altitude curve for one target.
 *
 * The horizon line and the shaded "too low" band do the real work: the
 * decision this chart supports is when the object is high enough to be
 * worth shooting, not what its exact altitude is at any moment.
 */
export function AltitudeChart({ visibility }: { visibility: Visibility }) {
  const { curve } = visibility;
  if (curve.length < 2) return null;

  const x = (index: number) => (index / (curve.length - 1)) * WIDTH;
  // Altitude runs -90..90, but only -20 up is interesting; clamping the
  // range spends the vertical pixels where the decision is made.
  const y = (altitude: number) => HEIGHT - ((Math.max(altitude, -20) + 20) / 110) * HEIGHT;

  const line = curve.map((point, index) => `${index === 0 ? "M" : "L"}${x(index)},${y(point.alt)}`).join(" ");
  const horizon = y(0);
  const usable = y(30);

  const now = Date.now();
  const nowIndex = curve.findIndex((point) => new Date(point.at).getTime() >= now);

  return (
    <svg className="chart" viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label="Altitude tonight">
      <rect x="0" y={horizon} width={WIDTH} height={HEIGHT - horizon} fill="var(--panel-raised)" opacity="0.6" />
      <line x1="0" y1={usable} x2={WIDTH} y2={usable} stroke="var(--border)" strokeDasharray="3 4" />
      <line x1="0" y1={horizon} x2={WIDTH} y2={horizon} stroke="var(--text-faint)" />
      <path d={line} fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinejoin="round" />
      {nowIndex > 0 && (
        <line x1={x(nowIndex)} y1="0" x2={x(nowIndex)} y2={HEIGHT} stroke="var(--fair)" strokeWidth="1.5" />
      )}
      <text x="2" y={usable - 3} fontSize="9" fill="var(--text-faint)">
        30&#176;
      </text>
    </svg>
  );
}
