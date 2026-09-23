import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { arcsec } from "../../lib/format";
import type { GuidingStatus } from "../../lib/types";
import type { Telemetry } from "../../lib/useTelemetry";
import { GuideChart } from "../GuideChart";
import { GuideSettings } from "../GuideSettings";
import { GuideView } from "../GuideView";
import { ErrorNote, Field, Section } from "../Field";

/** Which indicator a guiding state deserves: lost is a problem, not progress. */
function dotClass(state: string): string {
  if (state === "guiding") return "live";
  if (state === "lost" || state === "error") return "down";
  if (state === "stopped") return "";
  return "busy";
}

/** Total RMS below this is good enough that seeing, not tracking, limits you. */
const GOOD_RMS_ARCSEC = 1.0;

export function GuidingPanel({ telemetry }: { telemetry: Telemetry }) {
  const queryClient = useQueryClient();
  const status = useQuery({
    queryKey: ["guiding"],
    queryFn: api.guiding.status,
    refetchInterval: 4_000,
    retry: false,
  });

  const act = useMutation({
    mutationFn: (action: () => Promise<unknown>) => action(),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["guiding"] }),
  });

  if (status.isError) {
    return <div className="small faint">No guide camera connected.</div>;
  }

  const state = telemetry.guideState ?? status.data?.state ?? "stopped";
  const running = state !== "stopped" && state !== "error";
  const rms = status.data?.rms_total_arcsec ?? null;
  const latest = telemetry.guideSamples.at(-1);

  return (
    <>
      <div className="spread">
        <span className="pill">
          <span className={`dot ${dotClass(state)}`} />
          {state}
        </span>
        {latest && (
          <span className="small faint mono">
            SNR {latest.snr.toFixed(0)} &middot; HFD {latest.hfd.toFixed(1)}
          </span>
        )}
      </div>

      <GuideView latest={latest} running={running} />

      <GuideChart samples={telemetry.guideSamples} />

      {/*
        A frame that found no star at the lock point produces no sample,
        so every number above it stays exactly as it was. Without this
        line, a loop hunting for a star it has lost looks identical to a
        loop that has stopped running - which is what "nothing happening
        for a minute" turned out to be.
      */}
      {telemetry.guideProgress?.phase === "searching" && (
        <div className="small poor">{telemetry.guideProgress.message}</div>
      )}

      {/*
        What the last frame measured and what was sent because of it.
        The RMS figures below are the run; these are the moment, and an
        axis going wrong shows here first.
      */}
      <div className="spread">
        <Field
          label="RA now"
          value={latest == null ? "--" : arcsec(latest.ra_error_arcsec, 2)}
          tone={latest != null && Math.abs(latest.ra_error_arcsec) > 2 ? "fair" : undefined}
        />
        <Field
          label="Dec now"
          value={latest == null ? "--" : arcsec(latest.dec_error_arcsec, 2)}
          tone={latest != null && Math.abs(latest.dec_error_arcsec) > 2 ? "fair" : undefined}
        />
        <Field
          label="RA pulse"
          value={latest == null ? "--" : `${latest.ra_pulse_ms.toFixed(0)} ms`}
        />
        <Field
          label="Dec pulse"
          value={latest == null ? "--" : `${latest.dec_pulse_ms.toFixed(0)} ms`}
        />
      </div>

      <div className="spread">
        <Field label="RMS total" value={arcsec(rms)} tone={rms != null && rms < GOOD_RMS_ARCSEC ? "good" : "fair"} />
        <Field label="RA" value={arcsec(status.data?.rms_ra_arcsec)} />
        <Field label="Dec" value={arcsec(status.data?.rms_dec_arcsec)} />
        <Field label="Samples" value={String(status.data?.samples ?? 0)} />
      </div>

      <div className="row">
        {running ? (
          <button className="danger" onClick={() => act.mutate(api.guiding.stop)}>
            Stop
          </button>
        ) : (
          <button className="primary" onClick={() => act.mutate(api.guiding.start)}>
            {status.data?.calibrated ? "Start guiding" : "Calibrate & guide"}
          </button>
        )}
        <button onClick={() => act.mutate(() => api.guiding.dither(12))} disabled={state !== "guiding"}>
          Dither
        </button>
      </div>

      {status.data?.calibration && (
        <Section title="Calibration">
          <div className="spread">
            <Field
              label="RA rate"
              value={`${status.data.calibration.ra_rate_arcsec_per_s.toFixed(2)}"/s`}
            />
            <Field
              label="Dec rate"
              value={`${status.data.calibration.dec_rate_arcsec_per_s.toFixed(2)}"/s`}
            />
            <Field
              label="Camera angle"
              value={`${status.data.calibration.angle_deg.toFixed(1)}\u00b0`}
            />
            <Field
              label="At dec"
              value={`${status.data.calibration.dec_at_calibration_deg.toFixed(0)}\u00b0`}
            />
          </div>

          {/*
            What was measured, not only what was derived from it. A rate
            and an angle cannot answer "did declination come out
            perpendicular to right ascension, and which way round" - and
            a declination axis guiding backwards looks, from every other
            number here, exactly like a mount with bad backlash.
          */}
          <CalibrationVectors calibration={status.data.calibration} />

          <div className="spread">
            <span className="small faint">
              measured {new Date(status.data.calibration.calibrated_at * 1000).toLocaleTimeString()}
            </span>
            <button
              className="ghost"
              onClick={() => act.mutate(api.guiding.clearCalibration)}
              title="Rotating the camera or flipping the mount invalidates it"
            >
              Clear calibration
            </button>
          </div>
        </Section>
      )}

      <HowCorrectionsWork />

      <GuideSettings />

      <ErrorNote error={act.error} />
    </>
  );
}

/**
 * The two legs the calibration walked, as vectors on the sensor.
 *
 * The handedness is the part worth having: whether the star moved
 * anticlockwise from west to north or clockwise. That is the difference
 * between a declination correction that helps and one that doubles the
 * error every frame - and every other number on this panel looks
 * identical either way, which is exactly how a backwards declination
 * axis went unnoticed until the raw RMS figures were read by hand.
 */
function CalibrationVectors({
  calibration,
}: {
  calibration: NonNullable<GuidingStatus["calibration"]>;
}) {
  const west = calibration.west_shift_px;
  const north = calibration.north_shift_px;
  if (!west || !north) return null;

  const cross = west[0] * north[1] - west[1] * north[0];
  const dot = west[0] * north[0] + west[1] * north[1];
  // atan2 of the cross against the dot, not asin of the cross alone:
  // the sine cannot tell 80 degrees from 100, and those are opposite
  // sides of square.
  const between = (Math.atan2(Math.abs(cross), dot) * 180) / Math.PI;
  const square = Math.abs(between - 90) < 12;

  return (
    <div className="stack small">
      <div className="spread">
        <span className="label">Star moved west</span>
        <span className="mono">
          {west[0].toFixed(1)}, {west[1].toFixed(1)} px
        </span>
      </div>
      <div className="spread">
        <span className="label">Star moved north</span>
        <span className="mono">
          {north[0].toFixed(1)}, {north[1].toFixed(1)} px
        </span>
      </div>
      <div className="spread">
        <span className="label">Axes</span>
        <span className={`mono ${square ? "good" : "fair"}`}>
          {between.toFixed(0)}&#176; apart {cross >= 0 ? "(north anticlockwise)" : "(mirrored)"}
        </span>
      </div>
    </div>
  );
}

/**
 * What the loop actually does to the mount, in words.
 *
 * Every number on this panel is downstream of two decisions that were
 * nowhere on screen: how a correction is delivered, and how much of the
 * measured error is applied. Both change what the graph above means.
 */
function HowCorrectionsWork() {
  const settings = useQuery({ queryKey: ["guiding-settings"], queryFn: api.guiding.settings });
  const data = settings.data;

  return (
    <Section title="How corrections are made">
      <p className="small faint" style={{ margin: 0 }}>
        Right ascension is corrected by <strong>changing the tracking rate</strong> for the length
        of the pulse - the axis never stops, it just runs faster or slower than sidereal for a
        moment. Declination is a <strong>timed run</strong> of an axis that is otherwise still.
        Both are sized from the calibration: error &divide; rate &times; aggressiveness.
      </p>
      {data && (
        <div className="spread">
          <Field label="RA gain" value={`${(data.ra_aggressiveness * 100).toFixed(0)}%`} />
          <Field label="Dec gain" value={`${(data.dec_aggressiveness * 100).toFixed(0)}%`} />
          <Field label="Dead band" value={arcsec(data.min_move_arcsec, 2)} />
          <Field label="Pulse cap" value={`${data.max_pulse_ms} ms`} />
        </div>
      )}
      <p className="small faint" style={{ margin: 0 }}>
        No averaging between frames: each correction is a fraction of the error that one frame
        measured. The gains are what damps it - below 100% the loop deliberately under-corrects,
        because chasing seeing injects more motion than it removes - and errors inside the dead
        band are left alone.
      </p>
    </Section>
  );
}
