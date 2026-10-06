import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import { clockTime, duration, moonPhaseName } from "../../lib/format";
import type { ActiveTarget, FrameGroup, FrameGroupKind, ImagingSession, Target } from "../../lib/types";
import type { Telemetry } from "../../lib/useTelemetry";
import { ErrorNote, Section } from "../Field";
import { NumberField } from "../NumberField";
import { PointUpButton } from "../PointUpButton";
import { TargetPicker } from "../TargetPicker";

const SESSION_KEY = "astropi.imagingSession";

/** In the order a night is worked: lights, flats before anything moves, then the cap goes on. */
const GROUPS: { kind: FrameGroupKind; title: string; follows?: string; instruction: string }[] = [
  {
    kind: "light",
    title: "Lights",
    instruction: "Target framed and focused, tracking on (guiding too, if you dither).",
  },
  {
    kind: "flat",
    title: "Flats",
    follows: "lights",
    instruction:
      "Don't touch focus or camera rotation. Point straight up, put the flat panel or a white t-shirt over the scope. Exposure is found automatically.",
  },
  {
    kind: "darkflat",
    title: "Dark flats",
    follows: "flats",
    instruction: "Cap the scope. Same exposure, gain and temperature as the flats.",
  },
  {
    kind: "dark",
    title: "Darks",
    follows: "lights",
    instruction:
      "Cap the scope. Same exposure, gain, offset and temperature as the lights - at the end of the night, or another night at the same temperature.",
  },
];

/**
 * One target, and the four groups of frames that make it stackable.
 *
 * Nothing runs in sequence: each group is started by hand when the rig is
 * ready for it. The calibration groups take their settings from the
 * lights (dark flats from the flats) until edited.
 */
export function SessionPanel({ telemetry, busy }: { telemetry: Telemetry; busy: boolean }) {
  const queryClient = useQueryClient();
  const [sessionId, setSessionId] = useState<string | null>(() => {
    try {
      return localStorage.getItem(SESSION_KEY);
    } catch {
      return null;
    }
  });
  const [picking, setPicking] = useState(false);

  const sessions = useQuery({ queryKey: ["imaging"], queryFn: api.imaging.list });
  const session = sessions.data?.find((candidate) => candidate.id === sessionId) ?? null;
  const camera = useQuery({ queryKey: ["camera-status"], queryFn: () => api.camera.status() });
  const cooled = camera.data?.cooling.supported ?? false;

  useEffect(() => {
    try {
      if (sessionId) localStorage.setItem(SESSION_KEY, sessionId);
      else localStorage.removeItem(SESSION_KEY);
    } catch {
      // A remembered session is a convenience.
    }
    // The night log goes in the open session's folder.
    if (sessionId) api.imaging.open(sessionId).catch(() => setSessionId(null));
  }, [sessionId]);

  // Captured counts and the flats' found exposure change on the server as
  // a group runs; refetch whenever the task moves on.
  const task = telemetry.task;
  useEffect(() => {
    queryClient.invalidateQueries({ queryKey: ["imaging"] });
  }, [queryClient, task?.state, task?.messages.length]);

  const store = (updated: ImagingSession) => {
    queryClient.setQueryData<ImagingSession[]>(["imaging"], (current) =>
      current?.some((s) => s.id === updated.id)
        ? current.map((s) => (s.id === updated.id ? updated : s))
        : [updated, ...(current ?? [])],
    );
  };

  const create = useMutation({
    mutationFn: (target: Target | ActiveTarget) =>
      api.imaging.create(
        "ra_deg" in target && target.id === "custom"
          ? { target_name: target.display_name, ra_deg: target.ra_deg, dec_deg: target.dec_deg }
          : { target_id: target.id },
      ),
    onSuccess: (created) => {
      store(created);
      setSessionId(created.id);
      setPicking(false);
    },
  });
  const update = useMutation({
    mutationFn: ({ kind, change }: { kind: FrameGroupKind; change: Partial<FrameGroup> }) =>
      api.imaging.update(session!.id, { groups: { [kind]: change } }),
    onSuccess: (updated) => {
      store(updated);
      queryClient.invalidateQueries({ queryKey: ["filters"] });
    },
  });
  const filters = useQuery({ queryKey: ["filters"], queryFn: api.imaging.filters });
  const setFilter = useMutation({
    mutationFn: (filter: string) => api.imaging.update(session!.id, { filter }),
    onSuccess: (updated) => {
      store(updated);
      queryClient.invalidateQueries({ queryKey: ["filters"] });
    },
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.imaging.remove(id),
    onSuccess: () => {
      setSessionId(null);
      queryClient.invalidateQueries({ queryKey: ["imaging"] });
    },
  });
  const start = useMutation({
    mutationFn: ({ kind, library }: { kind: FrameGroupKind; library?: boolean }) =>
      api.imaging.start(session!.id, kind, library),
  });

  const current = telemetry.target;

  return (
    <>
      <Section title="Session" hint="One target per session. Frames go to <night>_<target>/LIGHT, FLAT, DARKFLAT and DARK on the SSD.">
        <div className="row">
          <select
            value={sessionId ?? ""}
            onChange={(event) => setSessionId(event.target.value || null)}
            aria-label="Open a session"
          >
            <option value="">{sessions.data?.length ? "Choose a session…" : "No sessions yet"}</option>
            {sessions.data?.map((candidate) => (
              <option key={candidate.id} value={candidate.id}>
                {candidate.target_name} · {candidate.night}
              </option>
            ))}
          </select>
          {session && (
            <button
              className="ghost"
              style={{ flex: "0 0 auto" }}
              onClick={() => window.confirm(`Delete the session for ${session.target_name}? Frames on disk stay.`) && remove.mutate(session.id)}
            >
              Delete
            </button>
          )}
        </div>
        {picking ? (
          <TargetPicker onPick={(target) => create.mutate(target)} onCancel={() => setPicking(false)} />
        ) : (
          <div className="row">
            {current && (
              <button disabled={create.isPending} onClick={() => create.mutate(current)}>
                New session: {current.display_name}
              </button>
            )}
            <button className="ghost" onClick={() => setPicking(true)}>
              New session…
            </button>
          </div>
        )}
        {session && (
          <>
            <label>
              <span className="label">Filter</span>
              <FilterSelect
                value={session.filter}
                known={filters.data?.known ?? []}
                onChange={(filter) => filter && setFilter.mutate(filter)}
              />
            </label>
            <div className="small faint mono" style={{ wordBreak: "break-all" }}>
              {session.folder}
            </div>
          </>
        )}
      </Section>

      {session &&
        GROUPS.map((group) => (
          <GroupCard
            key={group.kind}
            spec={group}
            session={session}
            knownFilters={filters.data?.known ?? []}
            cooled={cooled}
            telemetry={telemetry}
            busy={busy}
            onChange={(change) => update.mutate({ kind: group.kind, change })}
            onStart={(library) => start.mutate({ kind: group.kind, library })}
          />
        ))}

      <ErrorNote
        error={create.error ?? update.error ?? setFilter.error ?? start.error ?? remove.error ?? sessions.error}
      />

      <TonightSection />

      {telemetry.log.length > 0 && (
        <Section title="Log">
          <div className="log">
            {[...telemetry.log].reverse().map((entry) => (
              <div key={`${entry.at}-${entry.text}`}>{entry.text}</div>
            ))}
          </div>
        </Section>
      )}
    </>
  );
}

/**
 * The filters seen before, plus one typed in. `inherit` adds a first choice
 * that clears a group's own filter back to the session's.
 */
function FilterSelect({
  value,
  known,
  inherit,
  inheritFrom = "session",
  onChange,
}: {
  value: string | null;
  known: string[];
  inherit?: string;
  inheritFrom?: string;
  onChange: (filter: string | null) => void;
}) {
  const options = value && !known.includes(value) ? [...known, value] : known;
  return (
    <select
      value={value ?? ""}
      onChange={(event) => {
        const choice = event.target.value;
        if (choice === "__custom") {
          const typed = window.prompt("Filter name")?.trim();
          if (typed) onChange(typed);
        } else {
          onChange(choice || null);
        }
      }}
      aria-label="Filter"
    >
      {inherit !== undefined && (
        <option value="">
          Same as {inheritFrom} ({inherit})
        </option>
      )}
      {options.map((name) => (
        <option key={name} value={name}>
          {name}
        </option>
      ))}
      <option value="__custom">Add custom…</option>
    </select>
  );
}

function GroupCard({
  spec,
  session,
  knownFilters,
  cooled,
  telemetry,
  busy,
  onChange,
  onStart,
}: {
  spec: (typeof GROUPS)[number];
  session: ImagingSession;
  knownFilters: string[];
  cooled: boolean;
  telemetry: Telemetry;
  busy: boolean;
  onChange: (change: Partial<FrameGroup>) => void;
  onStart: (library?: boolean) => void;
}) {
  const group = session.groups[spec.kind];
  const task = telemetry.task;
  const mine =
    task?.kind === "session_group" &&
    task.state === "running" &&
    task.detail.session_id === session.id &&
    task.detail.group === spec.kind;
  const library = spec.kind === "dark" ? session.dark_library_match : null;
  const fraction = group.count ? Math.min(1, group.captured / group.count) : 0;
  const auto = spec.kind === "flat" && group.auto_exposure;
  const left = group.captured > 0 && group.captured < group.count;

  return (
    <Section title={spec.title}>
      <div className="small dim">{spec.instruction}</div>

      <div className="row block-fields">
        <NumberField label="Frames" value={group.count} min={1} step={5} onCommit={(count) => count && onChange({ count })} />
        <NumberField
          label="Exposure"
          suffix="s"
          value={group.exposure_s}
          min={0.001}
          step={spec.kind === "flat" ? 0.1 : 10}
          placeholder={spec.kind === "flat" ? "auto" : ""}
          title={spec.kind === "flat" ? "Leave empty to find it with test frames (median at 40% of full scale)" : undefined}
          onCommit={(exposure_s) => onChange({ exposure_s })}
        />
        <NumberField label="Gain" value={group.gain} min={0} step={10} placeholder="default" onCommit={(gain) => onChange({ gain })} />
        <NumberField label="Offset" value={group.offset} min={0} step={5} placeholder="default" onCommit={(offset) => onChange({ offset })} />
        {cooled && (
          <NumberField
            label="Temp"
            suffix="°C"
            value={group.temp_c}
            min={-40}
            max={30}
            step={1}
            placeholder="off"
            onCommit={(temp_c) => onChange({ temp_c })}
          />
        )}
        {spec.kind === "light" && (
          <NumberField
            label="Dither every"
            value={group.dither_every ?? 0}
            min={0}
            step={1}
            title="Frames between dithers; 0 for none. Only while guiding."
            onCommit={(dither_every) => dither_every != null && onChange({ dither_every })}
          />
        )}
      </div>

      {spec.kind !== "dark" && (
        <label className="small">
          <span className="label">Filter</span>
          <FilterSelect
            value={group.filter ?? null}
            known={knownFilters}
            inherit={spec.kind === "darkflat" ? (session.filters.flat ?? session.filter) : session.filter}
            inheritFrom={spec.kind === "darkflat" ? "flats" : "session"}
            onChange={(filter) => onChange({ filter })}
          />
        </label>
      )}
      {spec.kind === "flat" && session.filters.flat !== session.filters.light && (
        <div className="small poor">
          Flats through {session.filters.flat}, lights through {session.filters.light}: flats only
          calibrate lights shot through the same filter.
        </div>
      )}

      {spec.follows && (
        <div className="row small">
          {group.linked ? (
            <span className="faint">Following the {spec.follows}</span>
          ) : (
            <>
              <span className="faint">Own settings</span>
              <button className="ghost" onClick={() => onChange({ linked: true })}>
                Copy from {spec.follows}
              </button>
            </>
          )}
          {spec.kind === "flat" && !auto && (
            <button className="ghost" onClick={() => onChange({ auto_exposure: true })}>
              Find exposure automatically
            </button>
          )}
          {auto && group.exposure_s != null && <span className="faint">exposure re-found on start</span>}
        </div>
      )}

      <div className="spread small">
        <span className="mono">
          {group.captured}/{group.count}
        </span>
        <span className="faint mono">{mine ? task.messages.at(-1) : ""}</span>
      </div>
      <div className="bar">
        <span style={{ width: `${Math.round(fraction * 100)}%` }} />
      </div>

      {library && (
        <div className={`small ${library.stale ? "fair" : "good"}`}>
          Covered by library ({library.count} frames, shot {library.shot_on}) - you can skip these.
          <div className="faint mono" style={{ wordBreak: "break-all" }}>
            {library.path}
          </div>
          {library.stale && <div>That set is {Math.round(library.age_days / 30)} months old; consider re-shooting it.</div>}
        </div>
      )}

      <div className="row">
        {mine ? (
          <button className="danger ghost" onClick={() => api.tasks.cancel(task.id)}>
            Stop
          </button>
        ) : spec.kind === "dark" ? (
          <>
            <button className={library ? "ghost" : "primary"} disabled={busy} onClick={() => onStart(false)}>
              {library ? "Shoot anyway, for this session" : left ? "Continue for this session" : "Shoot for this session"}
            </button>
            {!library && (
              <button disabled={busy || group.temp_c == null} onClick={() => onStart(true)} title={group.temp_c == null ? "Library darks need a set-point temperature" : "Shoot into the dark library, reusable by any session at these settings"}>
                Shoot into library
              </button>
            )}
          </>
        ) : (
          <button className="primary" disabled={busy} onClick={() => onStart()}>
            {left ? `Continue (${group.count - group.captured} left)` : `Start ${spec.title.toLowerCase()}`}
          </button>
        )}
        {spec.kind === "flat" && <PointUpButton telemetry={telemetry} busy={busy} />}
      </div>
    </Section>
  );
}

/** The window the night's lights have. */
function TonightSection() {
  const night = useQuery({ queryKey: ["night"], queryFn: api.night, staleTime: 10 * 60_000 });
  if (!night.data) return null;

  return (
    <Section title="Tonight">
      <div className="spread">
        <div>
          <div className="label">Dark from</div>
          <div className="readout">{clockTime(night.data.astronomical_dusk)}</div>
        </div>
        <div>
          <div className="label">Until</div>
          <div className="readout">{clockTime(night.data.astronomical_dawn)}</div>
        </div>
        <div>
          <div className="label">Darkness</div>
          <div className={`readout ${night.data.dark_hours > 5 ? "good" : "fair"}`}>
            {duration(night.data.dark_hours)}
          </div>
        </div>
      </div>
      <div className="small dim">
        Moon {moonPhaseName(night.data.moon_illumination)} &middot;{" "}
        {Math.round(night.data.moon_illumination * 100)}% lit &middot;{" "}
        {night.data.moon_altitude_deg > 0
          ? `up at ${night.data.moon_altitude_deg.toFixed(0)}°`
          : "below the horizon"}
      </div>
    </Section>
  );
}
