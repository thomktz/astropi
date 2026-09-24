import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { api } from "../../lib/api";
import { clockTime, duration, moonPhaseName } from "../../lib/format";
import type { PlanBlockIn, PlanIssue, Target } from "../../lib/types";
import type { Telemetry } from "../../lib/useTelemetry";
import { ErrorNote, Section } from "../Field";
import { NumberField } from "../NumberField";
import { TargetPicker } from "../TargetPicker";

const DEFAULT_FRAMES = 30;
const DEFAULT_EXPOSURE_S = 120;

function newPlanName(): string {
  return `Session ${new Date().toLocaleDateString(undefined, { day: "numeric", month: "short" })}`;
}

/**
 * The session plan: an ordered list of targets and what to shoot on each.
 *
 * The work happens here rather than at run time. Two questions have to be
 * answered while the plan is still being edited - how long will it take,
 * and will it actually work - so every edit is re-scheduled against the
 * ephemeris and each block reports where its target will be when its turn
 * comes round.
 */
export function SessionPanel({ telemetry, busy }: { telemetry: Telemetry; busy: boolean }) {
  const queryClient = useQueryClient();
  const [planId, setPlanId] = useState<string | null>(null);
  const [name, setName] = useState(newPlanName);
  const [blocks, setBlocks] = useState<PlanBlockIn[]>([]);
  const [adding, setAdding] = useState(false);

  const saved = useQuery({ queryKey: ["plans"], queryFn: api.sessions.list });

  // Re-scheduled on every edit: this is where the planned duration and the
  // "this target will be at 22 degrees by then" warnings come from.
  const [debounced, setDebounced] = useState<{ name: string; blocks: PlanBlockIn[] }>({
    name,
    blocks,
  });
  useEffect(() => {
    const timer = setTimeout(() => setDebounced({ name, blocks }), 250);
    return () => clearTimeout(timer);
  }, [name, blocks]);

  const scheduled = useQuery({
    queryKey: ["plan-preview", debounced],
    queryFn: () => api.sessions.preview(debounced.name, debounced.blocks),
    enabled: debounced.blocks.length > 0,
  });

  const save = useMutation({
    mutationFn: () =>
      planId ? api.sessions.update(planId, name, blocks) : api.sessions.create(name, blocks),
    onSuccess: (plan) => {
      setPlanId(plan.id);
      queryClient.invalidateQueries({ queryKey: ["plans"] });
    },
  });

  const run = useMutation({
    mutationFn: async () => {
      // Always save first: running a plan that differs from what is on
      // disk would make the record of the night wrong.
      const plan = planId
        ? await api.sessions.update(planId, name, blocks)
        : await api.sessions.create(name, blocks);
      setPlanId(plan.id);
      return api.sessions.run(plan.id);
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["plans"] }),
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.sessions.remove(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["plans"] });
      startNew();
    },
  });

  const startNew = () => {
    setPlanId(null);
    setName(newPlanName());
    setBlocks([]);
  };

  const load = (id: string) => {
    const plan = saved.data?.find((candidate) => candidate.id === id);
    if (!plan) return;
    setPlanId(plan.id);
    setName(plan.name);
    setBlocks(
      plan.blocks.map((block) => ({
        id: block.id,
        target_id: block.target_id,
        target_name: block.target_name,
        ra_deg: block.ra_deg,
        dec_deg: block.dec_deg,
        frames: block.frames,
        exposure_s: block.exposure_s,
        gain: block.gain,
        binning: block.binning,
        dither_every: block.dither_every,
        center: block.center,
        autofocus: block.autofocus,
      })),
    );
  };

  const addTarget = (target: Target) => {
    setBlocks((current) => [
      ...current,
      {
        target_id: target.id,
        target_name: target.display_name,
        frames: DEFAULT_FRAMES,
        exposure_s: DEFAULT_EXPOSURE_S,
        gain: null,
        binning: 1,
        dither_every: 3,
        center: true,
        autofocus: false,
      },
    ]);
    setAdding(false);
  };

  const patch = (index: number, change: Partial<PlanBlockIn>) =>
    setBlocks((current) =>
      current.map((block, i) => (i === index ? { ...block, ...change } : block)),
    );

  const move = (index: number, by: number) =>
    setBlocks((current) => {
      const next = [...current];
      const to = index + by;
      if (to < 0 || to >= next.length) return current;
      [next[index], next[to]] = [next[to], next[index]];
      return next;
    });

  const plan = scheduled.data;
  const task = telemetry.task;
  const running = task?.state === "running";
  const scheduleFor = useMemo(
    () => new Map((plan?.blocks ?? []).map((block, index) => [index, block])),
    [plan],
  );

  return (
    <>
      {task && running && (
        <Section title="Running">
          <div className="spread">
            <div style={{ minWidth: 0 }}>
              <div className="name">{task.name}</div>
              <div className="small dim">{task.step}</div>
            </div>
            <button
              className="danger ghost"
              style={{ flex: "0 0 auto" }}
              onClick={() => api.tasks.cancel(task.id)}
            >
              Cancel
            </button>
          </div>
          {task.fraction != null && (
            <div className="bar">
              <span style={{ width: `${Math.round(task.fraction * 100)}%` }} />
            </div>
          )}
          {task.error && <div className="error">{task.error}</div>}
        </Section>
      )}

      <Section title="Plan">
        <div className="row">
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            aria-label="Plan name"
            placeholder="Plan name"
          />
          {planId && (
            <button
              className="ghost"
              style={{ flex: "0 0 auto" }}
              onClick={() => remove.mutate(planId)}
              title="Delete this plan"
            >
              Delete
            </button>
          )}
        </div>

        {(saved.data?.length ?? 0) > 0 && (
          <div className="row">
            <select
              value={planId ?? ""}
              onChange={(event) => (event.target.value ? load(event.target.value) : startNew())}
              aria-label="Load a saved plan"
            >
              <option value="">New plan…</option>
              {saved.data?.map((candidate) => (
                <option key={candidate.id} value={candidate.id}>
                  {candidate.name} ({candidate.blocks.length} blocks)
                </option>
              ))}
            </select>
          </div>
        )}
      </Section>

      <TonightSection />

      <Section
        title={`Blocks${blocks.length ? ` (${blocks.length})` : ""}`}
        hint="Each block slews to its target, plate-solves to centre it, then captures the frames you ask for."
      >
        {blocks.length === 0 && (
          <p className="small faint" style={{ margin: 0 }}>
            Nothing planned yet.
          </p>
        )}

        {blocks.map((block, index) => (
          <BlockRow
            key={block.id ?? `${block.target_id}-${index}`}
            block={block}
            schedule={scheduleFor.get(index)}
            first={index === 0}
            last={index === blocks.length - 1}
            disabled={running}
            onChange={(change) => patch(index, change)}
            onMove={(by) => move(index, by)}
            onRemove={() => setBlocks((current) => current.filter((_, i) => i !== index))}
          />
        ))}

        {adding ? (
          <TargetPicker onPick={addTarget} onCancel={() => setAdding(false)} />
        ) : (
          <button className="ghost" disabled={running} onClick={() => setAdding(true)}>
            + Add block
          </button>
        )}
      </Section>

      {plan && blocks.length > 0 && (
        <Section title="Totals">
          <div className="spread">
            <div>
              <div className="label">Runs</div>
              <div className="readout">
                {clockTime(plan.starts_at)} – {clockTime(plan.ends_at)}
              </div>
            </div>
            <div>
              <div className="label">Wall clock</div>
              <div className="readout">{duration(plan.duration_s / 3600)}</div>
            </div>
            <div>
              <div className="label">Integration</div>
              <div className="readout good">{duration(plan.integration_s / 3600)}</div>
            </div>
          </div>
          <Issues issues={plan.issues} />
        </Section>
      )}

      <div className="row">
        <button disabled={blocks.length === 0 || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : planId ? "Save" : "Save as new"}
        </button>
        <button
          className="primary"
          disabled={busy || blocks.length === 0 || run.isPending}
          onClick={() => run.mutate()}
        >
          {busy ? "Rig busy" : "Run plan"}
        </button>
      </div>

      <ErrorNote error={save.error ?? run.error ?? remove.error ?? scheduled.error} />

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

/** The window every plan is built against. */
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
          ? `up at ${night.data.moon_altitude_deg.toFixed(0)}\u00b0`
          : "below the horizon"}
      </div>
    </Section>
  );
}

function Issues({ issues }: { issues: PlanIssue[] }) {
  if (issues.length === 0) return null;
  return (
    <ul className="issues">
      {issues.map((issue) => (
        <li key={issue.message} className={issue.severity}>
          {issue.message}
        </li>
      ))}
    </ul>
  );
}

function BlockRow({
  block,
  schedule,
  first,
  last,
  disabled,
  onChange,
  onMove,
  onRemove,
}: {
  block: PlanBlockIn;
  schedule: import("../../lib/types").PlanBlock | undefined;
  first: boolean;
  last: boolean;
  disabled: boolean;
  onChange: (change: Partial<PlanBlockIn>) => void;
  onMove: (by: number) => void;
  onRemove: () => void;
}) {
  const worst = schedule?.issues.some((issue) => issue.severity === "problem")
    ? "problem"
    : schedule?.issues.length
      ? "warning"
      : "";

  return (
    <div className={`block ${worst}`}>
      <div className="spread">
        <span className="name">{block.target_name}</span>
        <span className="small faint mono">
          {schedule && `${clockTime(schedule.starts_at)}–${clockTime(schedule.ends_at)}`}
        </span>
      </div>

      <div className="row block-fields">
        <NumberField
          label="Frames"
          value={block.frames}
          min={1}
          step={1}
          disabled={disabled}
          onCommit={(frames) => frames != null && onChange({ frames })}
        />
        <NumberField
          label="Each (s)"
          value={block.exposure_s}
          min={0.1}
          step={10}
          disabled={disabled}
          onCommit={(exposure_s) => exposure_s != null && onChange({ exposure_s })}
        />
        <NumberField
          label="Gain"
          value={block.gain}
          min={0}
          step={10}
          placeholder="auto"
          disabled={disabled}
          onCommit={(gain) => onChange({ gain })}
        />
      </div>

      <div className="row small">
        <label className="check">
          <input
            type="checkbox"
            checked={block.autofocus}
            disabled={disabled}
            onChange={(event) => onChange({ autofocus: event.target.checked })}
          />
          Focus first
        </label>
        {schedule && (
          <span className="faint mono">
            {Math.round(schedule.duration_s / 60)} min &middot; alt{" "}
            {Math.round(schedule.min_altitude_deg)}&#8211;{Math.round(schedule.max_altitude_deg)}&#176;
          </span>
        )}
        <span className="block-actions">
          <button className="ghost" disabled={disabled || first} onClick={() => onMove(-1)} aria-label="Move up">
            &uarr;
          </button>
          <button className="ghost" disabled={disabled || last} onClick={() => onMove(1)} aria-label="Move down">
            &darr;
          </button>
          <button className="ghost" disabled={disabled} onClick={onRemove} aria-label="Remove block">
            &times;
          </button>
        </span>
      </div>

      {schedule && <Issues issues={schedule.issues} />}
    </div>
  );
}
