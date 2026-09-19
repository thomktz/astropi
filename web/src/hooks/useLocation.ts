import { useEffect, useState } from "react";

export interface Location {
  lat: number;
  lon: number;
}

const STORAGE_KEY = "astropi.location";

function loadStored(): Location | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Location) : null;
  } catch {
    return null;
  }
}

function storeLocation(loc: Location) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(loc));
  } catch {
    // ignore - not critical if it doesn't persist
  }
}

export function useLocation() {
  const [location, setLocation] = useState<Location | null>(() => loadStored());
  const [source, setSource] = useState<"geolocation" | "manual" | "none">(location ? "manual" : "none");

  useEffect(() => {
    // Browser Geolocation only works over HTTPS or localhost - on a Pi served
    // over plain HTTP on the LAN it will silently fail, hence the manual
    // fallback exposed via setManualLocation.
    if (!navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        const loc = { lat: pos.coords.latitude, lon: pos.coords.longitude };
        setLocation(loc);
        setSource("geolocation");
        storeLocation(loc);
      },
      () => {
        // permission denied or unavailable - keep whatever was stored, if any
      },
      { timeout: 10000 }
    );
  }, []);

  const setManualLocation = (loc: Location) => {
    setLocation(loc);
    setSource("manual");
    storeLocation(loc);
  };

  return { location, source, setManualLocation };
}
