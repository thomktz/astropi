import { useMutation } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { arcmin } from "../lib/format";
import type { PolarError } from "../lib/types";
import type { Telemetry } from "../lib/useTelemetry";
import { ErrorNote, Field, Panel } from "./Panel";

/** Below this the polar axis is good enough for long unguided sub-exposures. */
const EXCELLENT_ARCMIN = 2.0;
const REFINE_INTERVAL_MS = 6_000;

/**
 * Three-point polar alignment, then live feedback while the knobs turn.
 *
 * The refine loop is the part that matters in the field: the operator has
 * both hands on the mount and cannot keep pressing a button, so once the
 * measurement is done this keeps solving and updating on its own.
 */
export function PolarAlignPanel({ telemetry, busy }: { telemetry: Telemetry; busy: boolean }) {
  const [refining, setRefining] = useState(false);
  const [live, setLive] = useState<PolarError | null>(null);
  const refineRef = useRef<() => void>(() => {});

  const measure = useMutation({
    mutationFn: () => api.tasks.polarAlign({ points: 3, separation_deg: 25 }),
    onSuccess: () => setLive(null),
  });

  const refine = useMutation({
    mutationFn: api.tasks.polarRefine,
    onSuccess: setLive,
  });

  refineRef.current = () => {
    if (!refine.isPending) refine.mutate();
  };

  useEffect(() => {
    if (!refining) return;
    refineRef.current();
    const timer = setInterval(() => refineRef.current(), REFINE_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [refining]);

  // The measurement result arrives over the socket; the refine result comes
  // straight back from its own request.
  const measured = telemetry.polar;
  const shown: PolarError | null = live ?? measured ?? null;
  const total = shown?.total_error_arcmin ?? null;

  return (
    <Panel title="Polar alignment">
      {!shown && (
        <p className="small dim" style={{ margin: 0 }}>
          Sweeps three points in hour angle and plate-solves each. The circle they trace gives the
          mount&apos;s true rotation axis, so no view of Polaris is needed.
        </p>
      )}

      {shown && (
        <>
          <div className="spread">
            <Field
              label="Total error"
              value={arcmin(total)}
              tone={total == null ? undefined : total < EXCELLENT_ARCMIN ? "good" : total < 10 ? "fair" : "poor"}
            />
            <Field label="Altitude" value={arcmin(shown.altitude_error_arcmin)} />
            <Field label="Azimuth" value={arcmin(shown.azimuth_error_arcmin)} />
          </div>
          <ul className="stack small" style={{ margin: 0, paddingLeft: 18 }}>
            {shown.instructions.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </>
      )}

      <div className="row">
        <button
          className="primary"
          disabled={busy || measure.isPending}
          onClick={() => measure.mutate()}
        >
          {measure.isPending ? "Measuring..." : shown ? "Measure again" : "Measure"}
        </button>
        <button
          disabled={!measured || busy}
          onClick={() => setRefining((on) => !on)}
          aria-pressed={refining}
          title="Keeps solving while you turn the knobs"
        >
          {refining ? "Stop live" : "Live adjust"}
        </button>
      </div>

      {refining && (
        <div className="small dim">
          Re-solving every {REFINE_INTERVAL_MS / 1000}s &mdash; turn the knobs and watch the numbers fall.
        </div>
      )}

      <ErrorNote error={measure.error ?? refine.error} />
    </Panel>
  );
}
