import type { GuideSample } from "./types";

/**
 * The statistics every other guider puts next to its graph.
 *
 * Computed here rather than asked for, because the browser already holds
 * the samples the graph is drawn from and these are three lines of
 * arithmetic over them - a round trip to recompute what is on screen
 * would be the strange choice.
 */
export interface GuideStats {
  /** Worst single excursion in the window, per axis. */
  peakRa: number;
  peakDec: number;
  /**
   * How often consecutive RA corrections reverse direction.
   *
   * PHD2 calls this "RA Osc". Above about 0.6 the loop is fighting
   * itself: each correction overshoots and the next one comes back,
   * which looks like guiding and behaves like shaking. The cure is less
   * aggressiveness, not more.
   */
  raOscillation: number | null;
  /** Drift in arcseconds per minute, from a least-squares fit. */
  driftRa: number | null;
  driftDec: number | null;
}

export function guideStats(samples: GuideSample[]): GuideStats {
  const recent = samples.slice(-120);
  if (recent.length === 0) {
    return { peakRa: 0, peakDec: 0, raOscillation: null, driftRa: null, driftDec: null };
  }

  return {
    peakRa: Math.max(...recent.map((s) => Math.abs(s.ra_error_arcsec))),
    peakDec: Math.max(...recent.map((s) => Math.abs(s.dec_error_arcsec))),
    raOscillation: oscillation(recent),
    driftRa: drift(recent, (s) => s.ra_error_arcsec),
    driftDec: drift(recent, (s) => s.dec_error_arcsec),
  };
}

function oscillation(samples: GuideSample[]): number | null {
  const directions = samples
    .filter((sample) => sample.ra_pulse_ms > 0 && sample.ra_direction)
    .map((sample) => sample.ra_direction);
  if (directions.length < 4) return null;
  let reversals = 0;
  for (let index = 1; index < directions.length; index += 1) {
    if (directions[index] !== directions[index - 1]) reversals += 1;
  }
  return reversals / (directions.length - 1);
}

/**
 * Least-squares slope through the errors, in arcseconds per minute.
 *
 * A steady slope on an axis the loop is actively correcting means the
 * loop is losing: something is pulling harder than it is pushing back.
 * On declination that something is usually polar misalignment.
 */
function drift(samples: GuideSample[], pick: (sample: GuideSample) => number): number | null {
  if (samples.length < 6) return null;
  const t0 = samples[0].timestamp;
  const xs = samples.map((s) => (s.timestamp - t0) / 60);
  const ys = samples.map(pick);
  const n = xs.length;
  const meanX = xs.reduce((a, b) => a + b, 0) / n;
  const meanY = ys.reduce((a, b) => a + b, 0) / n;
  let top = 0;
  let bottom = 0;
  for (let i = 0; i < n; i += 1) {
    top += (xs[i] - meanX) * (ys[i] - meanY);
    bottom += (xs[i] - meanX) ** 2;
  }
  if (bottom <= 0) return null;
  return top / bottom;
}
