import { useMemo, useState } from "react";
import type { CelestialObject } from "../api/types";
import { searchCatalog } from "../api/catalog";
import { formatRiseSet, riseSetStatus, visibilityEmoji, visibilityLevel } from "../api/astro";
import type { Location } from "../hooks/useLocation";

interface ObjectSearchProps {
  onSelect: (target: CelestialObject) => void;
  location: Location | null;
  now: Date;
}

export function ObjectSearch({ onSelect, location, now }: ObjectSearchProps) {
  const [query, setQuery] = useState("");
  const results = useMemo(() => searchCatalog(query).slice(0, 30), [query]);

  return (
    <div className="panel object-search">
      <h2>Objects</h2>
      <input
        type="text"
        placeholder="Search name, common name, constellation…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      <ul className="object-list">
        {results.map((obj) => {
          const status = location ? riseSetStatus(obj.ra, obj.dec, location.lat, location.lon, now) : null;
          return (
            <li key={obj.id}>
              <button onClick={() => onSelect(obj)}>
                <span className="object-name">{obj.name}</span>
                {obj.commonNames.length > 0 && (
                  <span className="object-common">{obj.commonNames.join(", ")}</span>
                )}
                <span className="object-meta">
                  {obj.type} · mag {obj.magnitude.toFixed(1)}
                  {status && (
                    <>
                      {" · "}
                      <span className={`visibility visibility-${visibilityLevel(status)}`}>
                        {visibilityEmoji(status)} {formatRiseSet(status)}
                      </span>
                    </>
                  )}
                </span>
              </button>
            </li>
          );
        })}
        {results.length === 0 && <li className="object-empty">No matches</li>}
      </ul>
    </div>
  );
}
