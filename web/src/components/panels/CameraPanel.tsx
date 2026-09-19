import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../lib/api";
import { temperature } from "../../lib/format";
import type { Telemetry } from "../../lib/useTelemetry";
import { ErrorNote, Field, Section } from "../Field";

const COOLING_TARGET_C = -10;
/** Exposures offered as one tap, covering framing through to a real sub. */
const QUICK_EXPOSURES = [1, 5, 30, 120];

export function CameraPanel({ telemetry }: { telemetry: Telemetry }) {
  const queryClient = useQueryClient();
  const [exposure, setExposure] = useState(5);
  const [gain, setGain] = useState<number | "">("");

  const status = useQuery({ queryKey: ["camera"], queryFn: () => api.camera.status() });

  const expose = useMutation({
    mutationFn: () => api.camera.expose(exposure, gain === "" ? {} : { gain }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["frames"] }),
  });

  const cooling = useMutation({
    mutationFn: (enabled: boolean) => api.camera.cooling(enabled, COOLING_TARGET_C),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["camera"] }),
  });

  const live = telemetry.camera;
  const sensorTemp = live?.sensor_c ?? status.data?.cooling.sensor_c ?? null;
  const coolingOn = live?.cooling_enabled ?? status.data?.cooling.enabled ?? false;
  const busy = live?.state === "exposing" || live?.state === "reading" || live?.state === "downloading";

  return (
    <>
      <Section title="Expose">
        <div className="row quick">
          {QUICK_EXPOSURES.map((seconds) => (
            <button
              key={seconds}
              className="ghost"
              aria-pressed={exposure === seconds}
              onClick={() => setExposure(seconds)}
            >
              {seconds}s
            </button>
          ))}
        </div>
        <div className="row">
          <label style={{ flex: "1 1 100px" }}>
            <span className="label">Seconds</span>
            <input
              type="number"
              min={0.001}
              step={1}
              value={exposure}
              onChange={(event) => setExposure(Math.max(0.001, Number(event.target.value)))}
            />
          </label>
          <label style={{ flex: "1 1 80px" }}>
            <span className="label">Gain</span>
            <input
              type="number"
              min={0}
              placeholder={String(status.data?.gain ?? 100)}
              value={gain}
              onChange={(event) =>
                setGain(event.target.value === "" ? "" : Math.max(0, Number(event.target.value)))
              }
            />
          </label>
        </div>
        <div className="row">
          <button className="primary" disabled={expose.isPending || busy} onClick={() => expose.mutate()}>
            {busy ? "Exposing…" : "Capture"}
          </button>
          <button className="ghost" disabled={!busy} onClick={() => api.camera.abort()}>
            Abort
          </button>
        </div>
      </Section>

      {status.data?.cooling.supported && (
        <Section title="Cooling">
          <div className="spread">
            <Field
              label="Sensor"
              value={temperature(sensorTemp)}
              tone={sensorTemp != null && sensorTemp <= COOLING_TARGET_C + 1 ? "good" : undefined}
            />
            <Field
              label="Power"
              value={`${Math.round(live?.cooling_power ?? status.data.cooling.power_percent ?? 0)}%`}
            />
            <button style={{ flex: "0 0 auto", alignSelf: "center" }} onClick={() => cooling.mutate(!coolingOn)}>
              {coolingOn ? "Off" : `Cool to ${COOLING_TARGET_C}°C`}
            </button>
          </div>
        </Section>
      )}

      {status.data && (
        <Section title="Optics">
          <div className="small dim mono stack">
            <span>
              {status.data.sensor.width}&#215;{status.data.sensor.height} &middot;{" "}
              {status.data.sensor.pixel_size_um}&micro;m
              {status.data.sensor.bayer_pattern && ` · ${status.data.sensor.bayer_pattern}`}
            </span>
            <span>
              {status.data.pixel_scale_arcsec.toFixed(2)}&quot;/px &middot; field{" "}
              {status.data.field_of_view_deg[0].toFixed(2)}&#215;
              {status.data.field_of_view_deg[1].toFixed(2)}&#176;
            </span>
          </div>
        </Section>
      )}

      <ErrorNote error={expose.error ?? cooling.error} />
    </>
  );
}
