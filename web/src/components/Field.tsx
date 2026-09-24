import type { ReactNode } from "react";
import { Hint } from "./Hint";

export function Field({ label, value, tone }: { label: string; value: ReactNode; tone?: string }) {
  return (
    <div>
      <div className="label">{label}</div>
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
