import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import {
  altitudeQuality,
  arcsec,
  bearing,
  degrees,
  durationShort,
  formatDms,
  formatDmsShort,
  formatHms,
  formatHmsShort,
  hoursToMeridian,
  meridianIsMeaningful,
} from "../lib/format";
import type { Telemetry } from "../lib/useTelemetry";
import { GalaxyMark } from "./GalaxyMark";
import { DarknessIcon, MeridianIcon, MoonIcon } from "./Icons";
import { Sparkline } from "./Sparkline";

/** Total RMS above this is worth noticing at a glance. */
const RMS_WARN_ARCSEC = 1.5;
/** Ceiling for the RMS sparkline, so its shape means the same thing each time. */
const RMS_SPARK_MAX = 3.0;
/** Inside this long, a meridian crossing is close enough to plan around. */
const MERIDIAN_SOON_HOURS = 0.5;

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
  onOpenTarget,
}: {
  telemetry: Telemetry;
  night: boolean;
  onToggleNight: () => void;
  onOpenTarget: () => void;
}) {
  const { connected, mount, target, task, camera } = telemetry;
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
          The rig's three subsystems, always in the same order and always
          present: camera, tracking, guiding. Each keeps a dot, so the
          question "is anything not running" is answered by colour alone
          without reading a word.
        */}
        {hasCamera && <CameraPill camera={camera} />}

        {mount && (
          <span
            className="pill"
            title={
              mount.tracking
                ? "Turning at sidereal rate to cancel the earth's rotation"
                : mount.state === "parked"
                  ? "Parked - nothing is expected to be tracking"
                  : "Not tracking - the sky is drifting through the frame"
            }
          >
            <span className={`dot ${trackingDot(mount)}`} />
            {mount.state}
            <span className="mono faint">
              {degrees(mount.alt_deg, 0)}/{bearing(mount.az_deg)}
            </span>
          </span>
        )}

        {hasGuideCamera && <GuidePill state={telemetry.guideState} errors={errors} rms={rms} />}

        {target && (
          <button className="pill linked" onClick={onOpenTarget} title="Open the target panel">
            <span className="name hide-tight">{target.display_name}</span>
            <span className="name show-tight">{target.name}</span>
            <span className={`mono ${altitudeQuality(target.altitude_deg)}`}>
              {degrees(target.altitude_deg, 0)}
            </span>
          </button>
        )}

        {mount && (
          <span className="pill mono" title="Where the mount reports it is pointing">
            <span className="hide-tight">
              {formatHms(mount.ra_deg)} {formatDms(mount.dec_deg)}
            </span>
            <span className="show-tight">
              {formatHmsShort(mount.ra_deg)} {formatDmsShort(mount.dec_deg)}
            </span>
          </span>
        )}

        {mount?.hour_angle_deg != null && meridianIsMeaningful(mount.dec_deg) && (
          <MeridianPill hourAngleDeg={mount.hour_angle_deg} />
        )}

        <DarknessPill />

        {running && task && <TaskProgress task={task} />}
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
 * Tracking's indicator.
 *
 * Red when the mount is unparked and not tracking, because that means the
 * sky is drifting through the frame. Grey when parked: the rig is stowed
 * and nothing is expected to be running, so an alarm there would be the
 * kind of permanent red that teaches you to stop looking.
 */
function trackingDot(mount: NonNullable<Telemetry["mount"]>): string {
  if (mount.state === "slewing") return "busy";
  if (mount.tracking) return "live";
  return mount.state === "parked" ? "" : "down";
}

/**
 * The camera, always present rather than only while exposing.
 *
 * Idle is grey, not red: between exposures is a camera's normal resting
 * state, and colouring it as a fault would make the row meaningless.
 */
function CameraPill({ camera }: { camera: Telemetry["camera"] }) {
  const state = camera?.state ?? "idle";
  const busy = state === "exposing" || state === "reading" || state === "downloading";

  return (
    <span className={`pill ${state === "error" ? "poor" : ""}`} title={`Camera: ${state}`}>
      <span className={`dot ${state === "error" ? "down" : busy ? "busy" : ""}`} />
      {state === "exposing" && camera ? <ExposureCountdown camera={camera} /> : state}
    </span>
  );
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
}: {
  state: string;
  errors: number[];
  rms: number | null;
}) {
  const guiding = state === "guiding";
  const broken = state === "lost" || state === "error";
  // Stopped counts as off, and off is red. Unguided imaging is a choice
  // some nights, so soften this to grey here if the alarm gets tiresome.
  const off = state === "stopped";
  const tone = broken || off ? "poor" : guiding && rms != null && rms <= RMS_WARN_ARCSEC ? "good" : "";

  return (
    <span className={`pill ${tone}`} title={`Guiding: ${state}`}>
      <span className={`dot ${guiding ? "live" : broken || off ? "down" : "busy"}`} />
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
    </span>
  );
}

/**
 * Time to the meridian.
 *
 * The most actionable number during a run: it says when the mount has to
 * flip, or when tracking will start driving the optics into the tripod. It
 * cannot be worked out from anything else on the bar.
 *
 * Hidden near the pole, where hour angle is degenerate - see
 * `meridianIsMeaningful`.
 */
function MeridianPill({ hourAngleDeg }: { hourAngleDeg: number }) {
  const hours = hoursToMeridian(hourAngleDeg);
  const past = hours < 0;
  const magnitude = Math.abs(hours);
  const soon = !past && magnitude <= MERIDIAN_SOON_HOURS;

  return (
    <span
      className={`pill ${soon ? "fair" : ""}`}
      title={past ? "Past the meridian, tracking west" : "Time until the meridian"}
    >
      <MeridianIcon size={14} />
      <span className="hide-narrow">{past ? "past" : "meridian"}</span>
      <span className="mono">{durationShort(magnitude)}</span>
    </span>
  );
}

/**
 * Darkness left tonight.
 *
 * Session-level context: whether there is room for another forty-minute run
 * or it is time to start packing up.
 */
function DarknessPill() {
  const night = useQuery({ queryKey: ["night"], queryFn: api.night, staleTime: 10 * 60_000 });
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(timer);
  }, []);

  if (!night.data?.astronomical_dusk || !night.data.astronomical_dawn) return null;

  const dusk = new Date(night.data.astronomical_dusk).getTime();
  const dawn = new Date(night.data.astronomical_dawn).getTime();

  const dark = now >= dusk && now < dawn;
  const hours = (dark ? dawn - now : dusk - now) / 3_600_000;
  // Already past dawn: tonight's window is behind us and the next one is
  // tomorrow's, which this readout is not about.
  if (hours < 0) return null;

  return (
    <span
      className={`pill ${dark && hours < 1 ? "fair" : ""}`}
      title={dark ? "Darkness remaining until astronomical dawn" : "Time until astronomical dusk"}
    >
      <DarknessIcon size={14} />
      <span className="hide-narrow">{dark ? "dark left" : "dark in"}</span>
      <span className="mono">{durationShort(hours)}</span>
    </span>
  );
}

/**
 * Seconds left on the exposure under way.
 *
 * Counted locally from the start time and duration the camera reported, so
 * a five-minute sub does not cost five minutes of progress messages to
 * every connected browser.
 */
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
