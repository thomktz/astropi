import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../lib/api";
import { clockTime, duration, moonPhaseName } from "../lib/format";
import type { Telemetry } from "../lib/useTelemetry";
import { ErrorNote, Field, Panel } from "./Panel";

/** Running task, imaging sequence and autofocus - the things that take time. */
export function SessionPanel({ telemetry, busy }: { telemetry: Telemetry; busy: boolean }) {
  const [count, setCount] = useState(20);
  const [exposure, setExposure] = useState(120);

  const capture = useMutation({
    mutationFn: () => api.tasks.capture({ count, exposure_s: exposure, dither_every: 3 }),
  });
  const focus = useMutation({ mutationFn: () => api.tasks.autofocus({}) });
  const cancel = useMutation({ mutationFn: (id: string) => api.tasks.cancel(id) });

  const task = telemetry.task;
  const running = task?.state === "running";

  return (
    <Panel title="Session">
      {task && (
        <div className="stack">
          <div className="spread">
            <div>
              <div className="label">{task.name}</div>
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
        </div>
      )}

      <div className="row">
        <label style={{ flex: "1 1 90px" }}>
          <span className="label">Frames</span>
          <input
            type="number"
            min={1}
            value={count}
            onChange={(event) => setCount(Math.max(1, Number(event.target.value)))}
          />
        </label>
        <label style={{ flex: "1 1 90px" }}>
          <span className="label">Each (s)</span>
          <input
            type="number"
            min={0.1}
            value={exposure}
            onChange={(event) => setExposure(Math.max(0.1, Number(event.target.value)))}
          />
        </label>
        <button
          className="primary"
          style={{ flex: "0 0 auto", alignSelf: "end" }}
          disabled={busy}
          onClick={() => capture.mutate()}
        >
          Start run
        </button>
      </div>

      <div className="row">
        <span className="small faint">
          {duration((count * exposure) / 3600)} of integration
        </span>
        <button className="ghost" style={{ flex: "0 0 auto" }} disabled={busy} onClick={() => focus.mutate()}>
          Autofocus
        </button>
      </div>

      {telemetry.log.length > 0 && (
        <div className="log">
          {[...telemetry.log].reverse().map((entry) => (
            <div key={`${entry.at}-${entry.text}`}>{entry.text}</div>
          ))}
        </div>
      )}

      <ErrorNote error={capture.error ?? focus.error ?? cancel.error} />
    </Panel>
  );
}

/** Twilight, darkness and the moon - the constraints on the whole night. */
export function NightPanel() {
  const night = useQuery({ queryKey: ["night"], queryFn: api.night, staleTime: 10 * 60_000 });
  if (!night.data) return null;

  const { astronomical_dusk, astronomical_dawn, dark_hours, moon_illumination, moon_altitude_deg } =
    night.data;

  return (
    <Panel title="Tonight">
      <div className="spread">
        <Field label="Dark from" value={clockTime(astronomical_dusk)} />
        <Field label="Until" value={clockTime(astronomical_dawn)} />
        <Field label="Darkness" value={duration(dark_hours)} tone={dark_hours > 5 ? "good" : "fair"} />
      </div>
      <div className="small dim">
        Moon {moonPhaseName(moon_illumination)} &middot; {Math.round(moon_illumination * 100)}% lit &middot;{" "}
        {moon_altitude_deg > 0
          ? `up at ${moon_altitude_deg.toFixed(0)}°`
          : "below the horizon"}
      </div>
    </Panel>
  );
}
