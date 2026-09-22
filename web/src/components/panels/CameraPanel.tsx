import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { setCaptureSettings, useCaptureSettings } from "../../lib/captureSettings";
import type { CameraControl, FrameSummary } from "../../lib/types";
import type { Telemetry } from "../../lib/useTelemetry";
import { ControlField } from "../ControlField";
import { ErrorNote, Section } from "../Field";
import { NumberField } from "../NumberField";
import { Switch } from "../Switch";

/**
 * Controls that belong to the cooler rather than to the sensor.
 *
 * Only a grouping for the panel: the backend advertises one flat list, and
 * anything it names that is not here lands in "Sensor" instead of being
 * dropped, so a camera with a filter wheel or a rotator control still
 * shows it.
 */
const COOLING_CONTROLS = ["cooler_on", "target_temp", "sensor_temp", "cooler_power", "dew_heater"];

export function CameraPanel({
  telemetry,
  busy,
  onCaptured,
}: {
  telemetry: Telemetry;
  busy: boolean;
  onCaptured: (frame: FrameSummary) => void;
}) {
  const queryClient = useQueryClient();
  // Shared with the Capture button under the display, so the two cannot
  // disagree about what pressing them will shoot.
  const { exposure_s: exposure, gain } = useCaptureSettings();

  const status = useQuery({ queryKey: ["camera"], queryFn: () => api.camera.status() });
  const preview = useQuery({ queryKey: ["camera-preview"], queryFn: api.camera.preview, retry: false });
  // Polled, because two of these are measurements rather than settings -
  // the sensor temperature and the cooler's duty cycle move on their own.
  const controls = useQuery({
    queryKey: ["camera-controls"],
    queryFn: () => api.camera.controls(),
    refetchInterval: 5_000,
  });

  const setPreview = useMutation({
    mutationFn: api.camera.setPreview,
    onSuccess: (next) => {
      queryClient.setQueryData(["camera-preview"], next);
      queryClient.invalidateQueries({ queryKey: ["camera-view"] });
    },
  });

  const expose = useMutation({
    // Always sends a gain, never inheriting one. The live view sets the
    // camera's gain every frame, so a capture that left it unsaid would
    // silently shoot at whatever the preview last used.
    mutationFn: () => api.camera.expose(exposure, { gain: gain ?? captureGain }),
    onSuccess: (frame) => {
      onCaptured(frame);
      queryClient.invalidateQueries({ queryKey: ["camera-view"] });
    },
  });

  const focus = useMutation({ mutationFn: () => api.tasks.autofocus({}) });

  const setControl = useMutation({
    mutationFn: ({ name, value }: { name: string; value: number }) =>
      api.camera.setControl(name, value),
    onSuccess: (updated) =>
      // Written straight back rather than refetched, so the field does not
      // sit on its old value for however long the next poll takes.
      queryClient.setQueryData(["camera-controls"], (previous: CameraControl[] | undefined) =>
        previous?.map((control) => (control.name === updated.name ? updated : control)),
      ),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["camera"] }),
  });

  // The two measurements the WebSocket already carries. Taking them from
  // there rather than the poll means the sensor temperature moves while
  // you watch it cool instead of stepping every five seconds.
  const fresh: Record<string, number | null | undefined> = {
    sensor_temp: telemetry.camera?.sensor_c,
    cooler_power: telemetry.camera?.cooling_power,
  };
  const controlList = (controls.data ?? []).map((control) => {
    const live = fresh[control.name];
    return live == null ? control : { ...control, value: live };
  });

  const byName = new Map(controlList.map((control) => [control.name, control]));
  /** What a capture shoots at when the field is left empty. */
  const captureGain = byName.get("gain")?.default ?? 100;
  const coolingControls = COOLING_CONTROLS.map((name) => byName.get(name)).filter(
    (control): control is CameraControl => control != null,
  );
  const sensorControls = controlList.filter(
    (control) => !COOLING_CONTROLS.includes(control.name),
  );

  /**
   * Turning the cooler on with no setpoint would do nothing at all, since
   * the driver drives toward a target it does not have. Send the control's
   * own default first.
   */
  const applyControl = (name: string, value: number) => {
    const target = byName.get("target_temp");
    if (name === "cooler_on" && value >= 0.5 && target?.value == null && target?.default != null) {
      setControl.mutate({ name: "target_temp", value: target.default });
    }
    setControl.mutate({ name, value });
  };
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
            onCommit={(next) => next != null && setCaptureSettings({ exposure_s: next })}
          />
          <NumberField
            label="Gain"
            title={`For this frame only. Left empty it shoots at ${captureGain}, the camera's default - not at whatever the live view last set.`}
            value={gain}
            min={0}
            step={10}
            placeholder={String(captureGain)}
            onCommit={(next) => setCaptureSettings({ gain: next })}
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

      {coolingControls.length > 0 && (
        <Section title="Cooling">
          <div className="spread">
            {coolingControls
              .filter((control) => control.kind === "number")
              .map((control) => (
                <ControlField
                  key={control.name}
                  control={control}
                  tone={coolingTone(control, byName.get("target_temp")?.value ?? null)}
                  onSet={applyControl}
                />
              ))}
          </div>
          <div className="row">
            {coolingControls
              .filter((control) => control.kind === "boolean")
              .map((control) => (
                <ControlField key={control.name} control={control} onSet={applyControl} />
              ))}
          </div>
          <p className="small faint" style={{ margin: 0 }}>
            Pick a setpoint you can hold all night and all year, since darks only subtract properly
            at the temperature they were shot at. Sustained power near 100% means the cooler has no
            headroom left; ease the target up.
          </p>
        </Section>
      )}

      {sensorControls.length > 0 && (
        <Section title="Sensor">
          {/*
            Whatever the driver advertises, rendered from its own limits.
            Nothing in the panel knows this camera in particular - connect
            a different one and this section becomes its controls instead.
          */}
          <div className="spread">
            {sensorControls
              .filter((control) => control.kind === "number")
              .map((control) => (
                <ControlField key={control.name} control={control} onSet={applyControl} />
              ))}
          </div>
          <div className="row">
            {sensorControls
              .filter((control) => control.kind === "boolean")
              .map((control) => (
                <ControlField key={control.name} control={control} onSet={applyControl} />
              ))}
          </div>
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

      <ErrorNote
        error={expose.error ?? setControl.error ?? focus.error ?? setPreview.error}
      />

    </>
  );
}

/**
 * Colour for the two cooling readouts.
 *
 * Green once the sensor is within a degree of where it was asked to be -
 * the point at which a dark library shot at that temperature applies - and
 * amber when the cooler is running out of headroom.
 */
function coolingTone(control: CameraControl, target: number | null): string | undefined {
  if (control.name === "cooler_power") return (control.value ?? 0) > 90 ? "fair" : undefined;
  if (control.name === "sensor_temp" && control.value != null && target != null) {
    return Math.abs(control.value - target) <= 1 ? "good" : undefined;
  }
  return undefined;
}
