import { useMutation } from "@tanstack/react-query";
import { api } from "../lib/api";
import { arcsec } from "../lib/format";
import type { Telemetry } from "../lib/useTelemetry";
import { CalibrationPlot } from "./CalibrationPlot";
import { ErrorNote, Field } from "./Field";
import { GuideChart } from "./GuideChart";
import { Modal } from "./Modal";

/**
 * What calibration and settling are doing, while they do it.
 *
 * Both were a single word on a pill for half a minute, followed by a
 * number or an error. This is the part in between, which is where both
 * of them go wrong: a star too faint to hold, a mount that is parked and
 * swallowing every pulse, a sky that will not settle.
 */

/** Phases that belong to a calibration rather than to a settle. */
const CALIBRATION_PHASES = new Set([
  "acquiring",
  "acquired",
  "west",
  "west_measured",
  "east",
  "north",
  "north_measured",
  "south",
  "calibrated",
  "failed",
]);

/** The legs of a calibration, in the order it walks them. */
const LEGS = [
  { phases: ["acquiring", "acquired"], label: "Find a star" },
  { phases: ["west", "west_measured"], label: "Push west" },
  { phases: ["east"], label: "Come back east" },
  { phases: ["north", "north_measured"], label: "Push north" },
  { phases: ["south"], label: "Come back south" },
  { phases: ["calibrated"], label: "Measure the rates" },
];

export function GuidingProgress({
  telemetry,
  onClose,
}: {
  telemetry: Telemetry;
  onClose: () => void;
}) {
  const stop = useMutation({ mutationFn: api.guiding.stop });

  const latest = telemetry.guideSamples.at(-1);
  const state = telemetry.guideState;
  const progress = telemetry.guideProgress;
  const phase = progress?.phase ?? "";
  // Which of the two this window is about is decided by the phase, not
  // by the state: a finished calibration reports its result and *then*
  // goes back to "stopped", and the result is the part worth reading.
  const calibrating =
    state === "calibrating" || CALIBRATION_PHASES.has(phase);
  const settled = phase === "guiding";
  // On the last leg, everything before it is done - `findIndex` returns
  // the measuring row for "calibrated", which is what should be lit.
  const reached = LEGS.findIndex((leg) => leg.phases.includes(phase));
  const finished = phase === "calibrated" || phase === "failed";

  return (
    <Modal
      title={
        phase === "calibrated"
          ? "Guide loop calibrated"
          : calibrating
            ? "Calibrating the guide loop"
            : settled
              ? "Guiding"
              : "Settling"
      }
      subtitle={<span className="mono">{progress?.message ?? state}</span>}
      onClose={onClose}
    >
      {/*
        Said plainly, because "calibrate" does not explain itself: the
        loop has to learn how far the mount moves for a given pulse, and
        which way round the camera is, before a measured error in pixels
        can become a correction in milliseconds.
      */}
      {calibrating && (
        <p className="small faint" style={{ margin: 0 }}>
          Pushing a star a known amount and watching where it goes, to learn how far the mount
          moves per second of pulse and which way round the camera is. Without it an error in
          pixels cannot become a correction in milliseconds.
        </p>
      )}

      {calibrating ? (
        <div className="stages">
          {LEGS.map((leg, index) => (
            <div
              key={leg.label}
              className={`stage ${
                index === reached && !finished ? "active" : index <= reached ? "done" : ""
              }`}
            >
              <span
                className={`dot ${
                  index === reached && !finished ? "busy" : index <= reached ? "live" : ""
                }`}
              />
              <span className="stage-label" style={{ minWidth: "8.5em" }}>
                {leg.label}
              </span>
              <span className="stage-note small faint">
                {index === reached && !finished ? legNote(progress) : ""}
              </span>
              <span className="mono small">
                {index === reached && !finished && progress?.pulses
                  ? `${progress.pulse ?? 0}/${progress.pulses}`
                  : ""}
              </span>
              {index === reached && !finished && progress?.pulses != null && (
                <span className="mini-bar stage-bar" aria-hidden="true">
                  <span
                    style={{ width: `${((progress.pulse ?? 0) / progress.pulses) * 100}%` }}
                  />
                </span>
              )}
            </div>
          ))}
        </div>
      ) : (
        /*
          Settling is a wait with two conditions - hold inside a
          threshold, and hold there long enough - shown against the trace
          that is deciding it. One line of text about the current error
          says nothing about whether it is converging or running away,
          which is the only question being asked here.
        */
        <div className="stack">
          <GuideChart samples={telemetry.guideSamples} />
          <div className="spread">
            <Field
              label="RA error"
              value={latest == null ? "--" : arcsec(latest.ra_error_arcsec, 2)}
            />
            <Field
              label="Dec error"
              value={latest == null ? "--" : arcsec(latest.dec_error_arcsec, 2)}
            />
            <Field
              label="Last RA pulse"
              value={latest == null ? "--" : `${latest.ra_pulse_ms.toFixed(0)} ms`}
            />
            <Field
              label="Last Dec pulse"
              value={latest == null ? "--" : `${latest.dec_pulse_ms.toFixed(0)} ms`}
            />
          </div>
        <div className="stages">
          <div className={`stage ${settled ? "done" : "active"}`}>
            <span className={`dot ${settled ? "live" : phase === "searching" ? "down" : "busy"}`} />
            <span className="stage-label" style={{ minWidth: "8.5em" }}>
              {phase === "searching" ? "Searching" : "Holding"}
            </span>
            <span className="stage-note small faint">
              {phase === "searching"
                ? progress?.message
                : progress?.error_arcsec != null && progress.settle_arcsec != null
                  ? `${arcsec(progress.error_arcsec, 2)} against ${arcsec(progress.settle_arcsec, 1)}`
                  : "waiting for a sample"}
            </span>
            <span className="mono small">
              {phase === "searching"
                ? `${progress?.lost_frames ?? 0}/${progress?.max_lost_frames ?? 0}`
                : progress?.held_s != null && progress.settle_time_s != null
                  ? `${progress.held_s.toFixed(0)}/${progress.settle_time_s.toFixed(0)}s`
                  : ""}
            </span>
            {progress?.held_s != null && progress.settle_time_s != null && (
              <span className="mini-bar stage-bar" aria-hidden="true">
                <span
                  style={{
                    width: `${Math.min(100, (progress.held_s / progress.settle_time_s) * 100)}%`,
                  }}
                />
              </span>
            )}
          </div>
        </div>
        </div>
      )}

      {/* The star it is working on, which is the thing that usually fails. */}
      {progress?.star_x != null && (
        <div className="spread">
          <Field
            label="Guide star"
            value={`${progress.star_x.toFixed(0)}, ${progress.star_y?.toFixed(0)}`}
          />
          <Field label="SNR" value={progress.snr == null ? "--" : progress.snr.toFixed(0)} />
          <Field
            label="Size"
            value={progress.hfd == null ? "--" : `${progress.hfd.toFixed(1)} px`}
          />
          <Field
            label="Candidates"
            value={progress.candidates == null ? "--" : String(progress.candidates)}
          />
        </div>
      )}

      {/* What the calibration measured, once it has - drawn and named. */}
      {progress?.ra_rate_arcsec_per_s != null && progress.west_shift_px && progress.north_shift_px && (
        <div className="row" style={{ alignItems: "flex-start", gap: 12 }}>
          <CalibrationPlot
            west={progress.west_shift_px as [number, number]}
            north={progress.north_shift_px as [number, number]}
          />
          <div className="stack small" style={{ flex: "1 1 auto" }}>
            <div className="spread">
              <span className="label">West took the star</span>
              <span className="mono">
                {progress.west_shift_px[0].toFixed(1)}, {progress.west_shift_px[1].toFixed(1)} px
              </span>
            </div>
            <div className="spread">
              <span className="label">North took it</span>
              <span className="mono">
                {progress.north_shift_px[0].toFixed(1)}, {progress.north_shift_px[1].toFixed(1)} px
              </span>
            </div>
            <div className="spread">
              <span className="label">Handedness</span>
              <span className={`mono ${(progress.handedness ?? 0) >= 0 ? "good" : "poor"}`}>
                {(progress.handedness ?? 0) >= 0 ? "north anticlockwise" : "mirrored"}
              </span>
            </div>
            <p className="small faint" style={{ margin: 0 }}>
              The two arrows should be about a right angle apart. Mirrored means declination
              corrections would be applied the wrong way round.
            </p>
          </div>
        </div>
      )}

      {progress?.ra_rate_arcsec_per_s != null && (
        <div className="spread">
          <Field label="RA rate" value={`${progress.ra_rate_arcsec_per_s.toFixed(2)}"/s`} />
          <Field label="Dec rate" value={`${progress.dec_rate_arcsec_per_s?.toFixed(2)}"/s`} />
          <Field
            label="Camera angle"
            value={progress.angle_deg == null ? "--" : `${progress.angle_deg.toFixed(0)}°`}
          />
          <Field
            label="Moved"
            value={
              progress.ra_shift_px == null
                ? "--"
                : `${progress.ra_shift_px.toFixed(0)}/${progress.dec_shift_px?.toFixed(0)} px`
            }
          />
        </div>
      )}

      {phase === "failed" && <div className="error">{progress?.message}</div>}
      <ErrorNote error={stop.error} />

      <div className="row modal-actions">
        <button className="ghost" onClick={onClose}>
          Hide
        </button>
        {state === "stopped" || state === "error" ? (
          <button className="primary" onClick={onClose}>
            Done
          </button>
        ) : (
          <button className="ghost danger" disabled={stop.isPending} onClick={() => stop.mutate()}>
            Stop
          </button>
        )}
      </div>
    </Modal>
  );
}

/** What the leg under way has to say for itself. */
function legNote(progress: Telemetry["guideProgress"]): string {
  if (progress == null) return "";
  if (progress.shift_px != null) return `moved ${progress.shift_px.toFixed(1)} px`;
  if (progress.pulse != null && progress.pulse_ms != null) {
    return `${progress.pulse_ms} ms per pulse`;
  }
  if (progress.exposure_s != null) return `${progress.exposure_s}s frames`;
  return "";
}
