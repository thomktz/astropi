import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import { altitudeQuality, clockTime, degrees, duration } from "../../lib/format";
import type { Target } from "../../lib/types";
import { AltitudeChart } from "../AltitudeChart";
import { ErrorNote } from "../Field";

/**
 * Choosing what to shoot, and sending the mount there.
 *
 * Results carry their current altitude from the backend, so the list
 * answers "is this up right now" without a request per row.
 */
export function TargetPanel({ busy }: { busy: boolean }) {
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

  return (
    <>
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
    </>
  );
}
