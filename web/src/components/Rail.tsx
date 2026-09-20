import type { ReactNode } from "react";
import {
  AlignIcon,
  CameraIcon,
  GuidingIcon,
  MountIcon,
  SessionIcon,
  SetupIcon,
  TargetIcon,
} from "./Icons";
import { RAIL, type DrawerId } from "./railEntries";

const ICONS: Record<DrawerId, ReactNode> = {
  target: <TargetIcon />,
  mount: <MountIcon />,
  camera: <CameraIcon />,
  align: <AlignIcon />,
  guiding: <GuidingIcon />,
  session: <SessionIcon />,
  setup: <SetupIcon />,
};

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
      {RAIL.map((entry, index) => {
        const active = open === entry.id;
        const badge = badges?.[entry.id];
        // A rule where the during-session panels end and the
        // set-up-once ones begin.
        const startsGroup = index > 0 && RAIL[index - 1].group !== entry.group;
        return (
          <button
            key={entry.id}
            className={`rail-button${startsGroup ? " group-start" : ""}`}
            aria-pressed={active}
            aria-label={entry.label}
            title={entry.label}
            onClick={() => onSelect(active ? null : entry.id)}
          >
            {ICONS[entry.id]}
            <span className="rail-label">{entry.label}</span>
            {badge && <span className={`rail-badge ${badge}`} />}
          </button>
        );
      })}
    </nav>
  );
}
