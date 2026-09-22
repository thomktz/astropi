import { useEffect, useState } from "react";
import type { Task } from "./types";
import type { Telemetry } from "./useTelemetry";

/**
 * The four things a centring run does, over and over.
 *
 * The task reports one step for the whole middle of it - "solving" -
 * which covers a four second exposure, a readout and a plate solve. From
 * outside those are one opaque wait; from the camera's telemetry they
 * are three different things, and which one it is stuck on is the whole
 * question when a run takes longer than it should.
 */
export type Stage = "slew" | "expose" | "solve" | "sync" | "done";

export const STAGES: { id: Stage; label: string }[] = [
  { id: "slew", label: "Slew" },
  { id: "expose", label: "Expose" },
  { id: "solve", label: "Solve" },
  { id: "sync", label: "Sync" },
];

export function currentStage(task: Task, telemetry: Telemetry): Stage {
  if (task.state !== "running") return "done";
  switch (task.step) {
    case "slewing":
    case "slewed":
      return "slew";
    case "solved":
      return "sync";
    case "centred":
    case "not_converged":
      return "done";
    default: {
      // "solving" covers exposing and solving both. The camera says which.
      const state = telemetry.camera?.state;
      const busy = state === "exposing" || state === "reading" || state === "downloading";
      return busy ? "expose" : "solve";
    }
  }
}

/**
 * Seconds in the stage running now, ticking.
 *
 * Only the live one. How long the *finished* stages took is reported by
 * the task, which knows where the boundaries are - a browser watching
 * step names has to infer them, and one that joined the run halfway
 * through cannot know at all, which is how "Slew 0.0s" ended up on
 * screen underneath a slew that had taken eight seconds.
 */
export function useStageElapsed(stage: Stage, pass: number): number | null {
  const [now, setNow] = useState(() => Date.now());
  const [entered, setEntered] = useState(() => ({ stage, pass, since: Date.now() }));

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(timer);
  }, []);

  if (entered.stage !== stage || entered.pass !== pass) {
    setEntered({ stage, pass, since: now });
  }
  return Math.max(0, (now - entered.since) / 1000);
}

/** Angular distance between two coordinates, in degrees. */
export function separationDeg(
  a: { ra_deg: number; dec_deg: number },
  b: { ra_deg: number; dec_deg: number },
): number {
  const toRad = Math.PI / 180;
  const dec1 = a.dec_deg * toRad;
  const dec2 = b.dec_deg * toRad;
  const dRa = (b.ra_deg - a.ra_deg) * toRad;
  // Vincenty: stable at both ends, unlike the cosine formula, which
  // loses all its precision exactly where a centring run lives.
  const x = Math.cos(dec2) * Math.sin(dRa);
  const y = Math.cos(dec1) * Math.sin(dec2) - Math.sin(dec1) * Math.cos(dec2) * Math.cos(dRa);
  const z = Math.sin(dec1) * Math.sin(dec2) + Math.cos(dec1) * Math.cos(dec2) * Math.cos(dRa);
  return Math.atan2(Math.hypot(x, y), z) / toRad;
}
