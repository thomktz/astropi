import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import type { Telemetry } from "../../lib/useTelemetry";
import { ErrorNote, Section } from "../Field";
import { NumberField } from "../NumberField";

/**
 * The dark library: darks shot once and reused by every session at the
 * same exposure, gain, offset and temperature.
 *
 * Flats, dark flats and a session's own darks are shot from the Session
 * page, beside the lights they calibrate.
 */
export function CalibrationPanel({ telemetry, busy }: { telemetry: Telemetry; busy: boolean }) {
  const queryClient = useQueryClient();
  const sets = useQuery({ queryKey: ["dark-library"], queryFn: api.darkLibrary.list });
  const defaults = useQuery({ queryKey: ["calibration-defaults"], queryFn: api.tasks.calibrationDefaults });
  const light = defaults.data?.last_light ?? null;

  const [count, setCount] = useState(30);
  const [exposure, setExposure] = useState<number | null>(null);
  const [gain, setGain] = useState<number | null>(null);
  const [offset, setOffset] = useState<number | null>(null);
  const [temp, setTemp] = useState<number | null>(telemetry.camera?.cooling_target_c ?? null);

  const task = telemetry.task;
  const running = task?.kind === "session_group" && task.state === "running" && task.detail.session_id == null;
  useEffect(() => {
    queryClient.invalidateQueries({ queryKey: ["dark-library"] });
  }, [queryClient, task?.state]);

  const shoot = useMutation({
    mutationFn: () =>
      api.darkLibrary.shoot({ count, exposure_s: exposure!, gain, offset, temp_c: temp! }),
  });

  return (
    <>
      <Section title="Dark library">
        {sets.data?.length === 0 && <div className="small faint">No sets yet.</div>}
        {sets.data?.map((set) => (
          <div key={set.path} className="block">
            <div className="spread">
              <span className="name mono">
                {set.exposure_s}s · G{set.gain ?? "def"} · O{set.offset ?? "def"} · {set.temp_c}°C
              </span>
              <span className="small mono">{set.count} frames</span>
            </div>
            <div className={`small ${set.stale ? "fair" : "faint"}`}>
              shot {set.shot_on}
              {set.mean_sensor_c != null && ` at ${set.mean_sensor_c.toFixed(1)}°C`} · {set.camera}
              {set.stale && " · over 9 months old, consider re-shooting"}
            </div>
          </div>
        ))}
        <div className="small faint">
          Flats, dark flats and session darks are on the Session page.
        </div>
      </Section>

      <Section title="Shoot a set">
        <div className="small dim">
          Cap the scope or the camera. Can be done indoors or on a cloudy night - let the cooler
          stabilize first.
        </div>
        <div className="row block-fields">
          <NumberField label="Frames" value={count} min={1} max={500} step={5} onCommit={(n) => n && setCount(n)} />
          <NumberField label="Exposure" suffix="s" value={exposure} min={0.001} step={10} onCommit={setExposure} />
          <NumberField label="Gain" value={gain} min={0} step={10} placeholder="default" onCommit={setGain} />
          <NumberField label="Offset" value={offset} min={0} step={5} placeholder="default" onCommit={setOffset} />
          <NumberField label="Temp" suffix="°C" value={temp} min={-40} max={30} step={1} onCommit={setTemp} />
        </div>
        {light && (
          <button
            className="ghost"
            onClick={() => {
              setExposure(light.exposure_s);
              setGain(light.gain);
              setOffset(light.offset ?? null);
            }}
          >
            Use the last lights ({light.exposure_s}s, gain {light.gain ?? "default"})
          </button>
        )}
        {running ? (
          <>
            <div className="small faint mono">{task.messages.at(-1)}</div>
            <button className="danger ghost" onClick={() => api.tasks.cancel(task.id)}>
              Stop
            </button>
          </>
        ) : (
          <button
            className="primary"
            disabled={busy || exposure == null || temp == null || shoot.isPending}
            onClick={() => shoot.mutate()}
          >
            Shoot into library
          </button>
        )}
        <ErrorNote error={shoot.error ?? sets.error} />
      </Section>
    </>
  );
}
