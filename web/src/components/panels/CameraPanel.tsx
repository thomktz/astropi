import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../lib/api";
import { temperature } from "../../lib/format";
import type { Telemetry } from "../../lib/useTelemetry";
import { ErrorNote, Field, Section } from "../Field";
import { NumberField } from "../NumberField";
import { Switch } from "../Switch";

/** A reasonable starting setpoint for a cooled CMOS camera in temperate weather. */
const DEFAULT_TARGET_C = -10;

export function CameraPanel({ telemetry, busy }: { telemetry: Telemetry; busy: boolean }) {
  const queryClient = useQueryClient();
  const [exposure, setExposure] = useState(5);
  const [gain, setGain] = useState<number | null>(null);
  const [target, setTarget] = useState(DEFAULT_TARGET_C);

  const status = useQuery({ queryKey: ["camera"], queryFn: () => api.camera.status() });
  const preview = useQuery({ queryKey: ["camera-preview"], queryFn: api.camera.preview, retry: false });

  const setPreview = useMutation({
    mutationFn: api.camera.setPreview,
    onSuccess: (next) => {
      queryClient.setQueryData(["camera-preview"], next);
      queryClient.invalidateQueries({ queryKey: ["camera-view"] });
    },
  });

  const expose = useMutation({
    mutationFn: () => api.camera.expose(exposure, gain == null ? {} : { gain }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["camera-view"] }),
  });

  const focus = useMutation({ mutationFn: () => api.tasks.autofocus({}) });

  const cooling = useMutation({
    mutationFn: ({ enabled, target_c }: { enabled: boolean; target_c: number }) =>
      api.camera.cooling(enabled, target_c),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["camera"] }),
  });

  const live = telemetry.camera;
  const sensorTemp = live?.sensor_c ?? status.data?.cooling.sensor_c ?? null;
  const coolingOn = live?.cooling_enabled ?? status.data?.cooling.enabled ?? false;
  // Only a deliberate exposure counts. The live view keeps the sensor busy
  // continuously, and driving the button from sensor state made it flash
  // between Capture and Exposing every couple of seconds.
  const capturing = expose.isPending;

  return (
    <>
      <Section title="Live view">
        {/*
          Its own settings, not the imaging ones. A preview is a short,
          high-gain, binned frame answering "is it pointed at the thing and
          is it in focus"; a light frame is a long, low-gain one collecting
          signal. Sharing settings would make one of the two wrong.
        */}
        <Switch
          checked={preview.data?.enabled ?? false}
          label={preview.data?.enabled ? "Live view on" : "Live view off"}
          onChange={(enabled) => setPreview.mutate({ enabled })}
        />

        {preview.data?.enabled && (
          <div className="row">
            <NumberField
              label="Exposure (s)"
              value={preview.data.exposure_s}
              min={0.1}
              step={0.5}
              onCommit={(next) => next != null && setPreview.mutate({ exposure_s: next })}
            />
            <NumberField
              label="Gain"
              value={preview.data.gain}
              min={0}
              step={10}
              onCommit={(next) => next != null && setPreview.mutate({ gain: next })}
            />
            <NumberField
              label="Binning"
              title={`Sums each ${preview.data.binning}\u00d7${preview.data.binning} block of pixels into one: ${preview.data.binning ** 2}\u00d7 the signal per pixel and ${preview.data.binning ** 2}\u00d7 less to read out and send. Fine for framing and focus, which is all a preview is for.`}
              value={preview.data.binning}
              min={1}
              step={1}
              onCommit={(next) => next != null && setPreview.mutate({ binning: next })}
            />
            <NumberField
              label="Frequency (s)"
              value={preview.data.period_s}
              min={0}
              step={0.5}
              onCommit={(next) => next != null && setPreview.mutate({ period_s: next })}
            />
          </div>
        )}

        {preview.data?.enabled && (
          <p className="small faint" style={{ margin: 0 }}>
            A new frame starts every {preview.data.period_s}s, counted from the start of the last
            one — so the {preview.data.exposure_s}s exposure happens inside that, not on top of
            it. Set it below the exposure to run back to back.
          </p>
        )}

      </Section>

      <Section title="Expose">
        <div className="row">
          <NumberField
            label="Seconds"
            value={exposure}
            min={0.001}
            step={1}
            onCommit={(next) => next != null && setExposure(next)}
          />
          <NumberField
            label="Gain"
            value={gain}
            min={0}
            step={10}
            placeholder={String(status.data?.gain ?? 100)}
            onCommit={setGain}
          />
        </div>
        <div className="row">
          <button className="primary" disabled={capturing} onClick={() => expose.mutate()}>
            {capturing ? "Exposing…" : "Capture"}
          </button>
          <button className="ghost" disabled={!capturing} onClick={() => api.camera.abort()}>
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
              tone={
                sensorTemp != null && target != null && Math.abs(sensorTemp - target) <= 1
                  ? "good"
                  : undefined
              }
            />
            <Field
              label="Power"
              value={`${Math.round(live?.cooling_power ?? status.data.cooling.power_percent ?? 0)}%`}
              tone={(live?.cooling_power ?? 0) > 90 ? "fair" : undefined}
            />
            <NumberField
              label="Target"
              value={target}
              min={-40}
              max={30}
              step={1}
              suffix="°C"
              onCommit={(next) => next != null && setTarget(next)}
            />
          </div>
          <Switch
            checked={coolingOn}
            label={coolingOn ? `Cooling to ${target}\u00b0C` : "Cooler off"}
            onChange={(enabled) => cooling.mutate({ enabled, target_c: target })}
          />
          <p className="small faint" style={{ margin: 0 }}>
            Pick a setpoint you can hold all night and all year, since darks only subtract properly
            at the temperature they were shot at. Sustained power near 100% means the cooler has no
            headroom left; ease the target up.
          </p>
        </Section>
      )}

      <Section title="Focus">
        <div className="row">
          <button disabled={busy || focus.isPending} onClick={() => focus.mutate()}>
            Run autofocus
          </button>
          <span className="small faint">
            Steps through focus and fits the V-curve. Plan blocks can do this per target.
          </span>
        </div>
      </Section>

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

      <ErrorNote error={expose.error ?? cooling.error ?? focus.error ?? setPreview.error} />
    </>
  );
}
