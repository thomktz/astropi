import { useCallback, useEffect, useState } from "react";
import { CaptureOverlay } from "./components/CaptureOverlay";
import { Drawer } from "./components/Drawer";
import { GotoProgress } from "./components/GotoProgress";
import { GuidingProgress } from "./components/GuidingProgress";
import { Rail } from "./components/Rail";
import { RAIL, type DrawerId } from "./components/railEntries";
import { StatusStrip } from "./components/StatusStrip";
import { Viewer } from "./components/Viewer";
import { AlignPanel } from "./components/panels/AlignPanel";
import { CameraPanel } from "./components/panels/CameraPanel";
import { GuidingPanel } from "./components/panels/GuidingPanel";
import { OverviewPanel } from "./components/panels/OverviewPanel";
import { SessionPanel } from "./components/panels/SessionPanel";
import { SetupPanel } from "./components/panels/SetupPanel";
import { TargetPanel } from "./components/panels/TargetPanel";
import { badgeClass, cameraHealth, guidingHealth, mountHealth } from "./lib/status";
import type { FrameSummary } from "./lib/types";
import { useTelemetry } from "./lib/useTelemetry";

const NIGHT_MODE_KEY = "astropi.night";
const DRAWER_KEY = "astropi.drawer";

/** Number keys 1-6 jump straight to a drawer; Escape closes. */
const SHORTCUTS = RAIL.map((entry) => entry.id);

export default function App() {
  const telemetry = useTelemetry();
  // Held here rather than in either of the two places a capture can be
  // started from, so both raise the same overlay over the same display.
  const [captured, setCaptured] = useState<FrameSummary | null>(null);
  // Which centring run has a window open, and which has been dismissed.
  // Both are task ids, so the next GoTo opens a window of its own.
  const [openGoto, setOpenGoto] = useState<string | null>(null);
  const [hiddenGoto, setHiddenGoto] = useState<string | null>(null);
  // Calibration and settling get the same treatment, keyed on the run
  // they belong to rather than on a boolean: dismissing this one must
  // not dismiss the next calibration too.
  const [guidingRun, setGuidingRun] = useState({ id: 0, busy: false });
  const [hiddenGuiding, setHiddenGuiding] = useState<number | null>(null);
  const [night, setNight] = useState(() => readStored(NIGHT_MODE_KEY) === "on");
  const [drawer, setDrawer] = useState<DrawerId | null>(
    () => {
      // The remembered panel may be one a later build removed, so it is
      // checked against the rail rather than trusted.
      const stored = readStored(DRAWER_KEY);
      return RAIL.some((entry) => entry.id === stored) ? (stored as DrawerId) : "overview";
    },
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

  // Opened by seeing a centring run *start*, and kept until dismissed -
  // including after it finishes, because how well it centred is the part
  // worth reading. A dashboard opened afterwards never saw it start, so
  // it does not get an old run replayed at it as if it were live.
  const task = telemetry.task;
  const startedGoto =
    task?.kind === "goto_center" && task.state === "running" ? task.id : null;
  if (startedGoto && startedGoto !== openGoto && startedGoto !== hiddenGoto) {
    setOpenGoto(startedGoto);
  }
  const gotoTask = task && task.id === openGoto && task.id !== hiddenGoto ? task : null;

  // A calibration or a settle is worth watching; guiding steadily is
  // not, and neither is a stopped loop. The window opens when one
  // starts and stays until it is dismissed - closing it the moment the
  // state went back to "stopped" took the result down with it, which is
  // the one part worth reading: the rates it measured.
  const guidingBusy =
    telemetry.guideState === "calibrating" || telemetry.guideState === "settling";
  if (guidingBusy !== guidingRun.busy) {
    setGuidingRun({ id: guidingBusy ? guidingRun.id + 1 : guidingRun.id, busy: guidingBusy });
  }
  const showGuiding = guidingRun.id > 0 && hiddenGuiding !== guidingRun.id;

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
        <Viewer
          telemetry={telemetry}
          busy={busy}
          onOpenGuiding={() => setDrawer("guiding")}
          onCaptured={setCaptured}
        />
        {drawer && (
          <Drawer title={titleFor(drawer)} onClose={closeDrawer}>
            {drawer === "overview" && <OverviewPanel telemetry={telemetry} onOpen={setDrawer} />}
            {drawer === "target" && <TargetPanel telemetry={telemetry} busy={busy} />}
            {drawer === "camera" && (
              <CameraPanel telemetry={telemetry} busy={busy} onCaptured={setCaptured} />
            )}
            {drawer === "align" && <AlignPanel telemetry={telemetry} busy={busy} />}
            {drawer === "guiding" && <GuidingPanel telemetry={telemetry} />}
            {drawer === "session" && <SessionPanel telemetry={telemetry} busy={busy} />}
            {drawer === "setup" && <SetupPanel night={night} onToggleNight={toggleNight} />}
          </Drawer>
        )}
      </div>

      {/*
        A centring run is the one operation whose interesting part is the
        loop rather than the result, so it opens a window of its own and
        keeps it until dismissed - including after it finishes, because
        how well it centred is the thing worth reading.
      */}
      {/*
        One of these at a time. A calibration or a settle takes half a
        minute and wants the screen; a centring run keeps its state, so
        its window comes back the moment the guiding one is done.
      */}
      {!showGuiding && gotoTask && gotoTask.id !== hiddenGoto && (
        <GotoProgress
          task={gotoTask}
          telemetry={telemetry}
          onClose={() => setHiddenGoto(gotoTask.id)}
        />
      )}

      {showGuiding && (
        <GuidingProgress telemetry={telemetry} onClose={() => setHiddenGuiding(guidingRun.id)} />
      )}

      {captured && <CaptureOverlay frame={captured} onClose={() => setCaptured(null)} />}
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
  if (mount) badges.target = mount;

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
