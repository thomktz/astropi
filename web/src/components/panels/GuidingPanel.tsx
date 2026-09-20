import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { arcsec } from "../../lib/format";
import type { Telemetry } from "../../lib/useTelemetry";
import { GuideChart } from "../GuideChart";
import { GuideView } from "../GuideView";
import { ErrorNote, Field, Section } from "../Field";

/** Which indicator a guiding state deserves: lost is a problem, not progress. */
function dotClass(state: string): string {
  if (state === "guiding") return "live";
  if (state === "lost" || state === "error") return "down";
  if (state === "stopped") return "";
  return "busy";
}

/** Total RMS below this is good enough that seeing, not tracking, limits you. */
const GOOD_RMS_ARCSEC = 1.0;

export function GuidingPanel({ telemetry }: { telemetry: Telemetry }) {
  const queryClient = useQueryClient();
  const status = useQuery({
    queryKey: ["guiding"],
    queryFn: api.guiding.status,
    refetchInterval: 4_000,
    retry: false,
  });

  const act = useMutation({
    mutationFn: (action: () => Promise<unknown>) => action(),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["guiding"] }),
  });

  if (status.isError) {
    return <div className="small faint">No guide camera connected.</div>;
  }

  const state = telemetry.guideState ?? status.data?.state ?? "stopped";
  const running = state !== "stopped" && state !== "error";
  const rms = status.data?.rms_total_arcsec ?? null;
  const latest = telemetry.guideSamples.at(-1);

  return (
    <>
      <div className="spread">
        <span className="pill">
          <span className={`dot ${dotClass(state)}`} />
          {state}
        </span>
        {latest && (
          <span className="small faint mono">
            SNR {latest.snr.toFixed(0)} &middot; HFD {latest.hfd.toFixed(1)}
          </span>
        )}
      </div>

      <GuideView latest={latest} running={running} />

      <GuideChart samples={telemetry.guideSamples} />

      <div className="spread">
        <Field label="RMS total" value={arcsec(rms)} tone={rms != null && rms < GOOD_RMS_ARCSEC ? "good" : "fair"} />
        <Field label="RA" value={arcsec(status.data?.rms_ra_arcsec)} />
        <Field label="Dec" value={arcsec(status.data?.rms_dec_arcsec)} />
      </div>

      <div className="row">
        {running ? (
          <button className="danger" onClick={() => act.mutate(api.guiding.stop)}>
            Stop
          </button>
        ) : (
          <button className="primary" onClick={() => act.mutate(api.guiding.start)}>
            {status.data?.calibrated ? "Start guiding" : "Calibrate & guide"}
          </button>
        )}
        <button onClick={() => act.mutate(() => api.guiding.dither(12))} disabled={state !== "guiding"}>
          Dither
        </button>
      </div>

      {status.data?.calibration && (
        <Section title="Calibration">
          <div className="small faint mono">
            {status.data.calibration.ra_rate_arcsec_per_s.toFixed(1)}&quot;/s RA &middot;{" "}
            {status.data.calibration.dec_rate_arcsec_per_s.toFixed(1)}&quot;/s Dec &middot; camera angle{" "}
            {status.data.calibration.angle_deg.toFixed(0)}&#176;
          </div>
          <button
            className="ghost"
            onClick={() => act.mutate(api.guiding.clearCalibration)}
            title="Do this after rotating the camera or flipping the mount"
          >
            Clear calibration
          </button>
        </Section>
      )}

      <ErrorNote error={act.error} />
    </>
  );
}
