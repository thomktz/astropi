import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { arcsec } from "../lib/format";
import type { Task } from "../lib/types";
import { ErrorNote, Field } from "./Field";
import { Modal } from "./Modal";

/**
 * What the mount and the sky do when nothing is correcting them.
 *
 * The three measurements a guide loop cannot make about itself, because
 * making them means not guiding: how much of the motion is seeing and
 * therefore must not be chased, how much is steady drift and therefore
 * polar misalignment, and how much travel declination loses when it
 * reverses.
 */
export function AssistantReport({ task, onClose }: { task: Task; onClose: () => void }) {
  const queryClient = useQueryClient();
  const detail = task.detail as {
    seconds?: number;
    frames?: number;
    elapsed_s?: number;
    lost_frames?: number;
    drift_x_arcsec?: number;
    drift_y_arcsec?: number;
    ra_drift_arcsec_per_min?: number;
    dec_drift_arcsec_per_min?: number;
    ra_seeing_arcsec?: number;
    dec_seeing_arcsec?: number;
    ra_peak_to_peak_arcsec?: number;
    dec_peak_to_peak_arcsec?: number;
    polar_error_arcmin?: number | null;
    polar_error_confidence?: string;
    backlash_arcsec?: number | null;
    backlash_ms?: number | null;
    recommendations?: string[];
    suggested_min_move_arcsec?: number | null;
    suggested_dec_mode?: string | null;
  };

  const apply = useMutation({
    mutationFn: (changes: Record<string, unknown>) => api.guiding.updateSettings(changes),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["guiding-settings"] }),
  });

  const running = task.state === "running";
  const done = task.step === "done";

  return (
    <Modal
      title="Guiding assistant"
      subtitle={<span className="mono">{task.step === "done" ? "finished" : task.step}</span>}
      onClose={onClose}
    >
      <p className="small faint" style={{ margin: 0 }}>
        Guiding is off while this runs. It watches the star drift and separates what the loop
        should chase from what it should not - seeing cannot be corrected, and trying moves the
        mount without improving the image.
      </p>

      {running && (
        <>
          {task.fraction != null && (
            <div className="bar">
              <span style={{ width: `${Math.round(task.fraction * 100)}%` }} />
            </div>
          )}
          <div className="spread">
            <Field label="Frames" value={String(detail.frames ?? 0)} />
            <Field
              label="Watched"
              value={`${(detail.elapsed_s ?? 0).toFixed(0)}s of ${(detail.seconds ?? 0).toFixed(0)}s`}
            />
            <Field
              label="Moved so far"
              value={
                detail.drift_x_arcsec == null
                  ? "--"
                  : `${detail.drift_x_arcsec.toFixed(1)}, ${detail.drift_y_arcsec?.toFixed(1)}"`
              }
            />
            <Field label="Lost" value={String(detail.lost_frames ?? 0)} />
          </div>
        </>
      )}

      {done && (
        <>
          <div className="spread">
            <Field
              label="Seeing"
              value={arcsec(
                Math.max(detail.ra_seeing_arcsec ?? 0, detail.dec_seeing_arcsec ?? 0),
                2,
              )}
            />
            <Field
              label="RA drift"
              value={`${(detail.ra_drift_arcsec_per_min ?? 0).toFixed(2)}"/min`}
            />
            <Field
              label="Dec drift"
              value={`${(detail.dec_drift_arcsec_per_min ?? 0).toFixed(2)}"/min`}
            />
            <Field label="Frames" value={String(detail.frames ?? 0)} />
          </div>

          {/*
            The polar figure comes with its own caveat, because it is a
            conversion of the declination drift rather than a measurement
            of the axis - and what the drift shows depends on where in
            the sky it was measured.
          */}
          <div className="stack small">
            <div className="spread">
              <span className="label">Polar misalignment</span>
              <span className="mono">
                {detail.polar_error_arcmin == null
                  ? "not measurable here"
                  : `about ${detail.polar_error_arcmin.toFixed(1)} arcmin`}
              </span>
            </div>
            <div className="small faint">{detail.polar_error_confidence}</div>
          </div>

          {detail.backlash_ms != null && (
            <div className="spread">
              <Field label="Dec backlash" value={arcsec(detail.backlash_arcsec ?? 0, 1)} />
              <Field label="In time" value={`${detail.backlash_ms.toFixed(0)} ms`} />
              <Field
                label="Peak to peak"
                value={arcsec(detail.dec_peak_to_peak_arcsec ?? 0, 1)}
              />
            </div>
          )}

          {(detail.recommendations ?? []).length > 0 && (
            <div className="stack small">
              <div className="label">What to do about it</div>
              {(detail.recommendations ?? []).map((line) => (
                <div key={line} className="small">
                  {line}
                </div>
              ))}
            </div>
          )}

          {/*
            Applied here rather than copied out by hand. The numbers came
            from a measurement; retyping them into another panel is an
            opportunity to get one wrong.
          */}
          <div className="row modal-actions">
            {detail.suggested_min_move_arcsec != null && (
              <button
                className="ghost"
                disabled={apply.isPending}
                onClick={() =>
                  apply.mutate({ min_move_arcsec: detail.suggested_min_move_arcsec })
                }
              >
                Set dead band to {detail.suggested_min_move_arcsec.toFixed(2)}&Prime;
              </button>
            )}
            {detail.suggested_dec_mode && (
              <button
                className="ghost"
                disabled={apply.isPending}
                onClick={() => apply.mutate({ dec_mode: detail.suggested_dec_mode })}
              >
                Guide Dec {detail.suggested_dec_mode} only
              </button>
            )}
            <button className="primary" onClick={onClose}>
              Done
            </button>
          </div>
        </>
      )}

      <ErrorNote error={task.error ?? apply.error} />

      {running && (
        <div className="row modal-actions">
          <button className="ghost" onClick={onClose}>
            Hide
          </button>
        </div>
      )}
    </Modal>
  );
}
