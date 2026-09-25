import type { ReactNode } from "react";
import { Hint } from "./Hint";

export function Field({
  label,
  value,
  tone,
  hint,
}: {
  label: string;
  value: ReactNode;
  tone?: string;
  /** What the number means, on hover - never on screen. */
  hint?: ReactNode;
}) {
  return (
    <div>
      <div className="label">
        {label}
        {hint && <Hint>{hint}</Hint>}
      </div>
      <div className={`readout ${tone ?? ""}`}>{value}</div>
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  return <div className="error">{error instanceof Error ? error.message : String(error)}</div>;
}

/** A labelled group inside a drawer, with its explanation on the label. */
export function Section({
  title,
  hint,
  children,
}: {
  title: string;
  /** Why this section exists, shown on hovering the heading's mark. */
  hint?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="section">
      <h3 className="label">
        {title}
        {hint && <Hint>{hint}</Hint>}
      </h3>
      {children}
    </section>
  );
}
