import { useMutation } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { Telemetry } from "../lib/useTelemetry";

/**
 * The mount halted itself, over everything else until dealt with.
 *
 * Not a status word in the strip: a mount that stopped because an axis
 * would not - or because something else was driving it - needs someone to
 * go and look at it before it moves again, and it refuses to until then.
 */
export function MountFault({ telemetry }: { telemetry: Telemetry }) {
  const fault = telemetry.mount?.fault;
  const clear = useMutation({ mutationFn: api.mount.clearFault });
  if (!fault) return null;

  return (
    <div className="mount-fault" role="alert">
      <div className="stack">
        <strong>Mount halted</strong>
        <span>{fault}</span>
        <span className="small">
          Both axes were emergency-stopped and it will not move until the fault is cleared. Check
          the cables and where it points first; if an axis is still turning, cut its power.
        </span>
        {clear.error && <span className="small">{(clear.error as Error).message}</span>}
      </div>
      <button className="ghost" onClick={() => clear.mutate()} disabled={clear.isPending}>
        Clear fault
      </button>
    </div>
  );
}
