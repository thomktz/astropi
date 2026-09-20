/**
 * The rail's contents, and the drawer ids that go with them.
 *
 * Kept out of `Rail.tsx` so that file exports components only - a module
 * mixing components with constants breaks fast refresh during development.
 */
export type DrawerId =
  | "overview"
  | "target"
  | "camera"
  | "guiding"
  | "session"
  | "align"
  | "setup";

/**
 * Two groups, in the order a night is actually worked.
 *
 * `session` holds what gets touched repeatedly once imaging has started.
 * `rig` holds what is done once at the start and then left alone - polar
 * alignment and the site and device settings - so neither sits in the way
 * of the panels being used every few minutes.
 */
export const RAIL: { id: DrawerId; label: string; group: "session" | "rig" }[] = [
  { id: "overview", label: "Overview", group: "session" },
  // Target and mount are one panel: choosing where to point and driving
  // the mount there is a single job, and splitting them put the GoTo
  // button in a different place from the tracking state it depends on.
  { id: "target", label: "Target", group: "session" },
  { id: "camera", label: "Camera", group: "session" },
  { id: "guiding", label: "Guiding", group: "session" },
  { id: "session", label: "Session", group: "session" },
  { id: "align", label: "Polar align", group: "rig" },
  { id: "setup", label: "Setup", group: "rig" },
];
