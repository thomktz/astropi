import type { Telemetry } from "../lib/useTelemetry";

/**
 * The always-visible summary: is the link up, where is the mount, is
 * anything running. Everything here should be readable at arm's length.
 */
export function StatusBar({
  telemetry,
  night,
  onToggleNight,
}: {
  telemetry: Telemetry;
  night: boolean;
  onToggleNight: () => void;
}) {
  const { connected, mount, task, system } = telemetry;
  const running = task?.state === "running";

  return (
    <div className="statusbar">
      <span className="brand">astropi</span>

      <span className="pill">
        <span className={`dot ${connected ? "live" : "down"}`} />
        {connected ? "connected" : "reconnecting"}
      </span>

      {mount && (
        <>
          <span className="pill">
            <span className={`dot ${mount.state === "slewing" ? "busy" : mount.tracking ? "live" : ""}`} />
            {mount.state}
          </span>
          <span className="mono small dim">
            {mount.alt_deg != null && `alt ${mount.alt_deg.toFixed(1)}°`}
            {mount.az_deg != null && ` · az ${mount.az_deg.toFixed(1)}°`}
          </span>
        </>
      )}

      {running && task && (
        <span className="pill">
          <span className="dot busy" />
          {task.name}
          {task.fraction != null && ` ${Math.round(task.fraction * 100)}%`}
        </span>
      )}

      <span style={{ marginLeft: "auto" }} className="row">
        {system && <span className="small faint">{system.site.name}</span>}
        <button
          className="ghost"
          onClick={onToggleNight}
          aria-pressed={night}
          title="Red-only display, to protect dark adaptation"
        >
          {night ? "Night on" : "Night off"}
        </button>
      </span>
    </div>
  );
}
