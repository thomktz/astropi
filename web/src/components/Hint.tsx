import type { ReactNode } from "react";

/**
 * An explanation that is there when wanted and invisible when not.
 *
 * The panels had grown paragraphs - what a control does, why a default
 * is what it is, what a number means - and prose is the one thing a
 * dashboard cannot afford: it is read once, ignored for ever after, and
 * occupies the space where the numbers should be. This is the same text,
 * behind a mark you can point at.
 *
 * Hover *and* focus, so it is reachable from a keyboard, and `role`
 * "note" rather than a tooltip role because it explains rather than
 * labels.
 */
export function Hint({ children }: { children: ReactNode }) {
  return (
    <span className="hint" tabIndex={0} role="note">
      <span aria-hidden="true" className="hint-mark">
        i
      </span>
      <span className="hint-bubble small">{children}</span>
    </span>
  );
}
