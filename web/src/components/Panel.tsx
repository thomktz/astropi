import type { ReactNode } from "react";

export function Panel({
  title,
  actions,
  children,
}: {
  title: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="panel">
      <header>
        <h2>{title}</h2>
        {actions}
      </header>
      {children}
    </section>
  );
}

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
