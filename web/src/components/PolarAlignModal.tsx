import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { arcmin, formatDmsShort, formatHmsShort } from "../lib/format";
import { polarApi, type PolarShot, type PolarState, type ShotSettings } from "../lib/polar";
import type { Telemetry } from "../lib/useTelemetry";
import { ErrorNote, Field, Section } from "./Field";
import { Modal } from "./Modal";
import { NudgePad } from "./NudgePad";
import "./PolarAlignModal.css";

/** Bigger steps than the Target panel's: this move is tens of degrees. */
const STEPS = [
  { degrees: 1, label: "1°" },
  { degrees: 5, label: "5°" },
  { degrees: 10, label: "10°" },
  { degrees: 20, label: "20°" },
  { degrees: 30, label: "30°" },
];
const RECOMMENDED_MOVE_DEG = 20;
const MIN_MOVE_DEG = 5;
const EXCELLENT_ARCMIN = 2;
const SETTINGS_KEY = "astropi.polar2.settings";

/**
 * Polar alignment through a window, in the spirit of ASIAIR's.
 *
 * Take a frame, check it solved, nudge the mount to the other side of
 * the window, take another. The two solves and how far the mount says it
 * turned give the polar axis; from then on every new frame is solved
 * against that answer and the knob corrections are drawn on it, so you
 * turn until the arrows shrink to the ring.
 *
 * The mount only ever moves when a nudge button is pressed.
 */
export function PolarAlignModal({
  telemetry,
  busy,
  onClose,
}: {
  telemetry: Telemetry;
  busy: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const state = useQuery({ queryKey: ["polar2"], queryFn: polarApi.state });
  const [settings, setSettings] = useState<ShotSettings>(readSettings);
  const [shot, setShot] = useState<PolarShot | null>(null);
  const [step, setStep] = useState(20);
  const [live, setLive] = useState(false);
  const [stretch, setStretch] = useState(true);
  // The mount's hour angle when frame 1 was kept, so the move can be read
  // off as it happens rather than only after the next frame.
  const [firstHourAngle, setFirstHourAngle] = useState<number | null>(null);

  useEffect(() => {
    try {
      localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
    } catch {
      // Remembered settings are a convenience only.
    }
  }, [settings]);

  const stored = state.data?.step ?? "first";
  const setState = (next: PolarState) => queryClient.setQueryData(["polar2"], next);

  const capture = useMutation({
    mutationFn: () => polarApi.capture(settings),
    onSuccess: setShot,
  });
  const accept = useMutation({
    mutationFn: (id: string) => polarApi.accept(id),
    onSuccess: (next) => {
      setState(next);
      if (next.frames.length === 1) setFirstHourAngle(telemetry.mount?.hour_angle_deg ?? null);
      // The frame stays on screen, but it has been used: no second accept.
      setShot((current) => (current ? { ...current, id: "" } : current));
    },
  });
  const dropLast = useMutation({
    mutationFn: polarApi.dropLast,
    onSuccess: (next) => {
      setState(next);
      if (next.frames.length === 0) setFirstHourAngle(null);
    },
  });
  const reset = useMutation({
    mutationFn: polarApi.reset,
    onSuccess: (next) => {
      setState(next);
      setShot(null);
      setLive(false);
      setFirstHourAngle(null);
    },
  });

  // The live loop: one frame after another for as long as it is on. A
  // ref carries the latest settings in without restarting the loop.
  const settingsRef = useRef(settings);
  useEffect(() => {
    settingsRef.current = settings;
  });
  const [liveError, setLiveError] = useState<unknown>(null);
  // For the status line: a request is out, and when the last one landed.
  const [inFlight, setInFlight] = useState(false);
  const [lastUpdate, setLastUpdate] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!live) return;
    const timer = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(timer);
  }, [live]);
  useEffect(() => {
    if (!live) return;
    let stopped = false;
    const run = async () => {
      while (!stopped) {
        try {
          setInFlight(true);
          const next = await polarApi.live(settingsRef.current);
          setInFlight(false);
          if (stopped) return;
          setShot(next);
          setLastUpdate(Date.now());
          setLiveError(null);
          // The first live frame moves the session on to adjusting.
          queryClient.invalidateQueries({ queryKey: ["polar2"] });
        } catch (error) {
          setInFlight(false);
          if (stopped) return;
          setLiveError(error);
          await new Promise((resolve) => setTimeout(resolve, 2000));
        }
      }
    };
    run();
    return () => {
      stopped = true;
      setInFlight(false);
    };
  }, [live, queryClient]);

  const status = liveStatus(live, inFlight, telemetry.camera, now, lastUpdate, shot);

  // Adjusting from the moment it is asked for, not from the first answer.
  const phase = live ? "live" : stored;
  const mount = telemetry.mount;
  const moved =
    phase === "more" && firstHourAngle != null && mount?.hour_angle_deg != null
      ? wrap(mount.hour_angle_deg - firstHourAngle)
      : null;
  const working = capture.isPending || accept.isPending || dropLast.isPending || live;
  const kept = state.data?.frames.length ?? 0;
  const fit = state.data?.fit ?? null;
  const shown = shot?.error ?? null;

  return (
    <Modal
      size="full"
      title="Polar alignment"
      subtitle={subtitleFor(phase)}
      hint={
        <>
          Plate-solved frames taken wherever you put the mount - two at least, with a move in right
          ascension between them - give its rotation axis, wherever in the sky your window lets you
          look. After that, every new frame shows how far each knob still has to go. The mount only
          moves when you press a nudge button.
        </>
      }
      onClose={onClose}
    >
      <div className="polar2">
        <div className="polar2-view">
          <FrameView shot={shot} stretch={stretch} status={phase === "live" ? status : null} />
          <div className="row polar2-view-bar">
            <span className="small dim">{shotCaption(shot)}</span>
            <button
              className="ghost"
              aria-pressed={stretch}
              onClick={() => setStretch((on) => !on)}
              style={{ flex: "0 0 auto" }}
            >
              {stretch ? "stretched" : "linear"}
            </button>
          </div>
        </div>

        <div className="polar2-side">
          <Steps phase={phase} />

          <Section title="Exposure">
            <div className="row quick">
              <label className="polar2-input">
                <span className="label">Seconds</span>
                <input
                  type="number"
                  min={0.1}
                  max={60}
                  step={0.5}
                  value={settings.exposure_s}
                  onChange={(event) =>
                    setSettings({ ...settings, exposure_s: clamp(Number(event.target.value), 0.1, 60) })
                  }
                />
              </label>
              <label className="polar2-input">
                <span className="label">Gain</span>
                <input
                  type="number"
                  min={0}
                  max={1000}
                  placeholder="camera"
                  value={settings.gain ?? ""}
                  onChange={(event) =>
                    setSettings({
                      ...settings,
                      gain: event.target.value === "" ? null : clamp(Number(event.target.value), 0, 1000),
                    })
                  }
                />
              </label>
              <label className="polar2-input">
                <span className="label">Bin</span>
                <select
                  value={settings.binning}
                  onChange={(event) => setSettings({ ...settings, binning: Number(event.target.value) })}
                >
                  {[1, 2, 3, 4].map((b) => (
                    <option key={b} value={b}>
                      {b}×{b}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          </Section>

          {phase === "more" && (
            <Section title="Move">
              <p className="small dim polar2-prompt">
                Move however your window allows - east or west is what counts. Moving in RA alone keeps
                the answer clean.
              </p>
              <div className="spread">
                <Field
                  label="RA since frame 1"
                  value={moved == null ? "--" : `${Math.abs(moved).toFixed(1)}° ${moved >= 0 ? "W" : "E"}`}
                  tone={
                    moved == null
                      ? undefined
                      : Math.abs(moved) >= RECOMMENDED_MOVE_DEG
                        ? "good"
                        : Math.abs(moved) >= MIN_MOVE_DEG
                          ? "fair"
                          : "poor"
                  }
                />
                {mount && (
                  <Field
                    label="Mount"
                    value={
                      <span className="mono">
                        {formatHmsShort(mount.ra_deg)} {formatDmsShort(mount.dec_deg)}
                      </span>
                    }
                  />
                )}
              </div>
              <NudgePad
                steps={STEPS}
                step={step}
                onStep={setStep}
                parked={mount?.state === "parked"}
                disabled={busy || capture.isPending}
              />
            </Section>
          )}

          {phase !== "live" && (
            <Section title={`Frame ${kept + 1}`}>
              <p className="small dim polar2-prompt">
                {kept === 0
                  ? "Take a frame right where the mount is. Use it once it has solved."
                  : kept === 1
                    ? `After moving, take the next frame. ${RECOMMENDED_MOVE_DEG}° or more of RA in all gives the steadiest answer.`
                    : "Another frame is optional - it averages in and checks the others agree."}
              </p>
              <div className="row">
                <button
                  className={shot?.id && shot.solved ? undefined : "primary"}
                  disabled={busy || working}
                  onClick={() => capture.mutate()}
                >
                  {capture.isPending ? "Exposing…" : shot?.id ? "Retake" : "Take frame"}
                </button>
                <button
                  className="primary"
                  disabled={busy || working || !shot?.id || !shot.solved || tooShort(kept, shot)}
                  onClick={() => shot && accept.mutate(shot.id)}
                  title={tooShort(kept, shot) ? `Move at least ${MIN_MOVE_DEG}° in RA first` : undefined}
                >
                  {accept.isPending ? "Measuring…" : "Use this frame"}
                </button>
              </div>
              {shot?.solve_error && <div className="error">{shot.solve_error}</div>}
              <ErrorNote error={capture.error ?? accept.error ?? dropLast.error} />
            </Section>
          )}

          {phase === "more" && (
            <Section title={fit ? `Measured from ${fit.frames} frames` : "Measurement"}>
              {fit ? (
                <>
                  <div className="spread">
                    <Field label="Total" value={arcmin(fit.total_error_arcmin)} tone={tone(fit.total_error_arcmin)} />
                    <Field label="Altitude" value={arcmin(fit.altitude_error_arcmin)} />
                    <Field label="Azimuth" value={arcmin(fit.azimuth_error_arcmin)} />
                  </div>
                  <div className="small dim">
                    Over {fit.rotation_deg.toFixed(0)}° of RA, ±{fit.uncertainty_arcmin.toFixed(1)}' from solve
                    noise{fit.frames > 2 ? `, frames agree to ${fit.residual_arcsec.toFixed(0)}"` : ""}.
                  </div>
                  {fit.warnings.map((warning) => (
                    <div key={warning} className="small polar2-warning">
                      {warning}
                    </div>
                  ))}
                </>
              ) : (
                <div className="small dim">
                  {kept === 0 ? "No frames kept yet." : "1 frame kept - the math needs one more, after a move."}
                </div>
              )}
              <div className="row">
                <button className="ghost" disabled={kept === 0 || working} onClick={() => dropLast.mutate()}>
                  Undo frame {kept || ""}
                </button>
                <button className="primary" disabled={!fit || working} onClick={() => setLive(true)}>
                  Adjust the knobs
                </button>
              </div>
            </Section>
          )}

          {phase === "live" && (
            <Section title="Adjust">
              <div className="spread">
                <Field
                  label="Total"
                  value={arcmin(shown?.total_error_arcmin ?? fit?.total_error_arcmin)}
                  tone={tone(shown?.total_error_arcmin ?? fit?.total_error_arcmin)}
                />
                <Field label="Altitude" value={arcmin(shown?.altitude_error_arcmin ?? fit?.altitude_error_arcmin)} />
                <Field label="Azimuth" value={arcmin(shown?.azimuth_error_arcmin ?? fit?.azimuth_error_arcmin)} />
              </div>
              {shown && (
                <ul className="instructions">
                  {shown.instructions.map((line) => (
                    <li
                      key={line}
                      className={line.startsWith("Altitude") ? "polar2-alt" : line.startsWith("Azimuth") ? "polar2-az" : undefined}
                    >
                      {line}
                    </li>
                  ))}
                </ul>
              )}
              {fit && (
                <div className="small dim">
                  Measured from {fit.frames} frames over {fit.rotation_deg.toFixed(0)}° of RA, ±
                  {fit.uncertainty_arcmin.toFixed(1)}' from solve noise.
                </div>
              )}
              {fit?.warnings.map((warning) => (
                <div key={warning} className="small polar2-warning">
                  {warning}
                </div>
              ))}
              <div className="row">
                <button className={live ? undefined : "primary"} onClick={() => setLive((on) => !on)}>
                  {live ? "Pause" : "Resume live"}
                </button>
                <button
                  className="ghost"
                  onClick={() => polarApi.repin()}
                  title="Pin the start point and target again on the next frame"
                >
                  Re-centre
                </button>
              </div>
              <div className="small dim">
                Turn the knobs until the white cross sits in the green ring - altitude first, to the blue dot.
                The markers move each time a new frame solves.
              </div>
              <ErrorNote error={liveError} />
            </Section>
          )}

          <div className="row modal-actions">
            {phase !== "first" && (
              <button className="ghost" onClick={() => reset.mutate()} disabled={reset.isPending}>
                Start over
              </button>
            )}
            <button onClick={onClose}>Close</button>
          </div>
        </div>
      </div>
    </Modal>
  );
}

function Steps({ phase }: { phase: string }) {
  const index = phase === "first" ? 0 : phase === "more" ? 1 : 2;
  const labels = ["Frame where it stands", "Move, more frames", "Adjust knobs"];
  return (
    <ol className="polar2-steps">
      {labels.map((label, i) => (
        <li key={label} data-state={i < index ? "done" : i === index ? "current" : "todo"}>
          <span className="polar2-step-number">{i < index ? "✓" : i + 1}</span>
          {label}
        </li>
      ))}
    </ol>
  );
}

/**
 * The frame, with the correction drawn on it in the frame's own pixels.
 *
 * Image and drawing share one SVG viewBox, so the arrows land on the same
 * stars at any window size.
 */
function FrameView({
  shot,
  stretch,
  status,
}: {
  shot: PolarShot | null;
  stretch: boolean;
  status: { label: string; tone: string; detail: string } | null;
}) {
  const badge = status && (
    <div className={`polar2-status ${status.tone}`}>
      <span className="polar2-status-dot" />
      <span>{status.label}</span>
      {status.detail && <span className="dim">{status.detail}</span>}
    </div>
  );
  if (!shot) {
    return (
      <div className="polar2-frame polar2-empty">
        {badge}
        <span className="dim small">No frame yet</span>
      </div>
    );
  }
  const { width, height } = shot;
  const overlay = shot.overlay;
  const unit = Math.max(width, height) / 100;
  const at = (p: [number, number]) => [p[0] * width, p[1] * height] as const;

  return (
    <div className="polar2-frame">
      {badge}
      <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="xMidYMid meet" role="img">
        <title>Latest polar alignment frame</title>
        <defs>
          <marker id="polar2-head-alt" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" className="polar2-alt-fill" />
          </marker>
          <marker id="polar2-head-az" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" className="polar2-az-fill" />
          </marker>
        </defs>
        <image
          href={api.camera.previewUrl(shot.frame_id, { stretch, maxDimension: 1800 })}
          width={width}
          height={height}
          preserveAspectRatio="none"
        />
        {!overlay && (
          <circle cx={width / 2} cy={height / 2} r={unit * 1.2} className="polar2-now" strokeWidth={unit * 0.25} />
        )}
        {overlay && (
          <>
            {/* The route, fixed: altitude knob first, then azimuth. */}
            <polyline
              points={[overlay.start, overlay.after_altitude, overlay.target].map((p) => at(p).join(",")).join(" ")}
              fill="none"
              className="polar2-route"
              strokeWidth={unit * 0.2}
              strokeDasharray={`${unit} ${unit * 0.8}`}
            />
            <circle
              cx={at(overlay.after_altitude)[0]}
              cy={at(overlay.after_altitude)[1]}
              r={unit * 0.6}
              className="polar2-alt-fill"
            />
            {/* Where you started. */}
            <circle
              cx={at(overlay.start)[0]}
              cy={at(overlay.start)[1]}
              r={unit * 0.9}
              className="polar2-start"
              strokeWidth={unit * 0.25}
            />
            {/* Where to go. */}
            <circle
              cx={at(overlay.target)[0]}
              cy={at(overlay.target)[1]}
              r={unit * 2.2}
              className="polar2-target"
              strokeWidth={unit * 0.35}
            />
            {/* Where you are now: the start point, carried by the knobs. */}
            {overlay.current && (
              <>
                <line
                  x1={at(overlay.start)[0]}
                  y1={at(overlay.start)[1]}
                  x2={at(overlay.current)[0]}
                  y2={at(overlay.current)[1]}
                  className="polar2-current-stroke"
                  strokeWidth={unit * 0.2}
                />
                <g transform={`translate(${at(overlay.current).join(",")})`} className="polar2-current-stroke">
                  <line x1={-unit * 1.6} x2={unit * 1.6} y1={0} y2={0} strokeWidth={unit * 0.35} />
                  <line y1={-unit * 1.6} y2={unit * 1.6} x1={0} x2={0} strokeWidth={unit * 0.35} />
                </g>
              </>
            )}
          </>
        )}
      </svg>
      {overlay && (offFrame(overlay.target) || (overlay.current != null && offFrame(overlay.current))) && (
        <div className="polar2-offframe small">
          A marker is off this frame - turn toward it, or press Re-centre to pin them here again.
        </div>
      )}
    </div>
  );
}

function offFrame(p: [number, number]): boolean {
  return p[0] < 0 || p[0] > 1 || p[1] < 0 || p[1] > 1;
}

/** The second frame has to be a real move from the first; later ones need not be. */
function tooShort(kept: number, shot: PolarShot | null): boolean {
  return kept === 1 && shot?.moved_deg != null && Math.abs(shot.moved_deg) < MIN_MOVE_DEG;
}

function shotCaption(shot: PolarShot | null): string {
  if (!shot) return "Take a frame to begin.";
  if (!shot.solved) return "Did not solve - try a longer exposure or more gain.";
  const s = shot.solved;
  const parts = [
    `${formatHmsShort(s.ra_deg)} ${formatDmsShort(s.dec_deg)}`,
    `${s.stars} stars`,
    `${s.pixel_scale_arcsec.toFixed(2)}"/px`,
    `solved by ${s.solver} in ${s.solve_time_s.toFixed(1)}s`,
  ];
  if (shot.moved_deg != null) parts.push(`${Math.abs(shot.moved_deg).toFixed(1)}° from frame 1`);
  return parts.join(" · ");
}

function subtitleFor(phase: string): string {
  if (phase === "first") return "Step 1 of 3 - a frame where the mount stands";
  if (phase === "more") return "Step 2 of 3 - move how you like, then more frames";
  return "Step 3 of 3 - turn the knobs";
}

function tone(value: number | null | undefined): string | undefined {
  if (value == null) return undefined;
  return value < EXCELLENT_ARCMIN ? "good" : value < 10 ? "fair" : "poor";
}

function wrap(degrees: number): number {
  return ((((degrees + 180) % 360) + 360) % 360) - 180;
}

function clamp(value: number, low: number, high: number): number {
  if (!Number.isFinite(value)) return low;
  return Math.min(high, Math.max(low, value));
}

function readSettings(): ShotSettings {
  const fallback: ShotSettings = { exposure_s: 2, gain: null, binning: 2 };
  try {
    const stored = JSON.parse(localStorage.getItem(SETTINGS_KEY) ?? "null");
    if (stored && typeof stored.exposure_s === "number" && typeof stored.binning === "number") {
      return { ...fallback, ...stored };
    }
  } catch {
    // Fall through to the defaults.
  }
  return fallback;
}

/**
 * What the live loop is doing right now, for someone at the knobs who
 * cannot tell a slow solve from a stalled one by looking at the picture.
 */
function liveStatus(
  live: boolean,
  inFlight: boolean,
  camera: Telemetry["camera"],
  now: number,
  lastUpdate: number | null,
  shot: PolarShot | null,
): { label: string; tone: string; detail: string } {
  const age = lastUpdate == null ? "" : `updated ${Math.max(0, Math.round((now - lastUpdate) / 1000))}s ago`;
  if (!live) return { label: "Paused", tone: "idle", detail: age };
  if (!inFlight) return { label: "Starting next photo", tone: "busy", detail: age };
  const state = camera?.state ?? "idle";
  if (state === "exposing" && camera?.exposure_started_at != null && camera.exposure_s != null) {
    const left = Math.max(0, camera.exposure_s - (now / 1000 - camera.exposure_started_at));
    return { label: "Taking photo", tone: "busy", detail: `${left.toFixed(0)}s left` };
  }
  if (state === "exposing") return { label: "Taking photo", tone: "busy", detail: "" };
  if (state === "reading" || state === "downloading") {
    return { label: "Downloading photo", tone: "busy", detail: "" };
  }
  const last = shot?.solved?.solve_time_s;
  return {
    label: "Plate solving",
    tone: "solving",
    detail: last != null ? `last took ${last.toFixed(0)}s` : "",
  };
}
