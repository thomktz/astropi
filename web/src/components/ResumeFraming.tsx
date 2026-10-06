import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../lib/api";
import type { Telemetry } from "../lib/useTelemetry";
import { ErrorNote, Section } from "./Field";

/**
 * Pick up a target from an earlier night, framed exactly as it was.
 *
 * Choose one of the saved lights; the rig plate solves it, centres on it,
 * then keeps solving while you turn the camera and says which way and how
 * far until the angle matches. New lights then land beside the old ones.
 */
export function ResumeFraming({ telemetry, busy }: { telemetry: Telemetry; busy: boolean }) {
  const references = useQuery({ queryKey: ["reframe-references"], queryFn: api.tasks.reframeReferences });
  const [picked, setPicked] = useState<string | null>(null);
  const [frame, setFrame] = useState<string | null>(null);

  const start = useMutation({ mutationFn: (reference: string) => api.tasks.reframe({ reference }) });

  const groups = references.data ?? [];
  const group = groups.find((g) => `${g.night}/${g.target}` === picked) ?? null;
  const reference = frame ?? group?.latest ?? null;

  const task = telemetry.task?.kind === "reframe" ? telemetry.task : null;
  const running = task?.state === "running";
  const detail = (task?.detail ?? {}) as {
    rotate_deg?: number;
    direction?: string;
    offset_arcmin?: number;
  };
  const turning = running && task?.step === "rotate" && detail.rotate_deg != null;

  if (groups.length === 0 && !running) {
    return (
      <Section title="Resume a target">
        <div className="small faint">No saved lights yet. Shoot a night first.</div>
      </Section>
    );
  }

  return (
    <Section
      title="Resume a target"
      hint="Adding a night only helps if the frames overlap: same centre, same camera angle. This solves the frame you pick, centres on it, then tells you how to turn the camera until the angle matches."
    >
      {!running && (
        <>
          <div className="stack small" style={{ maxHeight: 180, overflowY: "auto" }}>
            {groups.map((g) => {
              const key = `${g.night}/${g.target}`;
              return (
                <button
                  key={key}
                  className="result"
                  aria-pressed={key === picked}
                  onClick={() => {
                    setPicked(key);
                    setFrame(null);
                  }}
                >
                  <span style={{ flex: 1 }}>{g.target}</span>
                  <span className="faint mono">
                    {g.night} · {g.count}
                  </span>
                </button>
              );
            })}
          </div>

          {group && (
            <label className="stack small">
              <span className="label">Reference frame</span>
              <select value={reference ?? ""} onChange={(event) => setFrame(event.target.value)}>
                {[...group.frames].reverse().map((path) => (
                  <option key={path} value={path}>
                    {path.split("/").at(-1)}
                  </option>
                ))}
              </select>
            </label>
          )}

          <button
            className="primary"
            disabled={busy || !reference || start.isPending}
            onClick={() => reference && start.mutate(reference)}
          >
            Resume this framing
          </button>
        </>
      )}

      {running && task && (
        <div className="stack">
          {turning ? (
            <div className="stack" style={{ alignItems: "center", gap: 6 }}>
              <RotateArrow clockwise={detail.direction === "clockwise"} />
              <div style={{ fontSize: "1.6rem" }} className="mono">
                {detail.rotate_deg?.toFixed(1)}&#176;
              </div>
              <div>
                Turn the camera <strong>{detail.direction}</strong>
              </div>
              <div className="small faint">seen from behind the camera, looking at the sky</div>
            </div>
          ) : (
            <div className="small">{task.messages.at(-1)}</div>
          )}
          <button className="ghost" onClick={() => api.tasks.cancel(task.id)}>
            Stop
          </button>
        </div>
      )}

      {!running && task?.state === "succeeded" && (
        <div className="small good">{task.messages.at(-1)}</div>
      )}
      {!running && task?.state === "failed" && <div className="small poor">{task.error?.split("\n").at(-1)}</div>}
      <ErrorNote error={start.error} />
    </Section>
  );
}

function RotateArrow({ clockwise }: { clockwise: boolean }) {
  return (
    <svg
      width="72"
      height="72"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      style={{ transform: clockwise ? undefined : "scaleX(-1)" }}
      aria-hidden
    >
      <path d="M20 12a8 8 0 1 1-2.34-5.66" />
      <path d="M20 4v4.5h-4.5" />
    </svg>
  );
}
