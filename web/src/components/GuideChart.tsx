import type { GuideSample } from "../lib/types";

const WIDTH = 320;
const HEIGHT = 110;
/** The scale it prefers, in arcseconds, and the one good guiding fits in. */
const RANGE = 4;

/**
 * The guiding error trace, one line per axis.
 *
 * Fixed scale while the errors fit, so that good guiding is visibly a flat
 * line near the middle rather than a graph that looks the same whether the
 * rig is holding 0.4 arcseconds or 4.
 *
 * It grows when they do not, which is the part that was missing: errors
 * beyond the range used to be clipped, so a declination axis walking out
 * to twenty arcseconds drew a flat line pinned to the top edge - the
 * picture of a rock-steady axis, produced by the one that was running
 * away. It took reading the raw RMS figures to notice.
 */
export function GuideChart({
  samples: given,
  corrections = true,
}: {
  samples: GuideSample[];
  /** Draw the pulses that were sent, under the errors that caused them. */
  corrections?: boolean;
}) {
  // One sample is a sample. It used to say "no guide samples yet"
  // directly underneath the numbers read from that very sample, which
  // is the sort of thing that makes a panel untrustworthy.
  const samples = given.length === 1 ? [given[0], given[0]] : given;
  if (samples.length < 2) {
    return (
      <div className="small faint" style={{ padding: "24px 0", textAlign: "center" }}>
        No guide samples yet
      </div>
    );
  }

  const range = Math.max(
    RANGE,
    ...samples.map((s) => Math.max(Math.abs(s.ra_error_arcsec), Math.abs(s.dec_error_arcsec))),
  );
  const x = (index: number) => (index / Math.max(samples.length - 1, 1)) * WIDTH;
  const y = (error: number) => HEIGHT / 2 - (error / range) * (HEIGHT / 2 - 3);

  const trace = (pick: (sample: GuideSample) => number) =>
    samples.map((sample, index) => `${index === 0 ? "M" : "L"}${x(index)},${y(pick(sample))}`).join(" ");

  return (
    <svg className="chart" viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label="Guiding error">
      {/* One-arcsecond band: inside it, guiding is doing its job. */}
      <rect x="0" y={y(1)} width={WIDTH} height={y(-1) - y(1)} fill="var(--good)" opacity="0.07" />
      {/* The scale it prefers, drawn when the data has outgrown it, so a
          graph that has been stretched says so rather than just looking
          calmer than it is. */}
      {range > RANGE && (
        <>
          <line x1="0" y1={y(RANGE)} x2={WIDTH} y2={y(RANGE)} stroke="var(--border)" strokeDasharray="3 3" />
          <line x1="0" y1={y(-RANGE)} x2={WIDTH} y2={y(-RANGE)} stroke="var(--border)" strokeDasharray="3 3" />
        </>
      )}
      <line x1="0" y1={HEIGHT / 2} x2={WIDTH} y2={HEIGHT / 2} stroke="var(--border)" />
      {/*
        The corrections, as bars from the centre line: what was actually
        sent to the mount, against the error that asked for it. A trace
        alone cannot tell "the loop is choosing not to correct" - which
        is what the minimum-move threshold does on purpose - from "the
        loop is correcting and the mount is ignoring it", and those want
        very different things done about them.
      */}
      {corrections &&
        samples.map((sample, index) => {
          const longest = Math.max(
            1,
            ...samples.map((s) => Math.max(Math.abs(s.ra_pulse_ms), Math.abs(s.dec_pulse_ms))),
          );
          const height = (ms: number, sign: number) =>
            (Math.abs(ms) / longest) * (HEIGHT / 2 - 3) * sign;
          const raSign = sample.ra_error_arcsec >= 0 ? -1 : 1;
          const decSign = sample.dec_error_arcsec >= 0 ? -1 : 1;
          return (
            <g key={sample.timestamp} opacity="0.35">
              <line
                x1={x(index)}
                y1={HEIGHT / 2}
                x2={x(index)}
                y2={HEIGHT / 2 + height(sample.ra_pulse_ms, raSign)}
                stroke="var(--accent)"
                strokeWidth="1.5"
              />
              <line
                x1={x(index) + 1.5}
                y1={HEIGHT / 2}
                x2={x(index) + 1.5}
                y2={HEIGHT / 2 + height(sample.dec_pulse_ms, decSign)}
                stroke="var(--fair)"
                strokeWidth="1.5"
              />
            </g>
          );
        })}
      <path d={trace((s) => s.ra_error_arcsec)} fill="none" stroke="var(--accent)" strokeWidth="1.5" />
      <path d={trace((s) => s.dec_error_arcsec)} fill="none" stroke="var(--fair)" strokeWidth="1.5" />
      <text x="2" y="10" fontSize="9" fill="var(--accent)">
        RA
      </text>
      <text x="24" y="10" fontSize="9" fill="var(--fair)">
        Dec
      </text>
      <text
        x={WIDTH - 4}
        y="10"
        fontSize="9"
        textAnchor="end"
        fill={range > RANGE ? "var(--fair)" : "var(--text-faint)"}
      >
        &#177;{range.toFixed(range > RANGE ? 0 : 0)}"
      </text>
    </svg>
  );
}
