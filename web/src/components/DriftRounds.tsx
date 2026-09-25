import type { GuideProgress } from "../lib/useTelemetry";
import { Hint } from "./Hint";

/** Phases of measuring and cancelling the drift before guiding. */
export const DRIFT_PHASES = new Set(["drift_measuring", "drift_measured", "recentring"]);

const STAGES = [
  { phases: ["drift_measuring", "drift_measured"], label: "Measure & cancel drift" },
  { phases: ["recentring"], label: "Walk back to lock" },
];

/**
 * The drift rounds: measure with no corrections, cancel what was found,
 * measure what is left - until nothing is.
 *
 * The numbers are the whole story here, so they are the display: what is
 * left over on each axis this round, against what is being cancelled,
 * with every earlier round underneath.
 */
export function DriftRounds({ progress }: { progress: GuideProgress }) {
  const reached = STAGES.findIndex((stage) => stage.phases.includes(progress.phase));
  const measuring = progress.phase === "drift_measuring";
  const history = progress.history ?? [];

  return (
    <div className="stack">
      <div className="stages">
        {STAGES.map((stage, index) => (
          <div
            key={stage.label}
            className={`stage ${index === reached ? "active" : index < reached ? "done" : ""}`}
          >
            <span className={`dot ${index === reached ? "busy" : index < reached ? "live" : ""}`} />
            {/* Spans the note column too: these labels are longer. */}
            <span className="stage-label" style={{ gridColumn: "2 / 4" }}>
              {stage.label}
            </span>
            <span className="mono small">
              {index === reached && progress.round != null
                ? `round ${progress.round}/${progress.rounds}`
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

      <table className="drift-table mono small">
        <thead>
          <tr>
            <th />
            <th>
              Left over
              <Hint>
                Drift measured this round with no position corrections, in arcsec per minute, ± its
                uncertainty. The round ends once that is small enough, or at the time limit.
              </Hint>
            </th>
            <th>
              Cancelling
              <Hint>
                The drift being cancelled: sent as a pulse every frame, sized to the time since the
                last one.
              </Hint>
            </th>
            <th>
              Lands
              <Hint>
                How much of the calibrated effect a correction actually has, from comparing two
                rounds at different rates. Measured once a round has changed the rate enough to
                tell.
              </Hint>
            </th>
          </tr>
        </thead>
        <tbody>
          <AxisRow
            label="RA"
            drift={progress.ra_drift}
            error={progress.ra_drift_error}
            cancelling={progress.ra_cancelling}
            response={progress.ra_response}
            unsteady={progress.ra_unsteady}
          />
          <AxisRow
            label="Dec"
            drift={progress.dec_drift}
            error={progress.dec_drift_error}
            cancelling={progress.dec_cancelling}
            response={progress.dec_response}
            unsteady={progress.dec_unsteady}
          />
        </tbody>
      </table>

      {measuring && progress.elapsed_s != null && (
        <div className="small faint mono">
          {progress.elapsed_s.toFixed(0)}s · {progress.frames ?? 0} frames · min{" "}
          {progress.min_s?.toFixed(0)}s, max {progress.max_s?.toFixed(0)}s
        </div>
      )}

      {history.length > 0 && (
        <table className="drift-table mono small faint">
          <thead>
            <tr>
              <th>Round</th>
              <th>RA left</th>
              <th>Dec left</th>
              <th>while cancelling</th>
            </tr>
          </thead>
          <tbody>
            {history.map((round) => (
              <tr key={round.round}>
                <td>{round.round}</td>
                <td>{signed(round.ra_drift)}</td>
                <td>{signed(round.dec_drift)}</td>
                <td>
                  {signed(round.ra_cancelling)} / {signed(round.dec_cancelling)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function AxisRow({
  label,
  drift,
  error,
  cancelling,
  response,
  unsteady,
}: {
  label: string;
  drift?: number;
  error?: number;
  cancelling?: number;
  response?: number | null;
  unsteady?: boolean;
}) {
  return (
    <tr>
      <th>{label}</th>
      <td
        className={unsteady ? "fair" : undefined}
        title={
          unsteady
            ? "Changed from round to round instead of shrinking - periodic error. Only its average is cancelled; position guiding handles the swing."
            : undefined
        }
      >
        {drift == null ? "--" : `${signed(drift)} ± ${(error ?? 0).toFixed(1)}`}
        {unsteady && " ~"}
      </td>
      <td>{signed(cancelling)}</td>
      <td>{response == null ? "--" : `${Math.round(response * 100)}%`}</td>
    </tr>
  );
}

function signed(value: number | undefined): string {
  if (value == null) return "--";
  return `${value >= 0 ? "+" : ""}${value.toFixed(1)}`;
}
