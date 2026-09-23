/**
 * Live state from the backend's WebSocket.
 *
 * One socket carries every topic, and this reduces them into a single state
 * object the whole app reads. Panels then render from pushed state rather
 * than each polling its own endpoint - which matters for guiding, where a
 * sample arrives every couple of seconds and polling would either miss
 * samples or hammer a Raspberry Pi.
 */

import { useEffect, useRef, useState } from "react";
import type { ActiveTarget, GuideSample, PolarError, SystemInfo, Task } from "./types";

/** Guide samples retained for the graph - a few minutes at typical cadence. */
const GUIDE_HISTORY = 120;
/** Backoff bounds for reconnection, in milliseconds. */
const RECONNECT_MIN_MS = 500;
const RECONNECT_MAX_MS = 10_000;

interface MountTelemetry {
  state: string;
  ra_deg: number;
  dec_deg: number;
  alt_deg: number | null;
  az_deg: number | null;
  tracking: boolean;
  /** Negative east of the meridian, positive west. 15 degrees an hour. */
  hour_angle_deg: number | null;
}

interface CameraTelemetry {
  role: string;
  state: string;
  sensor_c: number | null;
  cooling_enabled: boolean;
  cooling_target_c: number | null;
  cooling_power: number | null;
  gain: number | null;
  /** Length and start of the exposure under way, for a local countdown. */
  exposure_s: number | null;
  exposure_started_at: number | null;
  /** What the exposure is for, so live view can be told from a capture. */
  kind: string | null;
}

/**
 * One line of commentary from the guide loop.
 *
 * Calibration used to publish one word and then, half a minute later,
 * either a result or an error - and everything in between, which is
 * where it goes wrong, was invisible.
 */
export interface GuideProgress {
  phase: string;
  message?: string;
  direction?: string;
  pulse?: number;
  pulses?: number;
  pulse_ms?: number;
  exposure_s?: number;
  star_x?: number;
  star_y?: number;
  snr?: number;
  hfd?: number;
  candidates?: number;
  shift_px?: number;
  ra_shift_px?: number;
  dec_shift_px?: number;
  west_shift_px?: [number, number];
  north_shift_px?: [number, number];
  handedness?: number;
  ra_rate_arcsec_per_s?: number;
  dec_rate_arcsec_per_s?: number;
  angle_deg?: number;
  pixel_scale_arcsec?: number;
  error_arcsec?: number;
  settle_arcsec?: number;
  held_s?: number;
  settle_time_s?: number;
}

interface SolveTelemetry {
  success: boolean;
  solver?: string;
  ra_deg?: number;
  dec_deg?: number;
  stars_detected?: number;
  solve_time_s?: number;
  error?: string;
}

export interface Telemetry {
  connected: boolean;
  system: SystemInfo | null;
  mount: MountTelemetry | null;
  target: ActiveTarget | null;
  camera: CameraTelemetry | null;
  /** The Duo's second sensor, kept apart: the two run their own loops. */
  guideCamera: CameraTelemetry | null;
  guideState: string;
  /** What a calibration or a settle is doing right now, if either is. */
  guideProgress: GuideProgress | null;
  /**
   * Whether the guide state has been heard from at all.
   *
   * The default is "stopped", which is indistinguishable from a real
   * stopped loop - so without this a dashboard opened mid-settle counts
   * its own first update as the settle *starting* and opens a window
   * about it.
   */
  guideStateKnown: boolean;
  guideSamples: GuideSample[];
  lastSolve: SolveTelemetry | null;
  /** Increments whenever an imaging frame is captured, so views can refresh. */
  frameSeq: number;
  /** The same for the guide sensor, so the sub-display refreshes on its own. */
  guideFrameSeq: number;
  polar: (PolarError & { phase: string }) | null;
  task: Task | null;
  log: { at: number; text: string }[];
}

const EMPTY: Telemetry = {
  connected: false,
  system: null,
  mount: null,
  target: null,
  camera: null,
  guideCamera: null,
  guideState: "stopped",
  guideProgress: null,
  guideStateKnown: false,
  guideSamples: [],
  lastSolve: null,
  frameSeq: 0,
  guideFrameSeq: 0,
  polar: null,
  task: null,
  log: [],
};

interface Envelope {
  topic: string;
  timestamp?: number;
  payload: Record<string, unknown>;
}

function reduce(state: Telemetry, event: Envelope): Telemetry {
  const { topic, payload } = event;

  switch (topic) {
    case "hello":
      return { ...state, system: payload as unknown as SystemInfo };

    case "mount.position":
      return { ...state, mount: payload as unknown as MountTelemetry };

    case "target.active":
      return { ...state, target: (payload.target as ActiveTarget | null) ?? null };

    case "camera.state": {
      // Two sensors, two slots. They were sharing one, which meant the
      // guide loop's frames - far more frequent - overwrote the imaging
      // camera's state, cooling readout and all.
      const telemetry = payload as unknown as CameraTelemetry;
      return payload.role === "guide_camera"
        ? { ...state, guideCamera: telemetry }
        : { ...state, camera: telemetry };
    }

    case "guiding.state":
      // The last line of commentary is kept, deliberately. A calibration
      // publishes its result and *then* goes back to "stopped", so
      // clearing on stopped threw away the one part worth reading and
      // left the window claiming to be settling.
      return { ...state, guideState: String(payload.state), guideStateKnown: true };

    case "guiding.progress":
      return { ...state, guideProgress: payload as unknown as GuideProgress };

    case "guiding.sample": {
      const samples = [...state.guideSamples, payload as unknown as GuideSample];
      return { ...state, guideSamples: samples.slice(-GUIDE_HISTORY) };
    }

    case "camera.frame":
      // The payload is not kept: the frame itself is fetched from the
      // backend. This is only the nudge that one now exists - counted per
      // sensor, so a guide frame does not make the main viewer refetch a
      // picture that has not changed.
      return payload.role === "guide_camera"
        ? { ...state, guideFrameSeq: state.guideFrameSeq + 1 }
        : { ...state, frameSeq: state.frameSeq + 1 };

    case "solve.result":
      return { ...state, lastSolve: payload as unknown as SolveTelemetry };

    case "polar.align": {
      if (payload.phase === "measurement") return state;
      return { ...state, polar: payload as unknown as PolarError & { phase: string } };
    }

    case "task.update": {
      const task = payload as unknown as Task;
      // Keep the last message of each update as a running log, so the
      // operator can see what happened without watching the whole time.
      const latest = task.messages.at(-1);
      const log =
        latest && latest !== state.log.at(-1)?.text
          ? [...state.log, { at: Date.now(), text: latest }].slice(-60)
          : state.log;
      return { ...state, task, log };
    }

    default:
      return state;
  }
}

export function useTelemetry(): Telemetry {
  const [state, setState] = useState<Telemetry>(EMPTY);
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let disposed = false;
    let retryMs = RECONNECT_MIN_MS;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const connect = () => {
      if (disposed) return;
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      const socket = new WebSocket(`${scheme}://${window.location.host}/ws`);
      socketRef.current = socket;

      socket.onopen = () => {
        retryMs = RECONNECT_MIN_MS;
        setState((previous) => ({ ...previous, connected: true }));
      };

      socket.onmessage = (message) => {
        try {
          setState((previous) => reduce(previous, JSON.parse(message.data)));
        } catch {
          // A malformed frame is not worth tearing the connection down for.
        }
      };

      socket.onclose = () => {
        setState((previous) => ({ ...previous, connected: false }));
        if (disposed) return;
        // Exponential backoff: the Pi may be rebooting, or the phone may
        // have wandered out of Wi-Fi range at the far end of the garden.
        timer = setTimeout(connect, retryMs);
        retryMs = Math.min(retryMs * 2, RECONNECT_MAX_MS);
      };
    };

    connect();
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
      socketRef.current?.close();
    };
  }, []);

  return state;
}
