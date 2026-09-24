import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { api } from "../../lib/api";
import { arcsec } from "../../lib/format";
import { guideStats } from "../../lib/guidestats";
import type { GuideSample, GuidingStatus } from "../../lib/types";
import type { Telemetry } from "../../lib/useTelemetry";
import { GuideChart } from "../GuideChart";
import { GuideTarget } from "../GuideTarget";
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
    // Kept polling even when the tab is in the background: this is a
    // panel someone leaves open on a second screen while they do
    // something else, and a frozen readout on a rig is worse than the
    // traffic of one request every four seconds.
    refetchIntervalInBackground: true,
    retry: false,
  });

  // A finished calibration changes what this panel shows completely, and
  // waiting out the poll left it claiming there was no calibrated rate
  // underneath corrections that were being computed from one.
  const calibrationPhase = telemetry.guideProgress?.phase;
  useEffect(() => {
    if (calibrationPhase === "calibrated" || calibrationPhase === "failed") {
      queryClient.invalidateQueries({ queryKey: ["guiding"] });
    }
  }, [calibrationPhase, queryClient]);

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
  const stats = guideStats(telemetry.guideSamples);

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

      {/*
        The trace and the bullseye side by side, as every other guider
        shows them: one says how big the errors are over time, the other
        what shape they make. A cloud stretched along one axis is that
        axis misbehaving; a cloud off-centre is a standing offset.
      */}
      <div className="guide-plots">
        <GuideChart samples={telemetry.guideSamples} />
        <GuideTarget samples={telemetry.guideSamples} />
      </div>

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
        The last frame, as a chain rather than as four numbers: what was
        measured, what it was divided by, what fraction of it was asked
        for, and what actually went to the mount - including "nothing,
        and here is why", which is a decision the loop makes constantly
        and never used to mention.
      */}
      <ThisFrame latest={latest} calibration={status.data?.calibration ?? null} />

      {/*
        The statistics the rest of the world quotes, so a number from
        here can be compared with a number from anywhere else.
      */}
      <div className="spread">
        <Field label="Peak RA" value={arcsec(stats.peakRa, 2)} />
        <Field label="Peak Dec" value={arcsec(stats.peakDec, 2)} />
        <Field
          label="RA oscillation"
          value={stats.raOscillation == null ? "--" : stats.raOscillation.toFixed(2)}
          tone={stats.raOscillation != null && stats.raOscillation > 0.6 ? "fair" : undefined}
        />
        <Field
          label="Dec drift"
          value={stats.driftDec == null ? "--" : `${stats.driftDec.toFixed(2)}"/min`}
          tone={stats.driftDec != null && Math.abs(stats.driftDec) > 1 ? "fair" : undefined}
        />
      </div>

      <div className="spread">
        <Field
          label="RMS, all frames"
          value={arcsec(rms)}
          tone={rms != null && rms < GOOD_RMS_ARCSEC ? "good" : "fair"}
        />
        <Field label="RMS in RA" value={arcsec(status.data?.rms_ra_arcsec)} />
        <Field label="RMS in Dec" value={arcsec(status.data?.rms_dec_arcsec)} />
        <Field label="Frames" value={String(status.data?.samples ?? 0)} />
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
              label="RA, per second of pulse"
              value={`${status.data.calibration.ra_rate_arcsec_per_s.toFixed(2)}"`}
            />
            <Field
              label="Dec, per second"
              value={`${status.data.calibration.dec_rate_arcsec_per_s.toFixed(2)}"`}
            />
            <Field
              label="Camera rotation"
              value={`${status.data.calibration.angle_deg.toFixed(1)}\u00b0`}
            />
            <Field
              label="Measured at dec"
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

          <p className="small faint" style={{ margin: 0 }}>
            How far a star moves for one second of pulse, and how the sensor is turned relative
            to the mount&apos;s axes. The declination it was measured at matters: the RA rate
            falls off as the cosine of declination, so a calibration taken near the pole
            over-corrects everywhere else.
          </p>

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

      {stats.raOscillation != null && stats.raOscillation > 0.6 && (
        <div className="small fair">
          Right ascension corrections are reversing {(stats.raOscillation * 100).toFixed(0)}% of the
          time - the loop is overshooting and coming back. Lower the RA aggressiveness.
        </div>
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


/**
 * The last frame, as the chain that produced it.
 *
 * Four numbers in a row - error, error, milliseconds, milliseconds - do
 * not say which way anything went, what divided what, or why one of them
 * is often zero. This is the same arithmetic the loop does, written out.
 */
function ThisFrame({
  latest,
  calibration,
}: {
  latest: GuideSample | undefined;
  calibration: GuidingStatus["calibration"];
}) {
  if (!latest) {
    return <div className="small faint">No frame measured yet.</div>;
  }

  const rows = [
    {
      axis: "RA",
      error: latest.ra_error_arcsec,
      rate: calibration?.ra_rate_arcsec_per_s,
      ms: latest.ra_pulse_ms,
      direction: latest.ra_direction,
      withheld: latest.ra_withheld,
    },
    {
      axis: "Dec",
      error: latest.dec_error_arcsec,
      rate: calibration?.dec_rate_arcsec_per_s,
      ms: latest.dec_pulse_ms,
      direction: latest.dec_direction,
      withheld: latest.dec_withheld,
    },
  ];

  return (
    <div className="this-frame">
      <div className="label">This frame</div>
      {rows.map((row) => (
        <div key={row.axis} className="frame-row small">
          <span className="frame-axis">{row.axis}</span>
          <span className="mono">{arcsec(row.error, 2)} off</span>
          <span className="faint">
            {row.rate ? `\u00f7 ${row.rate.toFixed(1)}"/s` : "no rate"}
          </span>
          <span className={`mono ${row.ms > 0 ? "" : "faint"}`}>
            {row.ms > 0 ? `${row.ms.toFixed(0)} ms ${row.direction}` : "nothing sent"}
          </span>
        </div>
      ))}
      {(latest.ra_withheld || latest.dec_withheld) && (
        <div className="small faint">
          {[latest.ra_withheld && `RA: ${latest.ra_withheld}`,
            latest.dec_withheld && `Dec: ${latest.dec_withheld}`]
            .filter(Boolean)
            .join(" \u00b7 ")}
        </div>
      )}
    </div>
  );
}
