import { useEffect, useRef } from "react";
import type { ReactNode } from "react";
import { CloseIcon } from "./Icons";

/**
 * An overlay panel over the frame.
 *
 * Deliberately not modal: the image, the rail and the status strip all stay
 * live and interactive behind it. Dimming the rig's telemetry to show a
 * search box would be exactly backwards.
 */
export function Drawer({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <aside className="drawer" ref={panel} aria-label={title}>
      <header className="drawer-header">
        <h2>{title}</h2>
        <button className="ghost icon-button" onClick={onClose} aria-label={`Close ${title}`}>
          <CloseIcon size={18} />
        </button>
      </header>
      <div className="drawer-body">{children}</div>
    </aside>
  );
}
