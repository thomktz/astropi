import { useEffect, useState } from "react";
import type { Telemetry } from "../lib/useTelemetry";

/** How often to refetch the guide frame if no event announced one. */
const FALLBACK_MS = 4000;

/**
 * The star being pushed, on the frame it is being pushed across.
 *
 * Calibration was a row of counters saying "pulse 3 of 5" - which star,
 * where, and what was being measured were all absent, and for five
 * seconds at a time nothing on screen changed at all. It looked exactly
 * like a thing doing nothing.
 *
 * This is the frame, the star, and every position it has been in since
 * the leg started. The track walking west and then north is the whole
 * measurement, visible as it is taken.
 */
export function CalibrationView({ telemetry }: { telemetry: Telemetry }) {
  const [tick, setTick] = useState(0);
  const [missing, setMissing] = useState(false);

  useEffect(() => {
    const timer = setInterval(() => setTick((count) => count + 1), FALLBACK_MS);
    return () => clearInterval(timer);
  }, []);

  const progress = telemetry.guideProgress;
  const phase = progress?.phase;

  // The last track that had anything in it, kept until a new leg starts
  // measuring. The return legs report no track - they are not measured -
  // and dropping it there would blank the picture of the leg that was
  // just finished, which is the part worth looking at.
  const [shown, setShown] = useState<{ track: [number, number][]; width: number; height: number } | null>(
    null,
  );
  const incoming = progress?.track;
  if (phase === "acquiring" && shown !== null) {
    setShown(null);
  } else if (
    incoming != null &&
    incoming.length > 0 &&
    progress?.frame_width != null &&
    progress.frame_height != null &&
    incoming !== undefined &&
    (shown === null || shown.track !== incoming)
  ) {
    setShown({ track: incoming, width: progress.frame_width, height: progress.frame_height });
  }

  const width = shown?.width;
  const height = shown?.height;
  const track = shown?.track ?? [];
  const stamp = `${telemetry.guideFrameSeq}-${tick}`;

  // Zoomed onto the star, because the whole point is a motion of about
  // sixteen pixels on a sensor that is twelve hundred wide. At full
  // frame that is four screen pixels of travel - the star is visible and
  // the measurement is not, which is the complaint this exists to fix.
  const window = viewBox(track, width, height);
  // Marks sized in sensor pixels, so they stay the same size on screen
  // however far the view is zoomed in.
  const scale = window ? Number(window.split(" ")[2]) / 150 : 1;

  return (
    <div className="calibration-view">
      {missing && <div className="calibration-empty small faint">no guide frame yet</div>}

      {width != null && height != null && window != null && (
        <svg viewBox={window} preserveAspectRatio="xMidYMid slice" aria-hidden="true">
          {/*
            The frame inside the SVG rather than behind it, so the image
            and the track share one coordinate system and zoom together.
          */}
          <image
            href={`/api/guiding/frame.png?max_dimension=1024&t=${stamp}`}
            x="0"
            y="0"
            width={width}
            height={height}
            onError={() => setMissing(true)}
            onLoad={() => setMissing(false)}
            preserveAspectRatio="none"
          />
          {/* Where it started: everything is measured from here. */}
          <circle cx={track[0][0]} cy={track[0][1]} r={scale * 4} className="track-origin" />
          <polyline
            points={track.map(([x, y]) => `${x},${y}`).join(" ")}
            className="track-line"
          />
          {track.map(([x, y], index) => (
            <circle
              key={`${x}-${y}-${index}`}
              cx={x}
              cy={y}
              r={scale * 1.8}
              className="track-point"
            />
          ))}
          {/* Where it is now. */}
          <g
            className="track-star"
            transform={`translate(${track[track.length - 1][0]}, ${track[track.length - 1][1]})`}
          >
            <circle r={scale * 6} />
            <line x1={-scale * 11} y1="0" x2={-scale * 8} y2="0" />
            <line x1={scale * 8} y1="0" x2={scale * 11} y2="0" />
          </g>
        </svg>
      )}

      {progress?.star_x != null && (
        <span className="calibration-readout small mono">
          {progress.star_x.toFixed(0)}, {progress.star_y?.toFixed(0)} px
          {progress.moved_px != null && ` · moved ${progress.moved_px.toFixed(1)} px`}
        </span>
      )}
    </div>
  );
}

/**
 * A window onto the sensor, centred on the track and wide enough to hold
 * it with room to see.
 *
 * Never smaller than 120 pixels: a track of one point would otherwise
 * zoom to a single star filling the frame, which is a picture of
 * nothing at maximum magnification.
 */
function viewBox(
  track: [number, number][],
  width: number | undefined,
  height: number | undefined,
): string | null {
  if (width == null || height == null) return null;
  if (track.length === 0) return `0 0 ${width} ${height}`;

  const xs = track.map(([x]) => x);
  const ys = track.map(([, y]) => y);
  const spread = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
  // Four times the travel, so the track crosses a good part of the view
  // rather than sitting in the middle of a field of noise - but never
  // below 80 pixels, or a single point fills the frame with one star.
  const size = Math.min(Math.max(80, spread * 4), Math.min(width, height));
  const centreX = (Math.min(...xs) + Math.max(...xs)) / 2;
  const centreY = (Math.min(...ys) + Math.max(...ys)) / 2;
  // Clamped inside the sensor, so a star near an edge does not open a
  // window half of which is off the chip.
  const left = Math.min(Math.max(centreX - size / 2, 0), width - size);
  const top = Math.min(Math.max(centreY - size * 0.75 / 2, 0), height - (size * 0.75));
  return `${left.toFixed(1)} ${top.toFixed(1)} ${size.toFixed(1)} ${(size * 0.75).toFixed(1)}`;
}
