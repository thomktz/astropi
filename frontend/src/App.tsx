import { useEffect, useState } from "react";
import { capturePhoto, gotoObject, listObjects, park, subscribeCamera, subscribeTracking } from "./api/mockApi";
import type { CameraStatus, CelestialObject, TrackingStatus } from "./api/types";
import { useLocation } from "./hooks/useLocation";
import { SkyMap } from "./components/SkyMap";
import { TrackingPanel } from "./components/TrackingPanel";
import { CameraPanel } from "./components/CameraPanel";
import { ObjectSearch } from "./components/ObjectSearch";
import { LocationPanel } from "./components/LocationPanel";
import "./App.css";

const objects = listObjects();

export default function App() {
  const [tracking, setTracking] = useState<TrackingStatus | null>(null);
  const [camera, setCamera] = useState<CameraStatus | null>(null);
  const [now, setNow] = useState(() => new Date());
  const { location, source, setManualLocation } = useLocation();

  useEffect(() => {
    const unsubTracking = subscribeTracking(setTracking);
    const unsubCamera = subscribeCamera(setCamera);
    return () => {
      unsubTracking();
      unsubCamera();
    };
  }, []);

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 60_000);
    return () => clearInterval(id);
  }, []);

  if (!tracking || !camera) return null;

  const handleSelect = (target: CelestialObject) => {
    void gotoObject(target);
  };

  return (
    <div className="app">
      <h1>astropi</h1>
      <div className="layout">
        <div className="main-panels">
          <SkyMap objects={objects} targetId={tracking.target?.id ?? null} onSelect={handleSelect} />
          <ObjectSearch onSelect={handleSelect} location={location} now={now} />
        </div>
        <div className="side-panels">
          <TrackingPanel status={tracking} onPark={() => void park()} />
          <CameraPanel status={camera} onCapture={() => void capturePhoto()} />
          <LocationPanel location={location} source={source} onSetManual={setManualLocation} />
        </div>
      </div>
    </div>
  );
}
