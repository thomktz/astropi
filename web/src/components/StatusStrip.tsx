import { useEffect, useState } from "react";
import { arcsec, bearing, degrees } from "../lib/format";
import type { Telemetry } from "../lib/useTelemetry";
import { GalaxyMark } from "./GalaxyMark";
import { MoonIcon } from "./Icons";
import { Sparkline } from "./Sparkline";

/** Total RMS above this is worth noticing at a glance. */
const RMS_WARN_ARCSEC = 1.5;
/** Ceiling for the RMS sparkline, so its shape means the same thing each time. */
const RMS_SPARK_MAX = 3.0;

/**
 * The strip that never goes away.
 *
 * With one panel open at a time, this is the only thing guaranteeing that a
 * slew, a failing guide star or a running sequence cannot hide behind
 * whatever drawer happens to be in front.
 */
export function StatusStrip({
  telemetry,
  night,
  onToggleNight,
}: {
  telemetry: Telemetry;
  night: boolean;
  onToggleNight: () => void;
}) {
  const { connected, mount, task, camera } = telemetry;
  const running = task?.state === "running";

  const errors = telemetry.guideSamples.map((sample) =>
    Math.hypot(sample.ra_error_arcsec, sample.dec_error_arcsec),
  );
  const rms = errors.length
    ? Math.sqrt(errors.reduce((total, value) => total + value * value, 0) / errors.length)
    : null;

  return (
    <header className="strip">
      <span className="brand">
        <GalaxyMark size={17} />
        astropi
      </span>

      <span className="pill" title={connected ? "Live telemetry" : "Trying to reconnect"}>
        <span className={`dot ${connected ? "live" : "down"}`} />
        <span className="hide-narrow">{connected ? "connected" : "reconnecting"}</span>
      </span>

      {mount && (
        <span className="pill">
          <span
            className={`dot ${mount.state === "slewing" ? "busy" : mount.tracking ? "live" : ""}`}
          />
          {mount.state}
          <span className="mono faint hide-narrow">
            {" "}
            {degrees(mount.alt_deg, 0)} / {bearing(mount.az_deg)}
          </span>
        </span>
      )}

      {rms != null && (
        <span
          className={`pill ${rms > RMS_WARN_ARCSEC ? "fair" : "good"}`}
          title={`Guiding RMS over the last ${errors.length} samples`}
        >
          <Sparkline values={errors.slice(-40)} max={RMS_SPARK_MAX} />
          {arcsec(rms, 2)}
        </span>
      )}

      {camera && <ExposureCountdown camera={camera} />}
      {running && task && <TaskProgress task={task} />}

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
    if (camera.state === "reading" || camera.state === "downloading") {
      return (
        <span className="pill">
          <span className="dot busy" />
          {camera.state}
        </span>
      );
    }
    return null;
  }

  const elapsed = now - camera.exposure_started_at;
  const remaining = Math.max(0, camera.exposure_s - elapsed);
  const fraction = Math.min(1, elapsed / Math.max(camera.exposure_s, 1e-6));

  return (
    <span className="pill countdown">
      <span className="dot busy" />
      <span className="mono">{remaining < 10 ? remaining.toFixed(1) : Math.ceil(remaining)}s</span>
      <span className="mini-bar" aria-hidden="true">
        <span style={{ width: `${fraction * 100}%` }} />
      </span>
    </span>
  );
}

function TaskProgress({ task }: { task: NonNullable<Telemetry["task"]> }) {
  const frame = task.detail.frame as number | undefined;
  const total = task.detail.total as number | undefined;

  return (
    <span className="pill task">
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
