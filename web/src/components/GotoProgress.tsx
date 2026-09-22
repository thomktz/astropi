import { useMutation } from "@tanstack/react-query";
import { api } from "../lib/api";
import { arcmin, degrees, formatDms, formatHms } from "../lib/format";
import type { Task } from "../lib/types";
import type { Telemetry } from "../lib/useTelemetry";
import { ErrorNote, Field } from "../components/Field";
import { Modal } from "./Modal";

/**
 * What a GoTo is doing while it does it.
 *
 * A centring run is the one operation where the interesting part is the
 * loop rather than the result: it slews, photographs where it actually
 * landed, works out how far off that is, tells the mount the truth and
 * goes again. From the outside it looks like a minute of nothing, which
 * is exactly how it looked the first time this drove a real mount - and
 * the numbers it was producing along the way explained the mount's
 * behaviour better than anything else on screen.
 *
 * Reads the task's own reported values rather than parsing its messages,
 * which is why the task reports numbers as well as sentences.
 */
export function GotoProgress({
  task,
  telemetry,
  onClose,
}: {
  task: Task;
  telemetry: Telemetry;
  onClose: () => void;
}) {
  const cancel = useMutation({ mutationFn: () => api.tasks.cancel(task.id) });

  const detail = task.detail as {
    target_ra_deg?: number;
    target_dec_deg?: number;
    target_name?: string | null;
    tolerance_arcmin?: number;
    max_passes?: number;
    exposure_s?: number;
    iteration?: number;
    error_arcmin?: number;
    within_tolerance?: boolean;
    solved_ra_deg?: number;
    solved_dec_deg?: number;
    stars?: number;
    solver?: string;
    solve_seconds?: number;
    pixel_scale_arcsec?: number;
    rotation_deg?: number;
    passes?: number;
  };

  const running = task.state === "running";
  const mount = telemetry.mount;
  const pass = detail.iteration ?? 0;
  const passes = detail.max_passes ?? 0;
  const tolerance = detail.tolerance_arcmin;
  const error = detail.error_arcmin;
  const within = error != null && tolerance != null && error <= tolerance;

  return (
    <Modal
      title={task.name}
      subtitle={<span className="mono">{phrase(task.step, pass, passes)}</span>}
      onClose={onClose}
    >
      {task.fraction != null && (
        <div className="bar">
          <span style={{ width: `${Math.round(task.fraction * 100)}%` }} />
        </div>
      )}

      {/*
        The three coordinates that matter, in the order the loop uses
        them: where it is going, where the mount thinks it is, and where
        the sky says it actually is. The third is the only measurement
        among them.
      */}
      <div className="stack small">
        <div className="spread">
          <span className="label">Heading for</span>
          <span className="mono">
            {detail.target_ra_deg != null && detail.target_dec_deg != null
              ? `${formatHms(detail.target_ra_deg)} ${formatDms(detail.target_dec_deg)}`
              : "--"}
          </span>
        </div>
        <div className="spread">
          <span className="label">Mount reports</span>
          <span className="mono faint">
            {mount ? `${formatHms(mount.ra_deg)} ${formatDms(mount.dec_deg)}` : "--"}
          </span>
        </div>
        <div className="spread">
          <span className="label">Sky says</span>
          <span className="mono">
            {detail.solved_ra_deg != null && detail.solved_dec_deg != null
              ? `${formatHms(detail.solved_ra_deg)} ${formatDms(detail.solved_dec_deg)}`
              : "not solved yet"}
          </span>
        </div>
      </div>

      <div className="spread">
        <Field
          label="Off target"
          value={error == null ? "--" : arcmin(error, 2)}
          tone={error == null ? undefined : within ? "good" : "fair"}
        />
        <Field label="Within" value={tolerance == null ? "--" : arcmin(tolerance, 1)} />
        <Field label="Pass" value={passes ? `${Math.max(pass, 1)} of ${passes}` : "--"} />
      </div>

      <div className="spread">
        <Field label="Stars" value={detail.stars == null ? "--" : String(detail.stars)} />
        <Field label="Solver" value={detail.solver ?? "--"} />
        <Field
          label="Solve time"
          value={detail.solve_seconds == null ? "--" : `${detail.solve_seconds.toFixed(1)}s`}
        />
        <Field
          label="Field angle"
          value={detail.rotation_deg == null ? "--" : degrees(detail.rotation_deg, 1)}
        />
      </div>

      {/*
        The running commentary, which is where a failure explains itself -
        a solve that found four stars, a cloud, a mount that would not
        stop. Oldest first: six lines of narrative read forwards.
      */}
      <div className="log chronological">
        {task.messages.slice(-6).map((line, index) => (
          <div key={`${index}-${line}`}>{line}</div>
        ))}
      </div>

      <ErrorNote error={task.error ?? cancel.error} />

      <div className="row modal-actions">
        {running ? (
          <>
            <button className="ghost" onClick={onClose}>
              Hide
            </button>
            <button
              className="ghost danger"
              disabled={cancel.isPending}
              onClick={() => cancel.mutate()}
            >
              Abort
            </button>
          </>
        ) : (
          <button className="primary" onClick={onClose}>
            Done
          </button>
        )}
      </div>
    </Modal>
  );
}

/** The step name as a sentence, since "solved" alone says little. */
function phrase(step: string, pass: number, passes: number): string {
  switch (step) {
    case "slewing":
      return "slewing";
    case "slewed":
      return "arrived, tracking";
    case "solving":
      return passes ? `exposing for solve ${pass} of ${passes}` : "exposing to solve";
    case "solved":
      return "solved, correcting";
    case "solve_failed":
      return "solve failed, retrying";
    case "centred":
      return "centred";
    case "not_converged":
      return "gave up";
    default:
      return step;
  }
}
