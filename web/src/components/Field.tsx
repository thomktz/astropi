import type { ReactNode } from "react";

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

/** A labelled group inside a drawer. */
export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="section">
      <h3 className="label">{title}</h3>
      {children}
    </section>
  );
}
