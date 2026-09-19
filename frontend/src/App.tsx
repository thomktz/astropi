import { useEffect, useState } from "react";
import { CATALOG } from "./api/catalog";
import { gotoObject, park, subscribeTracking } from "./api/mountApi";
import type { CelestialObject, TrackingStatus } from "./api/types";
import { useLocation } from "./hooks/useLocation";
import { SkyMap } from "./components/SkyMap";
import { TrackingPanel } from "./components/TrackingPanel";
import { CameraPanel } from "./components/CameraPanel";
import { ObjectSearch } from "./components/ObjectSearch";
import { LocationPanel } from "./components/LocationPanel";
import "./App.css";

export default function App() {
  const [tracking, setTracking] = useState<TrackingStatus>({
    state: "idle",
    target: null,
    raDeg: 0,
    decDeg: 90,
  });
  const [now, setNow] = useState(() => new Date());
  const { location, source, setManualLocation } = useLocation();

  useEffect(() => {
    return subscribeTracking(setTracking);
  }, []);

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 60_000);
    return () => clearInterval(id);
  }, []);

  const handleSelect = (target: CelestialObject) => {
    void gotoObject(target);
  };

  return (
    <div className="app">
      <h1>astropi</h1>
      <div className="layout">
        <div className="main-panels">
          <SkyMap objects={CATALOG} targetId={tracking.target?.id ?? null} onSelect={handleSelect} />
          <ObjectSearch onSelect={handleSelect} location={location} now={now} />
        </div>
        <div className="side-panels">
          <TrackingPanel status={tracking} onPark={() => void park()} />
          <CameraPanel />
          <LocationPanel location={location} source={source} onSetManual={setManualLocation} />
        </div>
      </div>
    </div>
  );
}
