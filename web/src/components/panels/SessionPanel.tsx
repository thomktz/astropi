import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../lib/api";
import { clockTime, duration, moonPhaseName } from "../../lib/format";
import type { Telemetry } from "../../lib/useTelemetry";
import { ErrorNote, Field, Section } from "../Field";

/** The imaging run, autofocus, and the constraints on the night. */
export function SessionPanel({ telemetry, busy }: { telemetry: Telemetry; busy: boolean }) {
  const [count, setCount] = useState(20);
  const [exposure, setExposure] = useState(120);

  const night = useQuery({ queryKey: ["night"], queryFn: api.night, staleTime: 10 * 60_000 });
  const capture = useMutation({
    mutationFn: () => api.tasks.capture({ count, exposure_s: exposure, dither_every: 3 }),
  });
  const focus = useMutation({ mutationFn: () => api.tasks.autofocus({}) });
  const cancel = useMutation({ mutationFn: (id: string) => api.tasks.cancel(id) });

  const task = telemetry.task;
  const running = task?.state === "running";

  return (
    <>
      {task && (
        <Section title="Current">
          <div className="spread">
            <div style={{ minWidth: 0 }}>
              <div className="name">{task.name}</div>
              <div className="small dim">{task.step}</div>
            </div>
            {running && (
              <button className="danger ghost" style={{ flex: "0 0 auto" }} onClick={() => cancel.mutate(task.id)}>
                Cancel
              </button>
            )}
          </div>
          {task.fraction != null && (
            <div className="bar">
              <span style={{ width: `${Math.round(task.fraction * 100)}%` }} />
            </div>
          )}
          {task.error && <div className="error">{task.error}</div>}
        </Section>
      )}

      <Section title="Imaging run">
        <div className="row">
          <label style={{ flex: "1 1 80px" }}>
            <span className="label">Frames</span>
            <input
              type="number"
              min={1}
              value={count}
              onChange={(event) => setCount(Math.max(1, Number(event.target.value)))}
            />
          </label>
          <label style={{ flex: "1 1 80px" }}>
            <span className="label">Each (s)</span>
            <input
              type="number"
              min={0.1}
              value={exposure}
              onChange={(event) => setExposure(Math.max(0.1, Number(event.target.value)))}
            />
          </label>
        </div>
        <div className="row">
          <button className="primary" disabled={busy} onClick={() => capture.mutate()}>
            Start run
          </button>
          <span className="small faint" style={{ textAlign: "right" }}>
            {duration((count * exposure) / 3600)} total
          </span>
        </div>
      </Section>

      <Section title="Focus">
        <button disabled={busy} onClick={() => focus.mutate()}>
          Run autofocus
        </button>
      </Section>

      {night.data && (
        <Section title="Tonight">
          <div className="spread">
            <Field label="Dark from" value={clockTime(night.data.astronomical_dusk)} />
            <Field label="Until" value={clockTime(night.data.astronomical_dawn)} />
            <Field
              label="Darkness"
              value={duration(night.data.dark_hours)}
              tone={night.data.dark_hours > 5 ? "good" : "fair"}
            />
          </div>
          <div className="small dim">
            Moon {moonPhaseName(night.data.moon_illumination)} &middot;{" "}
            {Math.round(night.data.moon_illumination * 100)}% lit &middot;{" "}
            {night.data.moon_altitude_deg > 0
              ? `up at ${night.data.moon_altitude_deg.toFixed(0)}°`
              : "below the horizon"}
          </div>
        </Section>
      )}

      {telemetry.log.length > 0 && (
        <Section title="Log">
          <div className="log">
            {[...telemetry.log].reverse().map((entry) => (
              <div key={`${entry.at}-${entry.text}`}>{entry.text}</div>
            ))}
          </div>
        </Section>
      )}

      <ErrorNote error={capture.error ?? focus.error ?? cancel.error} />
    </>
  );
}
