import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../lib/api";
import type { Place } from "../lib/types";
import { ErrorNote, Panel } from "./Panel";

/**
 * Where the telescope is.
 *
 * Stored on the backend, not per browser: every device that opens the
 * dashboard has to agree about the horizon, and the Pi serves over plain
 * HTTP on the LAN, where the browser geolocation API refuses to work at all.
 */
export function SitePanel() {
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);

  const site = useQuery({ queryKey: ["site"], queryFn: api.site.get });
  const places = useQuery({
    queryKey: ["places", query],
    queryFn: () => api.site.search(query),
    enabled: query.trim().length >= 2,
  });

  const choose = useMutation({
    mutationFn: (place: Place) =>
      api.site.set({
        latitude_deg: place.latitude_deg,
        longitude_deg: place.longitude_deg,
        elevation_m: place.elevation_m ?? 0,
        name: place.name,
      }),
    onSuccess: () => {
      setQuery("");
      setOpen(false);
      // Everything derived from the site is now wrong: altitudes, rise and
      // set times, what is worth pointing at.
      queryClient.invalidateQueries();
    },
  });

  return (
    <Panel
      title="Site"
      actions={
        <button className="ghost" onClick={() => setOpen((value) => !value)}>
          {open ? "Done" : "Change"}
        </button>
      }
    >
      {site.data && (
        <div className="spread">
          <div>
            <div className="readout">{site.data.name}</div>
            <div className="small dim mono">
              {site.data.latitude_deg.toFixed(4)}, {site.data.longitude_deg.toFixed(4)}
            </div>
          </div>
        </div>
      )}

      {open && (
        <>
          <input
            type="search"
            value={query}
            placeholder="Search for a town or city"
            onChange={(event) => setQuery(event.target.value)}
            aria-label="Search for a place"
          />
          <div className="results">
            {places.data?.map((place) => (
              <button
                key={`${place.name}-${place.latitude_deg}-${place.longitude_deg}`}
                className="result"
                onClick={() => choose.mutate(place)}
              >
                <span>
                  <div className="name">{place.name}</div>
                  <div className="meta">
                    {[place.admin1, place.country].filter(Boolean).join(", ")}
                  </div>
                </span>
                <span className="alt small dim">
                  {place.latitude_deg.toFixed(2)}, {place.longitude_deg.toFixed(2)}
                </span>
              </button>
            ))}
            {places.isError && <div className="small faint">Place lookup needs internet.</div>}
          </div>
          <ErrorNote error={choose.error} />
        </>
      )}
    </Panel>
  );
}
