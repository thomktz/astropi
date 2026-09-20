import { useCallback, useEffect, useState } from "react";
import { Drawer } from "./components/Drawer";
import { Rail } from "./components/Rail";
import { RAIL, type DrawerId } from "./components/railEntries";
import { StatusStrip } from "./components/StatusStrip";
import { Viewer } from "./components/Viewer";
import { AlignPanel } from "./components/panels/AlignPanel";
import { CameraPanel } from "./components/panels/CameraPanel";
import { GuidingPanel } from "./components/panels/GuidingPanel";
import { MountPanel } from "./components/panels/MountPanel";
import { OverviewPanel } from "./components/panels/OverviewPanel";
import { SessionPanel } from "./components/panels/SessionPanel";
import { SetupPanel } from "./components/panels/SetupPanel";
import { TargetPanel } from "./components/panels/TargetPanel";
import { badgeClass, cameraHealth, guidingHealth, mountHealth } from "./lib/status";
import { useTelemetry } from "./lib/useTelemetry";

const NIGHT_MODE_KEY = "astropi.night";
const DRAWER_KEY = "astropi.drawer";

/** Number keys 1-6 jump straight to a drawer; Escape closes. */
const SHORTCUTS = RAIL.map((entry) => entry.id);

export default function App() {
  const telemetry = useTelemetry();
  const [night, setNight] = useState(() => readStored(NIGHT_MODE_KEY) === "on");
  const [drawer, setDrawer] = useState<DrawerId | null>(
    () => (readStored(DRAWER_KEY) as DrawerId | null) ?? "overview",
  );

  useEffect(() => {
    document.documentElement.dataset.night = night ? "on" : "off";
    writeStored(NIGHT_MODE_KEY, night ? "on" : "off");
  }, [night]);

  // Remembered per device: a phone at the mount and a laptop indoors are
  // usually being used for different things.
  useEffect(() => {
    writeStored(DRAWER_KEY, drawer ?? "");
  }, [drawer]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      // Never steal a keystroke meant for a search box.
      const target = event.target as HTMLElement | null;
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;

      const index = Number(event.key) - 1;
      if (Number.isInteger(index) && index >= 0 && index < SHORTCUTS.length) {
        setDrawer((current) => (current === SHORTCUTS[index] ? null : SHORTCUTS[index]));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const toggleNight = useCallback(() => setNight((on) => !on), []);
  const closeDrawer = useCallback(() => setDrawer(null), []);

  // One task runs at a time, because they all drive the same hardware.
  // Panels disable their own actions rather than letting the request fail.
  const busy = telemetry.task?.state === "running";

  return (
    <div className="shell">
      <StatusStrip
        telemetry={telemetry}
        night={night}
        onToggleNight={toggleNight}
        onOpen={setDrawer}
      />

      <div className="workspace" data-drawer={drawer ? "open" : "closed"}>
        <Rail open={drawer} onSelect={setDrawer} badges={badgesFor(telemetry)} />
        <Viewer telemetry={telemetry} />
        {drawer && (
          <Drawer title={titleFor(drawer)} onClose={closeDrawer}>
            {drawer === "overview" && <OverviewPanel telemetry={telemetry} onOpen={setDrawer} />}
            {drawer === "target" && <TargetPanel busy={busy} />}
            {drawer === "mount" && <MountPanel telemetry={telemetry} />}
            {drawer === "camera" && <CameraPanel telemetry={telemetry} busy={busy} />}
            {drawer === "align" && <AlignPanel telemetry={telemetry} busy={busy} />}
            {drawer === "guiding" && <GuidingPanel telemetry={telemetry} />}
            {drawer === "session" && <SessionPanel telemetry={telemetry} busy={busy} />}
            {drawer === "setup" && <SetupPanel night={night} onToggleNight={toggleNight} />}
          </Drawer>
        )}
      </div>
    </div>
  );
}

function titleFor(id: DrawerId): string {
  return RAIL.find((entry) => entry.id === id)?.label ?? id;
}

/**
 * Dots on the rail, so a closed panel can still say something is happening.
 *
 * Without these, the cost of showing one panel at a time is that guiding
 * losing its star while the target list is open becomes invisible.
 */
function badgesFor(
  telemetry: ReturnType<typeof useTelemetry>,
): Partial<Record<DrawerId, "busy" | "good" | "warn">> {
  const badges: Partial<Record<DrawerId, "busy" | "good" | "warn">> = {};

  const guiding = badgeClass(guidingHealth(telemetry.guideState));
  if (guiding) badges.guiding = guiding;

  const mount = badgeClass(mountHealth(telemetry.mount));
  if (mount) badges.mount = mount;

  const camera = badgeClass(cameraHealth(telemetry.camera?.state));
  if (camera) badges.camera = camera;

  // A running session plan is the rig doing the thing it was set up to
  // do, so it reads green rather than amber - amber is for something in
  // progress that you might still be waiting on.
  if (telemetry.task?.state === "running") {
    badges.session = telemetry.task.kind === "session" ? "good" : "busy";
  }

  return badges;
}

/**
 * localStorage throws in a private window and when site data is blocked.
 * A remembered panel is not worth a blank screen.
 */
function readStored(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStored(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Nothing to do; the preference simply will not persist.
  }
}
