import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { altitudeQuality, degrees } from "../lib/format";
import type { Target } from "../lib/types";

/**
 * A compact catalogue search for choosing a target inline.
 *
 * Separate from the target panel's version because the job is different:
 * there you are deciding what to look at now and want the altitude curve
 * and the GoTo button, here you are filling a slot in a plan and want to
 * pick a name and get back to editing.
 */
export function TargetPicker({
  onPick,
  onCancel,
}: {
  onPick: (target: Target) => void;
  onCancel: () => void;
}) {
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(query.trim()), 220);
    return () => clearTimeout(timer);
  }, [query]);

  const results = useQuery({
    queryKey: ["targets", debounced],
    queryFn: () => (debounced ? api.targets.search(debounced) : api.targets.recommended()),
  });

  return (
    <div className="picker">
      <div className="row">
        <input
          autoFocus
          type="search"
          value={query}
          placeholder="Search for a target"
          aria-label="Search for a target"
          onChange={(event) => setQuery(event.target.value)}
        />
        <button className="ghost" style={{ flex: "0 0 auto" }} onClick={onCancel}>
          Cancel
        </button>
      </div>
      <div className="results">
        {results.data?.length === 0 && <div className="small faint">Nothing matches.</div>}
        {results.data?.map((target) => (
          <button key={target.id} className="result" onClick={() => onPick(target)}>
            <span style={{ minWidth: 0 }}>
              <div className="name">{target.display_name}</div>
              <div className="meta">
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
    </div>
  );
}
