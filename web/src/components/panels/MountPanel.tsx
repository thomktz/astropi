import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import {
  bearing,
  degrees,
  durationShort,
  formatDms,
  formatHms,
  hoursToMeridian,
  meridianIsMeaningful,
} from "../../lib/format";
import type { Telemetry } from "../../lib/useTelemetry";
import { ErrorNote, Field, Section } from "../Field";

const NUDGE_MS = 800;

/**
 * Where the telescope is pointing, and the mount's own controls.
 *
 * Kept apart from guiding on purpose. Tracking is the mount turning at a
 * constant rate to cancel the earth's rotation; guiding is a closed loop
 * watching a star and nudging the mount to correct what tracking got wrong.
 * They fail independently, and collapsing them into one "is it following
 * the sky" readout hides which of the two needs attention.
 */
export function MountPanel({ telemetry }: { telemetry: Telemetry }) {
  const queryClient = useQueryClient();
  const status = useQuery({ queryKey: ["mount"], queryFn: api.mount.status, refetchInterval: 10_000 });

  const act = useMutation({
    mutationFn: (action: () => Promise<unknown>) => action(),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["mount"] }),
  });

  const live = telemetry.mount;
  const tracking = live?.tracking ?? status.data?.tracking ?? false;
  const state = live?.state ?? status.data?.state ?? "idle";
  const parked = state === "parked";
  const slewing = state === "slewing";
  const hourAngle = live?.hour_angle_deg ?? status.data?.hour_angle_deg ?? null;

  return (
    <>
      <Section title="Pointing">
        {/*
          What the rig is on, not just where it is aimed. The mount cannot
          know this - it reports a coordinate - so it comes from the
          observatory's record of the last GoTo.
        */}
        {telemetry.target ? (
          <div className="spread">
            <span className="name">{telemetry.target.display_name}</span>
            <span className="small faint">
              {telemetry.target.object_type} &middot; {degrees(telemetry.target.altitude_deg, 0)}
            </span>
          </div>
        ) : (
          <div className="small faint">
            No target. A GoTo records what it slewed to; a manual slew does not.
          </div>
        )}

        <div className="spread">
          <Field
            label="Right ascension"
            value={<span className="mono">{live ? formatHms(live.ra_deg) : "--"}</span>}
          />
          <Field
            label="Declination"
            value={<span className="mono">{live ? formatDms(live.dec_deg) : "--"}</span>}
          />
        </div>
        <div className="spread">
          <Field label="Altitude" value={degrees(live?.alt_deg, 1)} />
          <Field label="Azimuth" value={bearing(live?.az_deg, 1)} />
          {hourAngle != null && meridianIsMeaningful(live?.dec_deg) && (
            <Field
              label={hoursToMeridian(hourAngle) < 0 ? "Past meridian" : "To meridian"}
              value={durationShort(Math.abs(hoursToMeridian(hourAngle)))}
            />
          )}
        </div>
      </Section>

      <Section title="Tracking">
        <p className="small dim" style={{ margin: 0 }}>
          Turning at sidereal rate to cancel the earth&apos;s rotation. A GoTo switches this on by
          itself when the slew lands.
        </p>
        <div className="spread">
          <span className="pill">
            <span className={`dot ${tracking ? "live" : ""}`} />
            {tracking ? `tracking · ${status.data?.tracking_rate ?? "sidereal"}` : "not tracking"}
          </span>
          <button
            style={{ flex: "0 0 auto" }}
            disabled={parked}
            onClick={() => act.mutate(() => api.mount.tracking(!tracking))}
          >
            {tracking ? "Stop" : "Start"}
          </button>
        </div>
      </Section>

      <Section title="Mount">
        <div className="row">
          <button onClick={() => act.mutate(parked ? api.mount.unpark : api.mount.park)}>
            {parked ? "Unpark" : "Park"}
          </button>
          <button className="danger" onClick={() => act.mutate(api.mount.abort)} disabled={!slewing}>
            Abort slew
          </button>
        </div>

        <div className="label">Nudge ({NUDGE_MS} ms)</div>
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
      </Section>

      <ErrorNote error={act.error} />
    </>
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
