import type { GuideSample } from "../lib/types";

const SIZE = 132;

/**
 * The bullseye: every recent error as a point, the latest as a cross.
 *
 * The trace above says how big the errors are; this says what *shape*
 * they make, which is a different question with different answers. A
 * round cloud is seeing. A cloud stretched along one axis is that axis
 * misbehaving - drift, backlash, an aggressiveness that is too low. A
 * cloud with its centre off the middle is a standing offset the loop is
 * not correcting at all.
 *
 * Every other guider has one of these, and this is why.
 */
export function GuideTarget({ samples, rings = [1, 2] }: { samples: GuideSample[]; rings?: number[] }) {
  const recent = samples.slice(-80);
  const reach = Math.max(
    ...rings,
    ...recent.map((s) => Math.max(Math.abs(s.ra_error_arcsec), Math.abs(s.dec_error_arcsec))),
    1,
  );
  const half = SIZE / 2;
  const place = (value: number) => (value / reach) * (half - 6);
  const latest = recent.at(-1);

  return (
    <figure className="guide-target">
      <svg viewBox={`0 0 ${SIZE} ${SIZE}`} role="img" aria-label="Guiding error distribution">
        {rings
          .filter((ring) => ring < reach)
          .map((ring) => (
            <circle key={ring} cx={half} cy={half} r={place(ring)} className="target-ring" />
          ))}
        <circle cx={half} cy={half} r={half - 6} className="target-ring outer" />
        <line x1={half} y1="4" x2={half} y2={SIZE - 4} className="target-cross" />
        <line x1="4" y1={half} x2={SIZE - 4} y2={half} className="target-cross" />

        {recent.map((sample, index) => (
          <circle
            key={sample.timestamp}
            cx={half + place(sample.ra_error_arcsec)}
            cy={half - place(sample.dec_error_arcsec)}
            r="1.6"
            className="target-point"
            // Older points fade, so the eye follows where it is going
            // rather than where it has been.
            opacity={0.15 + 0.6 * (index / Math.max(recent.length - 1, 1))}
          />
        ))}

        {latest && (
          <g
            className="target-latest"
            transform={`translate(${half + place(latest.ra_error_arcsec)}, ${
              half - place(latest.dec_error_arcsec)
            })`}
          >
            <line x1="-4" y1="-4" x2="4" y2="4" />
            <line x1="-4" y1="4" x2="4" y2="-4" />
          </g>
        )}
      </svg>
      <figcaption className="small faint mono">&plusmn;{reach.toFixed(1)}&Prime;</figcaption>
    </figure>
  );
}
