import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import {
  altitudeQuality,
  bearing,
  clockTime,
  degrees,
  duration,
  durationShort,
  formatDms,
  formatHms,
  hoursToMeridian,
  meridianIsMeaningful,
} from "../../lib/format";
import { dotClass, mountHealth } from "../../lib/status";
import type { Target } from "../../lib/types";
import type { Telemetry } from "../../lib/useTelemetry";
import { AltitudeChart } from "../AltitudeChart";
import { ErrorNote, Field, Section } from "../Field";

const NUDGE_MS = 800;

/**
 * Where the telescope is pointed, and where it should be.
 *
 * One panel because it is one job. Choosing a target and driving the mount
 * were split across two, which meant the GoTo button sat in a different
 * place from the tracking state it depends on, and picking something to
 * shoot told you nothing about whether the rig was even unparked.
 */
export function TargetPanel({ telemetry, busy }: { telemetry: Telemetry; busy: boolean }) {
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [selected, setSelected] = useState<Target | null>(null);

  useEffect(() => {
    // Every keystroke would otherwise re-rank a thousand objects and
    // recompute their altitudes on a Raspberry Pi.
    const timer = setTimeout(() => setDebounced(query.trim()), 220);
    return () => clearTimeout(timer);
  }, [query]);

  const results = useQuery({
    queryKey: ["targets", debounced],
    queryFn: () => (debounced ? api.targets.search(debounced) : api.targets.recommended()),
  });

  const visibility = useQuery({
    queryKey: ["visibility", selected?.id],
    queryFn: () => api.targets.visibility(selected!.id),
    enabled: selected != null,
  });

  const goto = useMutation({
    mutationFn: (target: Target) => api.tasks.goto({ target_id: target.id, center: true }),
  });

  const act = useMutation({
    mutationFn: (action: () => Promise<unknown>) => action(),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["mount"] }),
  });

  const mount = telemetry.mount;
  const active = telemetry.target;
  const parked = mount?.state === "parked";
  const slewing = mount?.state === "slewing";
  const hourAngle = mount?.hour_angle_deg ?? null;
  const showMeridian = hourAngle != null && meridianIsMeaningful(mount?.dec_deg);

  return (
    <>
      <Section title="Pointing">
        {/*
          What the rig is on, not just where it is aimed. The mount cannot
          know this - it reports a coordinate - so it comes from the
          observatory's record of the last GoTo.
        */}
        <div className="spread">
          <span className="name">{active ? active.display_name : "No target"}</span>
          <span className="small faint mono">
            {mount ? `${formatHms(mount.ra_deg)} ${formatDms(mount.dec_deg)}` : "--"}
          </span>
        </div>

        <div className="spread">
          <Field label="Altitude" value={degrees(mount?.alt_deg, 1)} />
          <Field label="Azimuth" value={bearing(mount?.az_deg, 1)} />
          {showMeridian && (
            <Field
              label={hoursToMeridian(hourAngle) < 0 ? "Past meridian" : "To meridian"}
              value={durationShort(Math.abs(hoursToMeridian(hourAngle)))}
            />
          )}
        </div>

        <div className="spread">
          <span className="pill">
            <span className={`dot ${dotClass(mountHealth(mount))}`} />
            {mount?.tracking ? "tracking · sidereal" : (mount?.state ?? "unknown")}
          </span>
          <span className="row" style={{ flex: "0 0 auto", gap: 6 }}>
            <button
              className="ghost"
              disabled={parked}
              onClick={() => act.mutate(() => api.mount.tracking(!mount?.tracking))}
            >
              {mount?.tracking ? "Stop tracking" : "Track"}
            </button>
            <button
              className="ghost"
              onClick={() => act.mutate(parked ? api.mount.unpark : api.mount.park)}
            >
              {parked ? "Unpark" : "Park"}
            </button>
            {slewing && (
              <button className="ghost danger" onClick={() => act.mutate(api.mount.abort)}>
                Abort
              </button>
            )}
          </span>
        </div>
      </Section>

      <Section title="Choose a target">
        <input
          type="search"
          value={query}
          placeholder="M31, Vega, Orion, NGC 7000"
          onChange={(event) => setQuery(event.target.value)}
          aria-label="Search the catalogue"
        />

        <div className="label">{debounced ? "Results" : "Highest right now"}</div>

        <div className="results">
          {results.isLoading && <div className="small faint">Loading…</div>}
          {results.data?.length === 0 && <div className="small faint">Nothing matches.</div>}
          {results.data?.map((target) => (
            <button
              key={target.id}
              className="result"
              aria-pressed={selected?.id === target.id}
              onClick={() => setSelected(target)}
            >
              <span style={{ minWidth: 0 }}>
                <div className="name">{target.display_name}</div>
                <div className="meta">
                  {target.name !== target.display_name && `${target.name} · `}
                  {target.object_type}
                  {target.magnitude < 90 && ` · mag ${target.magnitude.toFixed(1)}`}
                </div>
              </span>
              <span className={`alt ${altitudeQuality(target.altitude_deg)}`}>
                {degrees(target.altitude_deg, 0)}
              </span>
            </button>
          ))}
        </div>

        {selected && (
          <div className="stack selected">
            <div className="spread">
              <div style={{ minWidth: 0 }}>
                <div className="name">{selected.display_name}</div>
                <div className="mono small dim">
                  {selected.coord.ra_hms} {selected.coord.dec_dms}
                </div>
              </div>
              <button
                className="primary"
                disabled={busy || goto.isPending}
                onClick={() => goto.mutate(selected)}
                style={{ flex: "0 0 auto" }}
              >
                {busy ? "Rig busy" : "GoTo & centre"}
              </button>
            </div>

            {visibility.data && (
              <>
                <AltitudeChart visibility={visibility.data} />
                <div className="row small dim facts">
                  <span>
                    {visibility.data.circumpolar
                      ? "Never sets"
                      : visibility.data.never_rises
                        ? "Never rises here"
                        : `Sets ${clockTime(visibility.data.sets_at)}`}
                  </span>
                  <span>Transit {clockTime(visibility.data.transit_at)}</span>
                  <span>Peak {degrees(visibility.data.max_altitude_deg, 0)}</span>
                  <span>{duration(visibility.data.hours_above_horizon)} up</span>
                  <span>Moon {degrees(visibility.data.moon_separation_deg, 0)}</span>
                </div>
              </>
            )}

            <ErrorNote error={goto.error} />
          </div>
        )}
      </Section>

      <Section title="Nudge">
        {/* Last, because it is the least used thing here: a manual framing
            tweak after the solve has already put you close. */}
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
        <ErrorNote error={act.error} />
      </Section>
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
