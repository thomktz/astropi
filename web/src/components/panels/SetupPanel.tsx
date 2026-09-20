import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../lib/api";
import type { Place } from "../../lib/types";
import { ErrorNote, Section } from "../Field";

/** Site, connected devices, and mount controls that are not a GoTo. */
export function SetupPanel({ night, onToggleNight }: { night: boolean; onToggleNight: () => void }) {
  return (
    <>
      <SiteSection />
      <DeviceSection />
      <Section title="Display">
        <button onClick={onToggleNight} aria-pressed={night}>
          {night ? "Leave night mode" : "Night mode (red only)"}
        </button>
        <p className="small faint" style={{ margin: 0 }}>
          Dark adaptation takes twenty minutes to build and seconds of white screen to destroy.
        </p>
      </Section>
    </>
  );
}

/**
 * Where the telescope is.
 *
 * Stored on the backend, not per browser: every device that opens the
 * dashboard has to agree about the horizon, and the Pi serves over plain
 * HTTP on the LAN, where the browser geolocation API refuses to work.
 */
function SiteSection() {
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");

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
      // Everything derived from the site is now wrong: altitudes, rise and
      // set times, what is worth pointing at.
      queryClient.invalidateQueries();
    },
  });

  return (
    <Section title="Observing site">
      {site.data && (
        <div className="spread">
          <div className="readout">{site.data.name}</div>
          <div className="small dim mono">
            {site.data.latitude_deg.toFixed(4)}, {site.data.longitude_deg.toFixed(4)}
          </div>
        </div>
      )}
      <input
        type="search"
        value={query}
        placeholder="Search for a town or city"
        onChange={(event) => setQuery(event.target.value)}
        aria-label="Search for a place"
      />
      {query.trim().length >= 2 && (
        <div className="results">
          {places.data?.map((place) => (
            <button
              key={`${place.name}-${place.latitude_deg}-${place.longitude_deg}`}
              className="result"
              onClick={() => choose.mutate(place)}
            >
              <span>
                <div className="name">{place.name}</div>
                <div className="meta">{[place.admin1, place.country].filter(Boolean).join(", ")}</div>
              </span>
              <span className="alt small dim">
                {place.latitude_deg.toFixed(2)}, {place.longitude_deg.toFixed(2)}
              </span>
            </button>
          ))}
          {places.isError && <div className="small faint">Place lookup needs internet.</div>}
        </div>
      )}
      <ErrorNote error={choose.error} />
    </Section>
  );
}

function DeviceSection() {
  const devices = useQuery({ queryKey: ["devices"], queryFn: api.devices, refetchInterval: 20_000 });
  if (!devices.data) return null;

  return (
    <Section title="Devices">
      <div className="stack small">
        {Object.entries(devices.data).map(([role, device]) => (
          <div key={role} className="spread">
            <span>
              <span className={`dot ${device.connection === "connected" ? "live" : "down"}`} /> {device.name}
            </span>
            <span className="faint mono">{device.driver}</span>
          </div>
        ))}
      </div>
    </Section>
  );
}
