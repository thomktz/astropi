import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { formatDms, formatHms } from "../lib/format";
import type { Telemetry } from "../lib/useTelemetry";
import { ErrorNote, Field, Panel } from "./Panel";

const NUDGE_MS = 800;

/**
 * Direct mount control. The GoTo that plate-solves lives in the target
 * panel; this is the manual layer underneath it - park, track, nudge.
 */
export function MountPanel({ telemetry }: { telemetry: Telemetry }) {
  const queryClient = useQueryClient();
  const { data: status } = useQuery({
    queryKey: ["mount"],
    queryFn: api.mount.status,
    // Position arrives over the socket; this is only for the fields the
    // socket does not carry, so it can refresh slowly.
    refetchInterval: 10_000,
  });

  const act = useMutation({
    mutationFn: (action: () => Promise<unknown>) => action(),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["mount"] }),
  });

  const live = telemetry.mount;
  const tracking = live?.tracking ?? status?.tracking ?? false;
  const parked = (live?.state ?? status?.state) === "parked";
  const slewing = (live?.state ?? status?.state) === "slewing";

  // Prefer the socket: it pushes a position every second, so the readout
  // stays correct through a slew rather than lagging the polling interval.
  const ra = live ? formatHms(live.ra_deg) : (status?.coord.ra_hms ?? "--");
  const dec = live ? formatDms(live.dec_deg) : (status?.coord.dec_dms ?? "--");

  return (
    <Panel title="Mount">
      <div className="spread">
        <Field label="Right ascension" value={<span className="mono">{ra}</span>} />
        <Field label="Declination" value={<span className="mono">{dec}</span>} />
      </div>

      <div className="row">
        <button onClick={() => act.mutate(() => api.mount.tracking(!tracking))} disabled={parked}>
          {tracking ? "Stop tracking" : "Start tracking"}
        </button>
        {parked ? (
          <button onClick={() => act.mutate(api.mount.unpark)}>Unpark</button>
        ) : (
          <button onClick={() => act.mutate(api.mount.park)}>Park</button>
        )}
        <button className="danger" onClick={() => act.mutate(api.mount.abort)} disabled={!slewing}>
          Abort
        </button>
      </div>

      <div>
        <div className="label" style={{ marginBottom: 6 }}>
          Nudge ({NUDGE_MS} ms)
        </div>
        <div className="keypad">
          <span className="spacer" />
          <NudgeButton direction="north" label="N" disabled={parked} onNudge={act.mutate} />
          <span className="spacer" />
          <NudgeButton direction="west" label="W" disabled={parked} onNudge={act.mutate} />
          <span className="spacer" />
          <NudgeButton direction="east" label="E" disabled={parked} onNudge={act.mutate} />
          <span className="spacer" />
          <NudgeButton direction="south" label="S" disabled={parked} onNudge={act.mutate} />
          <span className="spacer" />
        </div>
      </div>

      <ErrorNote error={act.error} />
    </Panel>
  );
}

function NudgeButton({
  direction,
  label,
  disabled,
  onNudge,
}: {
  direction: "north" | "south" | "east" | "west";
  label: string;
  disabled: boolean;
  onNudge: (action: () => Promise<unknown>) => void;
}) {
  return (
    <button
      disabled={disabled}
      aria-label={`Nudge ${direction}`}
      onClick={() => onNudge(() => api.mount.pulse(direction, NUDGE_MS))}
    >
      {label}
    </button>
  );
}
