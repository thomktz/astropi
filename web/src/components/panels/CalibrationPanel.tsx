import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../lib/api";
import type { CalibrationRequest } from "../../lib/types";
import type { Telemetry } from "../../lib/useTelemetry";
import { ErrorNote, Section } from "../Field";
import { NumberField } from "../NumberField";

/**
 * Flats, dark flats and darks, in the order they are shot.
 *
 * Each one has a single thing that makes it right, and the panel fills it
 * in rather than asking: flats find their own exposure, dark flats take
 * the flats', and darks start from the last light frame saved. What is
 * left to choose is how many.
 */
export function CalibrationPanel({ telemetry, busy }: { telemetry: Telemetry; busy: boolean }) {
  const queryClient = useQueryClient();
  const defaults = useQuery({
    queryKey: ["calibration-defaults"],
    queryFn: api.tasks.calibrationDefaults,
  });
  const light = defaults.data?.last_light ?? null;
  const flat = defaults.data?.last_flat ?? null;

  // Shared by all three: calibration frames only apply to lights shot at
  // the same gain and binning.
  const [gain, setGain] = useState<number | null>(null);
  const [binning, setBinning] = useState<number | null>(null);
  const [flatCount, setFlatCount] = useState(30);
  const [level, setLevel] = useState(50);
  const [darkFlatCount, setDarkFlatCount] = useState(30);
  const [darkCount, setDarkCount] = useState(20);
  const [darkExposure, setDarkExposure] = useState<number | null>(null);

  const shotGain = gain ?? light?.gain ?? undefined;
  const shotBinning = binning ?? light?.binning ?? 1;
  const shotDarkExposure = darkExposure ?? light?.exposure_s ?? null;

  const start = useMutation({
    mutationFn: (body: CalibrationRequest) => api.tasks.calibration(body),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["calibration-defaults"] }),
  });

  const task = telemetry.task;
  const running = task?.kind === "calibration" && task.state === "running";
  const disabled = busy || start.isPending;
  const sensor = telemetry.camera?.sensor_c;

  const shared = { gain: shotGain, binning: shotBinning };

  return (
    <>
      <Section
        title="Match the lights"
        hint="Calibration only subtracts or divides cleanly when gain and binning match the lights exactly. These start from the last light frame saved."
      >
        <div className="row">
          <NumberField
            label="Gain"
            value={shotGain ?? null}
            min={0}
            step={10}
            placeholder="camera default"
            onCommit={setGain}
          />
          <NumberField
            label="Binning"
            value={shotBinning}
            min={1}
            max={4}
            step={1}
            onCommit={setBinning}
          />
        </div>
        {light && (
          <div className="small faint mono">
            last lights: {light.exposure_s}s · gain {light.gain ?? "default"}
            {light.sensor_temp_c != null && ` · ${light.sensor_temp_c.toFixed(1)}°C`}
          </div>
        )}
      </Section>

      {running && (
        <Section title="Running">
          <div className="small">{task.name}</div>
          {task.fraction != null && (
            <progress max={1} value={task.fraction} style={{ width: "100%" }} />
          )}
          <div className="small faint mono">{task.messages.at(-1)}</div>
          <button className="ghost" onClick={() => api.tasks.cancel(task.id)}>
            Stop
          </button>
        </Section>
      )}

      <Section
        title="Flats"
        hint="With the scope pointed at an evenly lit panel or a dusk sky, focus and camera angle untouched since the lights. Short test frames find the exposure that puts the level where you ask, then the run is shot at it."
      >
        <div className="row">
          <NumberField label="Frames" value={flatCount} min={1} max={500} step={5} onCommit={(n) => n && setFlatCount(n)} />
          <NumberField
            label="Level"
            title="Where the middle of the histogram should sit. Around half is the usual choice: well clear of the noise floor and of the sensor's non-linear top end."
            value={level}
            min={10}
            max={85}
            step={5}
            suffix="%"
            onCommit={(n) => n && setLevel(n)}
          />
        </div>
        <button
          className="primary"
          disabled={disabled}
          onClick={() => start.mutate({ kind: "flats", count: flatCount, target_level: level / 100, ...shared })}
        >
          Shoot flats
        </button>
      </Section>

      <Section
        title="Dark flats"
        hint="Cap on, same exposure and gain as the flats. They remove the sensor's own signal from the flats, which at short exposures is mostly the offset."
      >
        <div className="row">
          <NumberField
            label="Frames"
            value={darkFlatCount}
            min={1}
            max={500}
            step={5}
            onCommit={(n) => n && setDarkFlatCount(n)}
          />
        </div>
        <div className="small faint mono">
          {flat
            ? `at the flats' ${flat.exposure_s}s, gain ${flat.gain ?? "default"}`
            : "shoot flats first - these take the flats' exposure"}
        </div>
        <button
          className="primary"
          disabled={disabled || !flat}
          onClick={() =>
            start.mutate({
              kind: "dark_flats",
              count: darkFlatCount,
              gain: flat?.gain ?? undefined,
              binning: flat?.binning ?? 1,
            })
          }
        >
          Shoot dark flats
        </button>
      </Section>

      <Section
        title="Darks"
        hint="Cap on, same exposure, gain and sensor temperature as the lights. Shoot them while the cooler is still at the lights' setpoint."
      >
        <div className="row">
          <NumberField label="Frames" value={darkCount} min={1} max={500} step={5} onCommit={(n) => n && setDarkCount(n)} />
          <NumberField
            label="Exposure (s)"
            value={shotDarkExposure}
            min={0.001}
            step={10}
            onCommit={setDarkExposure}
          />
        </div>
        {sensor != null && light?.sensor_temp_c != null && Math.abs(sensor - light.sensor_temp_c) > 1 && (
          <div className="small poor">
            Sensor is at {sensor.toFixed(1)}°C, the lights were at {light.sensor_temp_c.toFixed(1)}°C.
          </div>
        )}
        <button
          className="primary"
          disabled={disabled || shotDarkExposure == null}
          onClick={() =>
            shotDarkExposure != null &&
            start.mutate({ kind: "darks", count: darkCount, exposure_s: shotDarkExposure, ...shared })
          }
        >
          Shoot darks
        </button>
      </Section>

      <div className="small faint">
        Saved to the night&apos;s folder under <span className="mono">calibration/</span>, beside the lights.
      </div>
      <ErrorNote error={start.error} />
    </>
  );
}
