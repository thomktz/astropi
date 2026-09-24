import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import type { ReactNode } from "react";
import { Hint } from "./Hint";
import { CloseIcon } from "./Icons";

/**
 * A card over the workspace, for one thing you have deliberately opened.
 *
 * The drawer is not modal on purpose - the rig's telemetry has to stay
 * readable while you work. This is the exception: it holds a decision
 * ("shall I point the telescope at this?") rather than a reading, and it
 * still leaves the status strip uncovered, so a slew or a failing guide
 * star is visible even while the card is up.
 *
 * Rendered into `document.body` rather than in place, so it is not clipped
 * by the scrolling panel that opened it.
 */
export function Modal({
  title,
  subtitle,
  hint,
  size = "card",
  onClose,
  children,
}: {
  title: string;
  subtitle?: ReactNode;
  /** What this window is for, on the heading rather than in the body. */
  hint?: ReactNode;
  /** "full" fills nearly the whole window, for looking at a frame. */
  size?: "card" | "full";
  onClose: () => void;
  children: ReactNode;
}) {
  const card = useRef<HTMLDivElement>(null);

  // Moved into the card on open, so Tab starts inside it rather than
  // continuing through the list behind.
  useEffect(() => {
    card.current?.focus();
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      // Captured and stopped dead, because the drawer behind this listens
      // for Escape too - without this, one press would close both.
      event.stopImmediatePropagation();
      onClose();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  return createPortal(
    <div
      className="scrim"
      // On mousedown rather than click, and only when the press started on
      // the scrim itself: a drag that begins inside the card and ends
      // outside it is a selection, not a dismissal.
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className={`modal ${size}`} role="dialog" aria-modal="true" aria-label={title} tabIndex={-1} ref={card}>
        <header className="modal-header">
          <div style={{ minWidth: 0 }}>
            <div className="name">
              {title}
              {hint && <Hint>{hint}</Hint>}
            </div>
            {subtitle && <div className="small dim">{subtitle}</div>}
          </div>
          <button className="ghost icon-button" onClick={onClose} aria-label={`Close ${title}`}>
            <CloseIcon size={18} />
          </button>
        </header>
        <div className="modal-body">{children}</div>
      </div>
    </div>,
    document.body,
  );
}
