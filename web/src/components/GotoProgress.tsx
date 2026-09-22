import { useMutation } from "@tanstack/react-query";
import { api } from "../lib/api";
import { arcmin, degrees, formatDms, formatHms } from "../lib/format";
import { STAGES, currentStage, separationDeg, useStageElapsed } from "../lib/stages";
import type { Stage } from "../lib/stages";
import type { Task } from "../lib/types";
import { useExposureRemaining } from "../lib/useExposure";
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
    stage_seconds?: Partial<Record<string, number>>;
    passes_done?: {
      pass: number;
      error_arcmin: number;
      stars: number;
      seconds: number;
      within: boolean;
    }[];
  };

  const running = task.state === "running";
  const mount = telemetry.mount;
  const pass = detail.iteration ?? 0;
  const passes = detail.max_passes ?? 0;
  const tolerance = detail.tolerance_arcmin;
  const error = detail.error_arcmin;
  const within = error != null && tolerance != null && error <= tolerance;

  const stage = currentStage(task, telemetry);
  const elapsed = useStageElapsed(stage, pass);
  // Reported by the task, which timed them, rather than measured here.
  const done = detail.stage_seconds ?? {};
  const remaining = useExposureRemaining(telemetry.camera);
  const cameraState = telemetry.camera?.state;
  // How far the mount still has to turn. Computed here from two things
  // already on screen, rather than asked for: it is the one number that
  // makes a long slew legible while it happens.
  const toGo =
    mount != null && detail.target_ra_deg != null && detail.target_dec_deg != null
      ? separationDeg(mount, { ra_deg: detail.target_ra_deg, dec_deg: detail.target_dec_deg })
      : null;

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
        The four things the loop does, over and over, with the one that
        is happening now saying how long it has been happening. The task
        reports "solving" for the whole middle of this - a four second
        exposure, a readout and a plate solve - which from outside is one
        opaque wait, and which one of the three it is stuck on is the
        whole question when a run takes longer than it should.
      */}
      <div className="stages">
        {STAGES.map((entry) => (
          <StageRow
            key={entry.id}
            id={entry.id}
            label={entry.label}
            active={stage === entry.id}
            done={done[entry.id] != null}
            seconds={stage === entry.id ? elapsed : done[entry.id]}
            note={stageNote(entry.id, stage, { toGo, remaining, cameraState })}
          />
        ))}
      </div>

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

      {/*
        Every pass, so the convergence is visible as a shape rather than
        as a number that changes. Two lines going 28' then 0.2' says the
        loop is working; two lines going 28' then 26' says it is not.
      */}
      {detail.passes_done != null && detail.passes_done.length > 0 && (
        <div className="passes">
          {detail.passes_done.map((entry) => (
            <div key={entry.pass} className="spread small">
              <span className="faint">Pass {entry.pass}</span>
              <span className={`mono ${entry.within ? "good" : ""}`}>
                {arcmin(entry.error_arcmin, 2)}
              </span>
              <span className="faint mono">{entry.stars} stars</span>
              <span className="faint mono">{entry.seconds.toFixed(1)}s</span>
            </div>
          ))}
        </div>
      )}

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

/** One stage, with a dot that says whether it is done, doing or waiting. */
function StageRow({
  label,
  active,
  done,
  seconds,
  note,
}: {
  id: Stage;
  label: string;
  active: boolean;
  done: boolean;
  seconds?: number | null;
  note?: string | null;
}) {
  return (
    <div className={`stage ${active ? "active" : done ? "done" : ""}`}>
      <span className={`dot ${active ? "busy" : done ? "live" : ""}`} />
      <span className="stage-label">{label}</span>
      <span className="stage-note small faint">{note ?? ""}</span>
      {/* Blank rather than zero for a stage this browser did not see start. */}
      <span className="mono small">
        {seconds == null || Number.isNaN(seconds) ? "" : `${seconds.toFixed(1)}s`}
      </span>
    </div>
  );
}

/** What a stage has to say for itself while it is the one running. */
function stageNote(
  id: Stage,
  stage: Stage,
  live: { toGo: number | null; remaining: number | null; cameraState?: string },
): string | null {
  if (id !== stage) return null;
  if (id === "slew" && live.toGo != null) return `${degrees(live.toGo, 2)} to go`;
  if (id === "expose") {
    // After the shutter closes there are still seconds of readout and
    // download, and on a 26 megapixel sensor that is most of the wait.
    if (live.cameraState === "reading") return "reading out";
    if (live.cameraState === "downloading") return "downloading";
    if (live.remaining != null) return `${live.remaining.toFixed(1)}s left`;
  }
  if (id === "solve") return "matching stars against the catalogue";
  if (id === "sync") return "telling the mount where it really is";
  return null;
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
