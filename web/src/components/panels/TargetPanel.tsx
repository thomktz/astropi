import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import {
  altitudeQuality,
  clockTime,
  degrees,
  duration,
  formatDms,
  formatHms,
} from "../../lib/format";
import { dotClass, mountHealth } from "../../lib/status";
import type { Target } from "../../lib/types";
import type { Telemetry } from "../../lib/useTelemetry";
import { AltitudeChart } from "../AltitudeChart";
import { RollingNumber } from "../RollingNumber";
import { ErrorNote, Field, Section } from "../Field";

/** Nudge step sizes, in milliseconds of mount pulse. */
const NUDGE_STEPS = [
  { ms: 100, label: "0.1s" },
  { ms: 500, label: "0.5s" },
  { ms: 2000, label: "2s" },
  { ms: 5000, label: "5s" },
  { ms: 20_000, label: "20s" },
  { ms: 60_000, label: "60s" },
];
const NUDGE_KEY = "astropi.nudgeMs";

type Direction = "north" | "south" | "east" | "west";

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
  const [step, setStep] = useState(() => {
    try {
      const stored = Number(localStorage.getItem(NUDGE_KEY));
      return NUDGE_STEPS.some((option) => option.ms === stored) ? stored : 500;
    } catch {
      return 500;
    }
  });

  useEffect(() => {
    try {
      localStorage.setItem(NUDGE_KEY, String(step));
    } catch {
      // A remembered step size is not worth a blank screen.
    }
  }, [step]);

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
  // Wrapped here rather than in the formatter, since the rolling readout
  // takes a number rather than a formatted string.
  const normalizedAzimuth =
    mount?.az_deg == null ? null : ((mount.az_deg % 360) + 360) % 360;

  /**
   * Nudge from any state, unparking first where it has to.
   *
   * Its own mutation rather than sharing `act`, so the readout can name the
   * direction being pulsed and the keypad can lock for the duration -
   * without park and track lighting up the same indicator.
   */
  const nudge = useMutation({
    mutationFn: async ({ direction, duration }: { direction: Direction; duration: number }) => {
      if (parked) await api.mount.unpark();
      return api.mount.pulse(direction, duration);
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["mount"] }),
  });
  const nudging = nudge.isPending ? nudge.variables.direction : null;
  const run = (direction: Direction, duration: number) => nudge.mutate({ direction, duration });


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
          {/*
            Three decimals: a thousandth of a degree is 3.6 arcseconds, and
            both the things worth watching here are smaller than the single
            decimal this used to show - a nudge moves tens of arcseconds,
            and the horizon drift that tracking cancels runs at a few
            thousandths of a degree per second.
          */}
          <Field
            label="Altitude"
            value={<RollingNumber value={mount?.alt_deg} decimals={3} suffix="°" />}
          />
          <Field
            label="Azimuth"
            value={<RollingNumber value={normalizedAzimuth} decimals={3} suffix="°" />}
          />
        </div>

        <div className="spread">
          <span className={`pill ${nudging ? "fair" : ""}`}>
            <span className={`dot ${nudging ? "busy" : dotClass(mountHealth(mount))}`} />
            {nudging
              ? `nudging ${nudging}…`
              : mount?.tracking
                ? "tracking · sidereal"
                : (mount?.state ?? "unknown")}
          </span>
          <span className="row" style={{ flex: "0 0 auto", gap: 6 }}>
            {/*
              Starts tracking wherever the telescope already points, with no
              slew - for a manually framed target, or to hold position while
              polar aligning. It unparks first, because otherwise this was
              greyed out on a freshly booted rig and looked impossible.
            */}
            <button
              className="ghost"
              onClick={() =>
                act.mutate(async () => {
                  if (mount?.tracking) return api.mount.tracking(false);
                  if (parked) await api.mount.unpark();
                  return api.mount.tracking(true);
                })
              }
              title={
                mount?.tracking
                  ? "Stop following the sky"
                  : "Follow the sky from where the telescope is pointing now, without slewing"
              }
            >
              {mount?.tracking ? "Stop tracking" : "Track here"}
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

      <Section title="Nudge">
        <div className="row quick">
          {NUDGE_STEPS.map((option) => (
            <button
              key={option.ms}
              className="ghost"
              aria-pressed={step === option.ms}
              onClick={() => setStep(option.ms)}
              title={`Pulse the mount for ${option.label} per press`}
            >
              {option.label}
            </button>
          ))}
        </div>
        <div className="keypad">
          <span className="spacer" />
          <NudgeButton direction="north" label="N" step={step} onNudge={run} busy={nudging} />
          <span className="spacer" />
          <NudgeButton direction="west" label="W" step={step} onNudge={run} busy={nudging} />
          <span className="spacer" />
          <NudgeButton direction="east" label="E" step={step} onNudge={run} busy={nudging} />
          <span className="spacer" />
          <NudgeButton direction="south" label="S" step={step} onNudge={run} busy={nudging} />
          <span className="spacer" />
        </div>
        <ErrorNote error={nudge.error} />
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

    </>
  );
}

function NudgeButton({
  direction,
  label,
  step,
  busy,
  onNudge,
}: {
  direction: Direction;
  label: string;
  step: number;
  busy: Direction | null;
  onNudge: (direction: Direction, step: number) => void;
}) {
  return (
    <button
      // Locked for the length of the pulse. A five second nudge is five
      // seconds of a mount quietly moving, and without this the only
      // feedback was the button looking exactly as it did before.
      disabled={busy !== null}
      aria-label={`Nudge ${direction}`}
      aria-busy={busy === direction}
      className={busy === direction ? "primary" : undefined}
      onClick={() => onNudge(direction, step)}
    >
      {label}
    </button>
  );
}
