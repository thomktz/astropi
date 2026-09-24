import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../lib/api";
import type { Place } from "../../lib/types";
import { ErrorNote, Section } from "../Field";
import { Hint } from "../Hint";

/** Site, connected devices, and mount controls that are not a GoTo. */
export function SetupPanel({ night, onToggleNight }: { night: boolean; onToggleNight: () => void }) {
  return (
    <>
      <SiteSection />
      <MountSection />
      <DeviceSection />
      <Section
        title="Display"
        hint="Dark adaptation takes twenty minutes to build and seconds of white screen to destroy."
      >
        <button onClick={onToggleNight} aria-pressed={night}>
          {night ? "Leave night mode" : "Night mode (red only)"}
        </button>
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

/**
 * Which mount the rig is driving: the one on the end of the cable, or the
 * simulated one.
 *
 * The simulator is not a lesser mode to escape from. It is how this gets
 * worked on indoors, how a sequence is rehearsed before a clear night is
 * spent on it, and what everything falls back to when the real mount is
 * packed away - so switching either way is one click, and the choice is
 * remembered across restarts.
 */
/**
 * Goto speeds, as multiples of sidereal.
 *
 * 800 is where these controllers run when nobody tells them otherwise,
 * and it is their maximum rather than a sensible default - hence the
 * lower ones, and hence the whole control.
 */
const SLEW_RATES = [200, 400, 600, 800];

function MountSection() {
  const queryClient = useQueryClient();
  const [port, setPort] = useState<string | null>(null);

  const driver = useQuery({ queryKey: ["mount-driver"], queryFn: api.mount_driver.get });

  const speed = useMutation({
    mutationFn: (multiplier: number) => api.mount_driver.slewRate(multiplier),
    onSuccess: (info) => queryClient.setQueryData(["mount-driver"], info),
  });

  const choose = useMutation({
    mutationFn: ({ next, usePort }: { next: string; usePort?: string }) =>
      api.mount_driver.set(next, usePort),
    onSuccess: (info) => {
      queryClient.setQueryData(["mount-driver"], info);
      // The mount changed: everything that describes one is now stale.
      queryClient.invalidateQueries({ queryKey: ["devices"] });
      queryClient.invalidateQueries({ queryKey: ["mount"] });
    },
  });

  const info = driver.data;
  if (!info) return null;

  const chosenPort = port ?? info.port;
  const real = info.driver === "synta";

  return (
    <Section
      title="Mount"
      hint={
        <>
          The simulator is not a lesser mode: it is how this gets worked on indoors and how a
          session is rehearsed before a clear night is spent on it. Switching either way is one
          click, the choice is remembered, and the rig stays on the mount it has if the new one
          does not answer.
        </>
      }
    >
      <div className="row quick">
        <button
          className="ghost"
          aria-pressed={!real}
          disabled={choose.isPending}
          onClick={() => choose.mutate({ next: "simulator" })}
          title="A modelled mount with real geometry - misalignment, periodic error, backlash"
        >
          Simulated
        </button>
        <button
          className="ghost"
          aria-pressed={real}
          disabled={choose.isPending}
          onClick={() => choose.mutate({ next: "synta", usePort: chosenPort })}
          title="The mount on the end of the serial cable"
        >
          Sky-Watcher
        </button>
        {choose.isPending && <span className="small faint">connecting…</span>}
      </div>

      {/*
        Shown for both, because choosing the port is what you do *before*
        switching over - and a list of what is actually plugged in beats
        typing a device path from memory in the dark.
      */}
      <div className="stack small">
        <div className="label">Serial port</div>
        {info.ports.length === 0 && (
          <div className="small faint">Nothing serial is plugged in, or this machine has no ports.</div>
        )}
        {info.ports.map((candidate) => (
          <button
            key={candidate}
            className="result"
            aria-pressed={candidate === chosenPort}
            onClick={() => {
              setPort(candidate);
              if (real) choose.mutate({ next: "synta", usePort: candidate });
            }}
          >
            <span className="mono small" style={{ wordBreak: "break-all" }}>
              {candidate}
            </span>
          </button>
        ))}
      </div>

      {real && (
        <div className="spread">
          <span className="small">
            <span className={`dot ${info.connection === "connected" ? "live" : "down"}`} />{" "}
            {info.connection}
          </span>
          {info.details.firmware && (
            <span className="small faint mono">firmware {info.details.firmware}</span>
          )}
        </div>
      )}

      <div className="stack small">
        <div className="label">
          Slew speed
          <Hint>
            If a slew graunches or grinds, come down a step: a motor that skips steps is also
            losing track of where it is, and the counts keep going up while the axis stands still.
          </Hint>
        </div>
        <div className="row quick">
          {SLEW_RATES.map((option) => (
            <button
              key={option}
              className="ghost"
              aria-pressed={Math.abs(info.slew_rate - option) < 1}
              disabled={speed.isPending}
              onClick={() => speed.mutate(option)}
              title={`${option}x sidereal, ${(option * 15.0410686) / 3600} degrees per second`}
            >
              {((option * 15.0410686) / 3600).toFixed(1)}&#176;/s
            </button>
          ))}
        </div>
        <div className="small faint mono">
          {info.slew_deg_per_s}&#176;/s ({Math.round(info.slew_rate)}&#215; sidereal)
        </div>
      </div>


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
