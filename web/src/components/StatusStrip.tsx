import { useEffect, useState } from "react";
import { altitudeQuality, arcsec, bearing, degrees } from "../lib/format";
import { cameraHealth, dotClass, guidingHealth, mountHealth } from "../lib/status";
import { useExposureFraction } from "../lib/useExposure";
import type { Telemetry } from "../lib/useTelemetry";
import type { DrawerId } from "./railEntries";
import { GalaxyMark } from "./GalaxyMark";
import {
  CameraErrorIcon,
  DownloadingIcon,
  ExposingIcon,
  IdleCameraIcon,
  MoonIcon,
  ReadingIcon,
} from "./Icons";
import { Sparkline } from "./Sparkline";

/** Total RMS above this is worth noticing at a glance. */
const RMS_WARN_ARCSEC = 1.5;
/** Ceiling for the RMS sparkline, so its shape means the same thing each time. */
const RMS_SPARK_MAX = 3.0;

/**
 * The bar that never goes away.
 *
 * With one panel open at a time this is the only guarantee that a slew, a
 * failing guide star, a running sequence or a target sinking toward the
 * horizon cannot hide behind whatever drawer happens to be in front.
 *
 * Every entry is a value with at most an icon. Words are dropped as the
 * screen narrows (`.hide-narrow`) rather than whole readouts, so a phone
 * shows the same numbers as a laptop, just tersely.
 */
export function StatusStrip({
  telemetry,
  night,
  onToggleNight,
  onOpen,
}: {
  telemetry: Telemetry;
  night: boolean;
  onToggleNight: () => void;
  onOpen: (drawer: DrawerId) => void;
}) {
  const { connected, mount, target, task, camera, guideCamera } = telemetry;
  const running = task?.state === "running";

  // A rig without the hardware should not be told its state.
  const hasCamera = telemetry.system?.devices?.camera != null;
  const hasGuideCamera = telemetry.system?.devices?.guide_camera != null;

  const errors = telemetry.guideSamples.map((sample) =>
    Math.hypot(sample.ra_error_arcsec, sample.dec_error_arcsec),
  );
  const rms = errors.length
    ? Math.sqrt(errors.reduce((total, value) => total + value * value, 0) / errors.length)
    : null;

  return (
    <header className="strip">
      {/*
        Connection rides on the brand rather than taking a pill of its own.
        It is a property of the whole application, not one more reading off
        the rig, and on a phone a pill holding a single dot is pure cost.
      */}
      <span
        className="brand"
        title={connected ? "Live telemetry" : "Trying to reconnect"}
      >
        <GalaxyMark size={17} />
        <span className={`dot ${connected ? "live" : "down"}`} />
        <span className="hide-narrow">astropi</span>
        <span className="sr-only">{connected ? "connected" : "reconnecting"}</span>
      </span>

      <div className="readouts">
        {/*
          What the rig is doing, in one order that never changes: the
          mount, the guide loop, the target. Each keeps a dot, so the
          question "is anything not running" is answered by colour alone
          without reading a word. The two sensors sit apart, at the right.
        */}
        {mount && (
          <button
            className="pill linked"
            onClick={() => onOpen("target")}
            title={
              mount.tracking
                ? "Turning at sidereal rate to cancel the earth's rotation"
                : "Not tracking - the sky drifts through the frame"
            }
          >
            <span className={`dot ${dotClass(mountHealth(mount))}`} />
            {mount.state}
            <span className="mono faint">
              {degrees(mount.alt_deg, 0)}/{bearing(mount.az_deg)}
            </span>
          </button>
        )}

        {hasGuideCamera && (
          <GuidePill
            state={telemetry.guideState}
            errors={errors}
            rms={rms}
            onOpen={() => onOpen("guiding")}
          />
        )}

        {target && (
          <button className="pill linked" onClick={() => onOpen("target")} title="Open the target panel">
            <span className="name hide-tight">{target.display_name}</span>
            <span className="name show-tight">{target.name}</span>
            <span className={`mono ${altitudeQuality(target.altitude_deg)}`}>
              {degrees(target.altitude_deg, 0)}
            </span>
          </button>
        )}

        {running && task && <TaskProgress task={task} />}

        {/*
          The cameras, grouped and pushed to the right end. They are the
          two things on this bar that are not a state of the observing
          session but a state of a device, and they are read together -
          "are both sensors alive" is one glance, not two.
        */}
        <span className="sensors">
          {hasCamera && (
            <SensorPill
              camera={camera}
              name="main"
              // The live view is a loop of its own; a capture is not, and
              // is the one thing this pill exists to announce.
              ambient={camera?.kind === "preview"}
              countdown
              onOpen={() => onOpen("camera")}
            />
          )}
          {/*
            The guide sensor is always on a loop of its own: idle previews
            while guiding is stopped, guide frames while it is not. Which
            of the two is running is the guiding pill's business, and it
            says so with a state and an RMS rather than with a shutter.
          */}
          {hasGuideCamera && (
            <SensorPill camera={guideCamera} name="guide" ambient onOpen={() => onOpen("guiding")} />
          )}
        </span>
      </div>

      <button
        className="ghost night-toggle"
        onClick={onToggleNight}
        aria-pressed={night}
        title="Red-only display, to protect dark adaptation"
      >
        <MoonIcon size={16} />
        <span className="hide-narrow">{night ? "Night on" : "Night off"}</span>
      </button>
    </header>
  );
}

/**
 * One sensor, always present rather than only while exposing.
 *
 * Idle is grey, not red: between exposures is a camera's normal resting
 * state, and colouring it as a fault would make the row meaningless.
 *
 * The action is an icon. Spelling it out meant the pill changed width
 * three times a frame and pushed the rest of the bar around, and the fix
 * for that - one fixed word - left it saying "live" for minutes on end
 * while the camera was visibly working. A glyph is a constant width, so
 * every phase can show itself.
 */
function SensorPill({
  camera,
  name,
  ambient,
  countdown,
  onOpen,
}: {
  camera: Telemetry["camera"];
  name: string;
  /**
   * The sensor is running its own background loop rather than doing
   * something asked of it. Both sensors spend most of the night here: the
   * main one on the live view, the guide one feeding the sub-display.
   */
  ambient: boolean;
  /**
   * Show the seconds left as well as the bar. Worth it for an imaging
   * exposure, which runs for minutes; not for the guide sensor, which
   * would spend all night counting down from two.
   */
  countdown?: boolean;
  onOpen: () => void;
}) {
  const state = camera?.state ?? "idle";
  const failed = state === "error";
  const { Icon, verb } = cameraAction(state);
  const working = state === "reading" || state === "downloading";
  const exposing = state === "exposing" && camera != null;

  return (
    <button
      className={`pill linked camera-pill ${failed ? "poor" : ""}`}
      onClick={onOpen}
      title={`${name} camera: ${verb}${ambient && !failed ? ", on its own loop" : ""}`}
    >
      {/*
        One vocabulary for both sensors, because they are doing the same
        thing. A steady live dot means a loop of its own is running - the
        live view, or the guide sub-display - and the amber busy dot is
        kept for work that was actually asked for, which on the main
        sensor means a capture. Green for one and flashing amber for the
        other, for the same activity, told you nothing except that two
        people wrote the two pills.
      */}
      <span className={`dot ${failed ? "down" : ambient ? "live" : dotClass(cameraHealth(state))}`} />
      <span className="sensor-name hide-narrow">{name}</span>
      <Icon size={14} />
      <span className="sr-only">{verb}</span>
      {exposing && camera && countdown && !ambient ? (
        <ExposureCountdown camera={camera} />
      ) : exposing ? (
        <ExposureProgress camera={camera} />
      ) : working ? (
        // Nothing to count: readout and download have no reported
        // duration, so the bar runs on its own rather than sitting empty.
        <span className="mini-bar indeterminate" aria-hidden="true">
          <span />
        </span>
      ) : (
        <span className="mini-bar" aria-hidden="true">
          <span style={{ width: 0 }} />
        </span>
      )}
    </button>
  );
}

/** One glyph and one word per camera state, so both agree. */
function cameraAction(state: string): { Icon: typeof ExposingIcon; verb: string } {
  switch (state) {
    case "exposing":
      return { Icon: ExposingIcon, verb: "exposing" };
    case "reading":
      return { Icon: ReadingIcon, verb: "reading out" };
    case "downloading":
      return { Icon: DownloadingIcon, verb: "downloading" };
    case "error":
      return { Icon: CameraErrorIcon, verb: "error" };
    default:
      return { Icon: IdleCameraIcon, verb: "idle" };
  }
}

/**
 * Guiding state, kept distinct from tracking.
 *
 * Tracking is the mount turning at a constant rate to cancel the earth's
 * rotation. Guiding is a closed loop watching a star and correcting what
 * tracking got wrong - polar misalignment, periodic error, flexure. They
 * fail independently, and one readout covering both would hide which of the
 * two is the reason the stars are trailing.
 *
 * Shown even when stopped, because during an imaging run "not guiding" is
 * information, not an absence of it.
 */
function GuidePill({
  state,
  errors,
  rms,
  onOpen,
}: {
  state: string;
  errors: number[];
  rms: number | null;
  onOpen: () => void;
}) {
  const guiding = state === "guiding";
  const health = guidingHealth(state);
  // Off is red, stopped included. Unguided imaging is a choice some
  // nights, so soften this in `guidingHealth` if the alarm gets tiresome.
  const tone = health === "off" ? "poor" : guiding && rms != null && rms <= RMS_WARN_ARCSEC ? "good" : "";

  return (
    <button className={`pill linked ${tone}`} onClick={onOpen} title={`Guiding: ${state}`}>
      <span className={`dot ${dotClass(health)}`} />
      {/*
        Never hidden. "settling", "calibrating" and "lost" are the state
        itself, not a word describing a number beside it, and a bare dot
        cannot say which of them you are looking at.
      */}
      <span>{state}</span>
      {errors.length > 1 && <Sparkline values={errors.slice(-40)} max={RMS_SPARK_MAX} />}
      {/*
        Dimmed when not actually guiding. The figure is still the real
        RMS of the samples collected, but beside the word "stopped" a
        bright number reads as a live measurement rather than the record
        of one that has ended.
      */}
      {rms != null && <span className={`mono ${guiding ? "" : "faint"}`}>{arcsec(rms, 2)}</span>}
    </button>
  );
}

/**
 * Seconds left on the exposure under way.
 *
 * Counted locally from the start time and duration the camera reported, so
 * a five-minute sub does not cost five minutes of progress messages to
 * every connected browser.
 */
/** Just the bar, for the live view, where the seconds are not the point. */
function ExposureProgress({ camera }: { camera: Telemetry["camera"] }) {
  const fraction = useExposureFraction(camera);
  return (
    <span className="mini-bar" aria-hidden="true">
      <span style={{ width: `${(fraction ?? 0) * 100}%` }} />
    </span>
  );
}

function ExposureCountdown({ camera }: { camera: NonNullable<Telemetry["camera"]> }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  const exposing = camera.state === "exposing" && camera.exposure_started_at != null;

  useEffect(() => {
    if (!exposing) return;
    const timer = setInterval(() => setNow(Date.now() / 1000), 250);
    return () => clearInterval(timer);
  }, [exposing]);

  if (!exposing || camera.exposure_s == null || camera.exposure_started_at == null) {
    return <>{camera.state}</>;
  }

  const elapsed = now - camera.exposure_started_at;
  const remaining = Math.max(0, camera.exposure_s - elapsed);
  const fraction = Math.min(1, elapsed / Math.max(camera.exposure_s, 1e-6));

  return (
    <>
      <span className="mono">{remaining < 10 ? remaining.toFixed(1) : Math.ceil(remaining)}s</span>
      <span className="mini-bar" aria-hidden="true">
        <span style={{ width: `${fraction * 100}%` }} />
      </span>
    </>
  );
}

function TaskProgress({ task }: { task: NonNullable<Telemetry["task"]> }) {
  const frame = task.detail.frame as number | undefined;
  const total = task.detail.total as number | undefined;

  return (
    <span className="pill task" title={`${task.name} - ${task.step}`}>
      <span className="dot busy" />
      <span className="hide-narrow">{task.name}</span>
      {frame != null && total != null && (
        <span className="mono">
          {frame}/{total}
        </span>
      )}
      {task.fraction != null && (
        <span className="mini-bar" aria-hidden="true">
          <span style={{ width: `${Math.round(task.fraction * 100)}%` }} />
        </span>
      )}
    </span>
  );
}
