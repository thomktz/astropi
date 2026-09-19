import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../lib/api";
import { temperature } from "../lib/format";
import type { Telemetry } from "../lib/useTelemetry";
import { ErrorNote, Field, Panel } from "./Panel";

const COOLING_TARGET_C = -10;

export function CameraPanel({ telemetry }: { telemetry: Telemetry }) {
  const queryClient = useQueryClient();
  const [exposure, setExposure] = useState(5);

  const status = useQuery({ queryKey: ["camera"], queryFn: () => api.camera.status() });
  const frames = useQuery({ queryKey: ["frames"], queryFn: api.camera.frames, refetchInterval: 5_000 });

  const expose = useMutation({
    mutationFn: () => api.camera.expose(exposure),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["frames"] }),
  });

  const cooling = useMutation({
    mutationFn: (enabled: boolean) => api.camera.cooling(enabled, COOLING_TARGET_C),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["camera"] }),
  });

  const live = telemetry.camera;
  const latest = frames.data?.[0];
  const sensorTemp = live?.sensor_c ?? status.data?.cooling.sensor_c ?? null;
  const coolingOn = live?.cooling_enabled ?? status.data?.cooling.enabled ?? false;
  const exposing = live?.state === "exposing";

  return (
    <Panel
      title="Camera"
      actions={
        status.data && (
          <span className="small faint mono">
            {status.data.pixel_scale_arcsec.toFixed(2)}&quot;/px &middot;{" "}
            {status.data.field_of_view_deg[0].toFixed(1)}&#215;
            {status.data.field_of_view_deg[1].toFixed(1)}&#176;
          </span>
        )
      }
    >
      {latest ? (
        <img
          className="preview"
          src={api.camera.previewUrl(latest.id)}
          alt={`Last frame, ${latest.duration_s} second exposure`}
        />
      ) : (
        <div className="preview" role="presentation" />
      )}

      <div className="row">
        <label style={{ flex: "1 1 120px" }}>
          <span className="label">Exposure (s)</span>
          <input
            type="number"
            min={0.001}
            step={1}
            value={exposure}
            onChange={(event) => setExposure(Math.max(0.001, Number(event.target.value)))}
          />
        </label>
        <button
          className="primary"
          style={{ flex: "0 0 auto", alignSelf: "end" }}
          disabled={expose.isPending || exposing}
          onClick={() => expose.mutate()}
        >
          {expose.isPending || exposing ? "Exposing..." : "Capture"}
        </button>
      </div>

      {status.data?.cooling.supported && (
        <div className="spread">
          <Field
            label="Sensor"
            value={temperature(sensorTemp)}
            tone={sensorTemp != null && sensorTemp <= COOLING_TARGET_C + 1 ? "good" : undefined}
          />
          <button style={{ flex: "0 0 auto" }} onClick={() => cooling.mutate(!coolingOn)}>
            {coolingOn ? "Cooler off" : `Cool to ${COOLING_TARGET_C}°C`}
          </button>
        </div>
      )}

      {telemetry.lastSolve && (
        <div className="small dim">
          {telemetry.lastSolve.success
            ? `Solved with ${telemetry.lastSolve.solver} — ${telemetry.lastSolve.stars_detected} stars in ${telemetry.lastSolve.solve_time_s?.toFixed(1)}s`
            : `Solve failed: ${telemetry.lastSolve.error}`}
        </div>
      )}

      <ErrorNote error={expose.error ?? cooling.error} />
    </Panel>
  );
}
