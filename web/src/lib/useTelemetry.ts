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
import type { GuideSample, PolarError, SystemInfo, Task } from "./types";

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
}

interface CameraTelemetry {
  role: string;
  state: string;
  sensor_c: number | null;
  cooling_enabled: boolean;
  cooling_target_c: number | null;
  cooling_power: number | null;
  gain: number | null;
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
  camera: CameraTelemetry | null;
  guideState: string;
  guideSamples: GuideSample[];
  lastSolve: SolveTelemetry | null;
  polar: (PolarError & { phase: string }) | null;
  task: Task | null;
  log: { at: number; text: string }[];
}

const EMPTY: Telemetry = {
  connected: false,
  system: null,
  mount: null,
  camera: null,
  guideState: "stopped",
  guideSamples: [],
  lastSolve: null,
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

    case "camera.state": {
      // Ignore the guide sensor here: this drives the imaging camera panel,
      // and the guide camera fires far more often, which would make the
      // cooling readout flicker with values from the wrong device.
      if (payload.role === "guide_camera") return state;
      return { ...state, camera: payload as unknown as CameraTelemetry };
    }

    case "guiding.state":
      return { ...state, guideState: String(payload.state) };

    case "guiding.sample": {
      const samples = [...state.guideSamples, payload as unknown as GuideSample];
      return { ...state, guideSamples: samples.slice(-GUIDE_HISTORY) };
    }

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
