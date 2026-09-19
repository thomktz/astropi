import { useEffect, useState } from "react";
import type { Location } from "../hooks/useLocation";
import { searchCities, type CityResult } from "../api/geocode";

interface LocationPanelProps {
  location: Location | null;
  source: "geolocation" | "manual" | "none";
  onSetManual: (loc: Location) => void;
}

export function LocationPanel({ location, source, onSetManual }: LocationPanelProps) {
  const [lat, setLat] = useState(location ? String(location.lat) : "");
  const [lon, setLon] = useState(location ? String(location.lon) : "");
  const [cityQuery, setCityQuery] = useState("");
  const [cityResults, setCityResults] = useState<CityResult[]>([]);

  useEffect(() => {
    if (cityQuery.trim().length < 2) {
      setCityResults([]);
      return;
    }
    let cancelled = false;
    const timer = setTimeout(() => {
      searchCities(cityQuery).then((results) => {
        if (!cancelled) setCityResults(results);
      });
    }, 300);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [cityQuery]);

  const submit = () => {
    const latNum = parseFloat(lat);
    const lonNum = parseFloat(lon);
    if (Number.isFinite(latNum) && Number.isFinite(lonNum)) {
      onSetManual({ lat: latNum, lon: lonNum });
    }
  };

  const selectCity = (city: CityResult) => {
    onSetManual({ lat: city.lat, lon: city.lon });
    setLat(String(city.lat));
    setLon(String(city.lon));
    setCityQuery("");
    setCityResults([]);
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

      <div className="city-search">
        <input
          type="text"
          placeholder="Nearest city…"
          value={cityQuery}
          onChange={(e) => setCityQuery(e.target.value)}
        />
        {cityResults.length > 0 && (
          <ul className="city-results">
            {cityResults.map((city, i) => (
              <li key={i}>
                <button onClick={() => selectCity(city)}>
                  {city.name}
                  {city.admin1 ? `, ${city.admin1}` : ""}
                  {city.country ? `, ${city.country}` : ""}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="location-inputs">
        <input type="text" inputMode="decimal" placeholder="Latitude" value={lat} onChange={(e) => setLat(e.target.value)} />
        <input type="text" inputMode="decimal" placeholder="Longitude" value={lon} onChange={(e) => setLon(e.target.value)} />
        <button onClick={submit}>Set</button>
      </div>
    </div>
  );
}
