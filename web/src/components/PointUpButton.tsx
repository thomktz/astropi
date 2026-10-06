import { useMutation } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { Telemetry } from "../lib/useTelemetry";
import { ErrorNote } from "./Field";

/**
 * Slew to the zenith and stop tracking there, for flats.
 *
 * While the move runs the button stops it: cancelling the task halts the
 * motors, and the mount's own Abort does too.
 */
export function PointUpButton({
  telemetry,
  busy,
  className = "ghost",
}: {
  telemetry: Telemetry;
  busy: boolean;
  className?: string;
}) {
  const task = telemetry.task;
  const moving = task?.kind === "zenith" && task.state === "running";
  const start = useMutation({ mutationFn: api.tasks.zenith });
  const stop = useMutation({
    mutationFn: async () => {
      if (task) await api.tasks.cancel(task.id);
      await api.mount.abort();
    },
  });

  return (
    <>
      {moving ? (
        <button className="ghost danger" onClick={() => stop.mutate()}>
          Stop slew
        </button>
      ) : (
        <button
          className={className}
          disabled={busy || start.isPending}
          onClick={() => start.mutate()}
          title="Slew to the zenith (hour angle 0, declination = your latitude) on the side of the mount it is already on, then stop tracking so it stays pointing up. For flats."
        >
          Point straight up
        </button>
      )}
      <ErrorNote error={start.error ?? stop.error} />
    </>
  );
}
