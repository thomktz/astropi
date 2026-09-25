import type { GuideSample } from "../lib/types";
import type { GuideProgress } from "../lib/useTelemetry";
import { DriftChart } from "./DriftChart";

/** Phases between starting a run and holding the star. */
export const DRIFT_PHASES = new Set(["drift_measuring", "recentring"]);

const STAGES = [
  { phase: "drift_measuring", label: "Measure drift" },
  { phase: "recentring", label: "Cancel it, walk back" },
];

/**
 * Measuring the drift before any correction, then walking back.
 *
 * The chart is the display: each axis's drift estimate, one line refined
 * by every frame inside a band that narrows as it becomes known. The
 * measuring ends when both bands are narrow enough.
 */
export function DriftProgress({
  progress,
  samples,
}: {
  progress: GuideProgress;
  samples: GuideSample[];
}) {
  const reached = STAGES.findIndex((stage) => stage.phase === progress.phase);
  const measuring = progress.phase === "drift_measuring";

  return (
    <div className="stack">
      <DriftChart samples={samples} />
      <div className="stages">
        {STAGES.map((stage, index) => (
          <div
            key={stage.label}
            className={`stage ${index === reached ? "active" : index < reached ? "done" : ""}`}
          >
            <span className={`dot ${index === reached ? "busy" : index < reached ? "live" : ""}`} />
            <span className="stage-label" style={{ gridColumn: "2 / 4" }}>
              {stage.label}
            </span>
            <span className="mono small">
              {index === reached && measuring && progress.elapsed_s != null
                ? `${progress.elapsed_s.toFixed(0)}s`
                : ""}
            </span>
            {index === reached && measuring && progress.elapsed_s != null && progress.max_s && (
              <span className="mini-bar stage-bar" aria-hidden="true">
                <span style={{ width: `${Math.min(100, (progress.elapsed_s / progress.max_s) * 100)}%` }} />
              </span>
            )}
          </div>
        ))}
      </div>
      <div className="spread mono small">
        <Axis label="RA" drift={progress.ra_drift} error={progress.ra_drift_error} seeing={progress.ra_seeing} />
        <Axis label="Dec" drift={progress.dec_drift} error={progress.dec_drift_error} seeing={progress.dec_seeing} />
      </div>
      {measuring && progress.precision != null && (
        <div className="small faint">
          No corrections until both are known to ±{progress.precision}″/min
          {progress.min_s != null && progress.max_s != null
            ? ` (${progress.min_s.toFixed(0)}–${progress.max_s.toFixed(0)}s)`
            : ""}
        </div>
      )}
    </div>
  );
}

function Axis({
  label,
  drift,
  error,
  seeing,
}: {
  label: string;
  drift?: number;
  error?: number;
  seeing?: number;
}) {
  if (drift == null) return <span>{label} --</span>;
  const known = error != null && Math.abs(drift) > 2 * error;
  return (
    <span title={seeing != null ? `Frame-to-frame scatter ${seeing.toFixed(2)}″` : undefined}>
      {label}{" "}
      <span className={known ? "" : "faint"}>
        {drift >= 0 ? "+" : ""}
        {drift.toFixed(1)}
      </span>
      <span className="faint"> ±{(error ?? 0).toFixed(1)}″/min</span>
    </span>
  );
}
