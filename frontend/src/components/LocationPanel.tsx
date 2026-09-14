import { useState } from "react";
import type { Location } from "../hooks/useLocation";

interface LocationPanelProps {
  location: Location | null;
  source: "geolocation" | "manual" | "none";
  onSetManual: (loc: Location) => void;
}

export function LocationPanel({ location, source, onSetManual }: LocationPanelProps) {
  const [lat, setLat] = useState(location ? String(location.lat) : "");
  const [lon, setLon] = useState(location ? String(location.lon) : "");

  const submit = () => {
    const latNum = parseFloat(lat);
    const lonNum = parseFloat(lon);
    if (Number.isFinite(latNum) && Number.isFinite(lonNum)) {
      onSetManual({ lat: latNum, lon: lonNum });
    }
  };

  return (
    <div className="panel location-panel">
      <h2>Location</h2>
      {location ? (
        <p className="location-current">
          {location.lat.toFixed(3)}°, {location.lon.toFixed(3)}°
          <span className="object-meta"> ({source === "geolocation" ? "device GPS" : "manual"})</span>
        </p>
      ) : (
        <p className="location-current object-meta">
          Not set - browser geolocation needs HTTPS or localhost, so on the Pi's LAN address enter it manually.
        </p>
      )}
      <div className="location-inputs">
        <input type="text" inputMode="decimal" placeholder="Latitude" value={lat} onChange={(e) => setLat(e.target.value)} />
        <input type="text" inputMode="decimal" placeholder="Longitude" value={lon} onChange={(e) => setLon(e.target.value)} />
        <button onClick={submit}>Set</button>
      </div>
    </div>
  );
}
