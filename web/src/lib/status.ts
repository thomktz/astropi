/**
 * One notion of "how is this subsystem doing", shared by the top bar and
 * the rail.
 *
 * They were deciding separately, and drifted: the bar showed tracking as a
 * red dot the moment the mount stopped tracking, while the rail quietly
 * showed nothing at all. Anything with a dot in both places has to agree,
 * or the dots stop meaning anything.
 */

export type Health =
  /** Doing its job. */
  | "ok"
  /** Working towards it - slewing, settling, exposing. */
  | "working"
  /** Should be running and is not. */
  | "off"
  /** Deliberately not running; nothing is expected of it. */
  | "idle";

export function mountHealth(
  mount: { state: string; tracking: boolean } | null | undefined,
): Health {
  if (!mount) return "idle";
  if (mount.state === "slewing") return "working";
  if (mount.tracking) return "ok";
  // Not tracking is off, parked included. Parked was grey at first, on the
  // grounds that a stowed rig is not a fault - but it also meant the mount
  // showed no indicator at all for most of the evening, which made the
  // absence of a dot ambiguous between "fine" and "not connected".
  return "off";
}

export function guidingHealth(state: string): Health {
  if (state === "guiding") return "ok";
  if (state === "stopped" || state === "lost" || state === "error") return "off";
  return "working";
}

export function cameraHealth(state: string | undefined): Health {
  if (state === "error") return "off";
  if (state === "exposing" || state === "reading" || state === "downloading") return "working";
  // Between exposures is a camera's resting state, not a fault.
  return "idle";
}

/** Class for the small round indicator in the top bar. */
export function dotClass(health: Health): string {
  return { ok: "live", working: "busy", off: "down", idle: "" }[health];
}

/** Class for the rail badge, or null where a badge would be noise. */
export function badgeClass(health: Health): "busy" | "good" | "warn" | null {
  return { ok: "good", working: "busy", off: "warn", idle: null }[health] as
    | "busy"
    | "good"
    | "warn"
    | null;
}
