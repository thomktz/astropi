import { useEffect, useState } from "react";
import { CameraPanel } from "./components/CameraPanel";
import { GuidingPanel } from "./components/GuidingPanel";
import { MountPanel } from "./components/MountPanel";
import { PolarAlignPanel } from "./components/PolarAlignPanel";
import { NightPanel, SessionPanel } from "./components/SessionPanel";
import { SitePanel } from "./components/SitePanel";
import { StatusBar } from "./components/StatusBar";
import { TargetPanel } from "./components/TargetPanel";
import { useTelemetry } from "./lib/useTelemetry";

const NIGHT_MODE_KEY = "astropi.night";

export default function App() {
  const telemetry = useTelemetry();
  const [night, setNight] = useState(() => localStorage.getItem(NIGHT_MODE_KEY) === "on");

  useEffect(() => {
    document.documentElement.dataset.night = night ? "on" : "off";
    localStorage.setItem(NIGHT_MODE_KEY, night ? "on" : "off");
  }, [night]);

  // One task runs at a time, because they all drive the same hardware.
  // Panels disable their own actions rather than letting the request fail.
  const busy = telemetry.task?.state === "running";

  return (
    <div className="app">
      <StatusBar telemetry={telemetry} night={night} onToggleNight={() => setNight((on) => !on)} />

      <div className="grid">
        <TargetPanel busy={busy} />
        <CameraPanel telemetry={telemetry} />
        <SessionPanel telemetry={telemetry} busy={busy} />
        <MountPanel telemetry={telemetry} />
        <GuidingPanel telemetry={telemetry} />
        <PolarAlignPanel telemetry={telemetry} busy={busy} />
        <NightPanel />
        <SitePanel />
      </div>
    </div>
  );
}
