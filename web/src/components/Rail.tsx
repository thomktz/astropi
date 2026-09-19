import type { ReactNode } from "react";
import { AlignIcon, CameraIcon, GuidingIcon, SessionIcon, SetupIcon, TargetIcon } from "./Icons";

export type DrawerId = "target" | "camera" | "guiding" | "align" | "session" | "setup";

interface RailEntry {
  id: DrawerId;
  label: string;
  icon: ReactNode;
}

/** Ordered the way a session actually runs, top to bottom. */
export const RAIL: RailEntry[] = [
  { id: "target", label: "Target", icon: <TargetIcon /> },
  { id: "camera", label: "Camera", icon: <CameraIcon /> },
  { id: "align", label: "Polar align", icon: <AlignIcon /> },
  { id: "guiding", label: "Guiding", icon: <GuidingIcon /> },
  { id: "session", label: "Session", icon: <SessionIcon /> },
  { id: "setup", label: "Setup", icon: <SetupIcon /> },
];

/**
 * The drawer switcher.
 *
 * Clicking the open drawer's own button closes it, so the frame can always
 * be got back to in one tap - which is the point of an image-first layout.
 */
export function Rail({
  open,
  onSelect,
  badges,
}: {
  open: DrawerId | null;
  onSelect: (id: DrawerId | null) => void;
  badges?: Partial<Record<DrawerId, "busy" | "good" | "warn">>;
}) {
  return (
    <nav className="rail" aria-label="Panels">
      {RAIL.map((entry) => {
        const active = open === entry.id;
        const badge = badges?.[entry.id];
        return (
          <button
            key={entry.id}
            className="rail-button"
            aria-pressed={active}
            aria-label={entry.label}
            title={entry.label}
            onClick={() => onSelect(active ? null : entry.id)}
          >
            {entry.icon}
            <span className="rail-label">{entry.label}</span>
            {badge && <span className={`rail-badge ${badge}`} />}
          </button>
        );
      })}
    </nav>
  );
}
