/**
 * The rail's contents, and the drawer ids that go with them.
 *
 * Kept out of `Rail.tsx` so that file exports components only - a module
 * mixing components with constants breaks fast refresh during development.
 */
export type DrawerId = "target" | "mount" | "camera" | "guiding" | "align" | "session" | "setup";

/** Ordered the way a session actually runs. */
export const RAIL: { id: DrawerId; label: string }[] = [
  { id: "target", label: "Target" },
  { id: "mount", label: "Mount" },
  { id: "camera", label: "Camera" },
  { id: "align", label: "Polar align" },
  { id: "guiding", label: "Guiding" },
  { id: "session", label: "Session" },
  { id: "setup", label: "Setup" },
];
